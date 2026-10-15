"""ACL-1532: меняют ли наши снимки экранные значения профиля Multilogin.

Разовый замер. По умолчанию — ТОЛЬКО чтения (`page.evaluate` и
`Browser.getWindowForTarget`), без единого снимка. Цепляется к УЖЕ открытому профилю по его собственному порту
автоматизации (тот же поиск порта, что у робота:
`MultiloginDirectCDPProvider._discover_owned_debug_port`), но без
`_connect_playwright`: тот разворачивает окно, расширяет его и снимает
троттлинг — для замера это недопустимые вмешательства. Профиль не
поднимается и не останавливается; закрыт или открыт руками без порта —
скрипт выходит.

Ничего не кликает, не печатает, не переходит и не перезагружает. Снимки под
замером включаются только флагами:

  a) page.screenshot(full_page=False) — статус-снимок (account_spend);
  b) screenshots.capture — снимок упавшего шага (page_capture PNG не снимает,
     PNG отказа делает screenshots.capture: page.screenshot c animations=
     disabled, caret=hide, timeout 5 с);
  c) Page.captureScreenshot с clip и scale 2 через новую CDP-сессию (ACL-1529);
  d) Page.captureScreenshot без clip через новую CDP-сессию.

ВНИМАНИЕ, снимки меняют профиль:
  * (a) и (b) СБРАСЫВАЮТ вьюпорт профиля. Живой замер ACL-1532 на 9943
    (03.10, см. #532 и #535): после (a) вьюпорт ушёл с 1919x1079 на
    3837x2157 device px и так и остался. Флаг `--allow-viewport-reset`.
  * (c) тоже: ревью ACL-1529 нашло в headless Chromium, что
    `Page.captureScreenshot` с clip из новой сессии сбрасывает подмену
    размеров (device-metrics override), выставленную другой сессией (#532).
  * (d) не измерен: что он делает с профилем, неизвестно.
  (c) и (d) включаются флагом `--allow-cdp-capture`.
Без флагов скрипт только читает и ничего не меняет.

Свёрнутое окно скрипт не разворачивает: CDP-снимки (c, d) тогда
пропускаются — без кадра `Page.captureScreenshot` висит. Каждый шаг
CDP-снимка (подключение, сессия, поиск вкладки, сам снимок) ограничен по
времени (`CDP_SHOT_TIMEOUT_S`): при зависании вызов бросается, CDP-сессия
отключается.

Запуск (только чтения):
  python scripts/probe_screen_values.py --account 9943 --out output/acl1532_probe
Со снимками — добавить `--allow-cdp-capture` и/или `--allow-viewport-reset`.
PNG в `--out` показывают кабинет (имена, id, расходы) — не коммитить; папка
внутри репозитория допускается только под `output/` (она в .gitignore).

Печатает JSON с замерами. Номера кабинетов, имена и адреса вкладок не
печатает: от вкладок — только хост.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

#: потолок одного CDP-снимка (c, d), включая подключение и отключение
CDP_SHOT_TIMEOUT_S = 20.0

SNAPSHOT_JS = r"""
() => {
  const vv = window.visualViewport;
  const mm = q => window.matchMedia(q).matches;
  return {
    screen_width: screen.width, screen_height: screen.height,
    avail_width: screen.availWidth, avail_height: screen.availHeight,
    color_depth: screen.colorDepth,
    dpr: window.devicePixelRatio,
    inner_width: window.innerWidth, inner_height: window.innerHeight,
    outer_width: window.outerWidth, outer_height: window.outerHeight,
    vv_width: vv ? vv.width : null, vv_height: vv ? vv.height : null,
    vv_scale: vv ? vv.scale : null,
    mq_1dppx: mm('(resolution: 1dppx)'), mq_2dppx: mm('(resolution: 2dppx)'),
    screen_x: window.screenX, screen_y: window.screenY,
  };
}
"""

# Рамка ACL-1529 (PR #530, `_SHOT_FRAME_JS`): видимая область по DOM.
FRAME_JS = r"""
() => {
  const de = document.documentElement;
  const vv = window.visualViewport;
  const w = Math.floor((vv && vv.width) || de.clientWidth || window.innerWidth);
  const h = Math.floor((vv && vv.height) || de.clientHeight || window.innerHeight);
  const x = (vv && vv.pageLeft) || window.scrollX || 0;
  const y = (vv && vv.pageTop) || window.scrollY || 0;
  return {x: x, y: y, width: w, height: h};
}
"""

OUT_HELP = (
    "folder for the PNGs. The images show the ad account (names, ids, spend): "
    "never commit them. Use a temp folder or a folder under output/; any other "
    "folder inside the repository is refused."
)
CDP_FLAG_HELP = (
    "also run captures (c) CDP Page.captureScreenshot with clip and scale 2 and (d) "
    "without clip, each from a new CDP session. (c) is KNOWN TO RESET another "
    "session's device-metrics override (ACL-1529 review, headless Chromium); (d) is "
    "unmeasured. Skipped when the window is minimised. Off by default."
)
FLAG_HELP = (
    "also run captures (a) page.screenshot and (b) screenshots.capture. Both are "
    "KNOWN TO RESET the profile's viewport (ACL-1532 live run on 9943, 03.10: "
    "1919x1079 -> 3837x2157 device px, it stays). Off by default."
)


def _tab_host(url: str) -> str:
    """Только хост вкладки: в путях Meta бывают id кабинетов и имена."""
    parts = urlsplit(url or "")
    if parts.hostname:
        return parts.hostname
    return f"{parts.scheme}:" if parts.scheme else ""


def _snap(page) -> dict:
    return page.evaluate(SNAPSHOT_JS)


def _window_state(context, page) -> tuple[dict, str | None]:
    """Browser.getWindowForTarget — домен Browser, отвечает без отрисовки.

    Заодно id цели страницы: по нему CDP-снимок находит ту же вкладку в
    своём отдельном подключении."""
    s = context.new_cdp_session(page)
    try:
        info = s.send("Browser.getWindowForTarget") or {}
        target = (s.send("Target.getTargetInfo") or {}).get("targetInfo") or {}
        return dict(info.get("bounds") or {}), target.get("targetId")
    finally:
        s.detach()


async def _bounded_capture(session, params: dict, timeout_s: float) -> dict | None:
    """`Page.captureScreenshot` с потолком; сессия отключается в любом исходе.

    None — не уложились в `timeout_s`."""
    try:
        return await asyncio.wait_for(session.send("Page.captureScreenshot", params), timeout_s)
    except asyncio.TimeoutError:
        return None
    finally:
        await _quiet_detach(session)


async def _quiet_detach(session) -> None:
    try:
        await asyncio.wait_for(session.detach(), 5)
    except Exception:  # noqa: BLE001 — разрыв соединения всё равно снимет сессию
        pass


async def _cdp_shot_async(cdp_url: str, target_id: str, path: Path,
                          clip: dict | None, timeout_s: float) -> str:
    # Sync Playwright не даёт таймаута у CDPSession.send и не пускает свои
    # объекты в другой поток, поэтому снимок идёт отдельным async-подключением.
    # Выход из async_playwright рвёт соединение (браузер не закрывается).
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await asyncio.wait_for(pw.chromium.connect_over_cdp(cdp_url), timeout_s)
        for ctx in browser.contexts:
            for page in ctx.pages:
                s = await asyncio.wait_for(ctx.new_cdp_session(page), timeout_s)
                try:
                    got = await asyncio.wait_for(s.send("Target.getTargetInfo"), timeout_s)
                except BaseException:
                    await _quiet_detach(s)
                    raise
                info = (got or {}).get("targetInfo") or {}
                if info.get("targetId") != target_id:
                    await _quiet_detach(s)
                    continue
                params = {"format": "png", "captureBeyondViewport": False}
                if clip is not None:
                    params["clip"] = clip
                got = await _bounded_capture(s, params, timeout_s)
                if got is None:
                    return f"timeout after {timeout_s:g}s; CDP session detached"
                data = base64.b64decode(got.get("data") or "")
                path.write_bytes(data)
                return "ok" if data.startswith(b"\x89PNG") else "no png"
        return "target not found"


def _cdp_shot(cdp_url: str, target_id: str, path: Path, clip: dict | None,
              timeout_s: float = CDP_SHOT_TIMEOUT_S) -> str:
    box: dict = {}

    def run() -> None:
        try:
            box["r"] = asyncio.run(_cdp_shot_async(cdp_url, target_id, path, clip, timeout_s))
        except Exception as exc:  # noqa: BLE001
            box["r"] = f"error: {str(exc)[:160]}"

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout_s * 2 + 10)
    # Поток не вернулся даже после своего потолка — бросаем его: соединение
    # умрёт вместе с процессом, сессия отключится.
    return box.get("r", "timeout: capture worker did not return")


def _png_size(path: Path) -> list[int] | None:
    try:
        b = path.read_bytes()[:24]
        return [int.from_bytes(b[16:20], "big"), int.from_bytes(b[20:24], "big")]
    except Exception:
        return None


def _out_refused(out: Path) -> str | None:
    """PNG с кабинетом не должны лечь туда, где их закоммитят."""
    resolved = out.resolve()
    try:
        rel = resolved.relative_to(REPO_ROOT)
    except ValueError:
        return None
    if rel.parts[:1] == ("output",) and len(rel.parts) > 1:
        return None
    return "--out is inside the repository but not under output/; the PNGs carry account data"


def probe(context, pages, out: Path, *, allow_viewport_reset: bool,
          allow_cdp_capture: bool, shoot_cdp,
          settle_s: float = 0.5, final_wait_s: float = 3.0) -> dict:
    """Чтения и снимки на первой вкладке. `shoot_cdp(target_id, path, clip) -> str`."""
    if not pages:
        return {"stop": "profile has no open tabs"}
    result: dict = {"pages": [_tab_host(p.url) for p in pages], "steps": []}
    page = pages[0]
    result["page"] = result["pages"][0]
    win, target_id = _window_state(context, page)
    result["window"] = {k: win.get(k) for k in ("width", "height", "windowState")}
    result["viewport_reset_captures"] = "on" if allow_viewport_reset else "off (default)"
    result["cdp_captures"] = "on" if allow_cdp_capture else "off (default)"
    minimized = win.get("windowState") == "minimized"

    def step(name: str, fn) -> None:
        started = time.time()
        try:
            note = fn()
        except Exception as exc:  # noqa: BLE001
            note = f"error: {str(exc)[:160]}"
        time.sleep(settle_s)
        result["steps"].append({"step": name, "note": note,
                                "took_s": round(time.time() - started, 2),
                                "after": _snap(page)})

    result["before"] = _snap(page)

    def a():
        p = out / "a_page_screenshot.png"
        page.screenshot(path=str(p), full_page=False, timeout=15000)
        return {"png": _png_size(p)}

    def b():
        from src.fb_ads_rpa import screenshots
        path = screenshots.capture(page, "acl1532_failure_shot", base_dir=out, cdp_fallback=False)
        return {"png": _png_size(Path(path)) if path else None, "saved": bool(path)}

    def c():
        if minimized:
            return "skipped: window minimized (CDP capture would hang)"
        frame = page.evaluate(FRAME_JS)
        clip = {k: float(frame[k]) for k in ("x", "y", "width", "height")}
        clip["scale"] = 2
        p = out / "c_cdp_clip_scale2.png"
        return {"result": shoot_cdp(target_id, p, clip), "png": _png_size(p),
                "clip_css": [clip["width"], clip["height"]]}

    def d():
        if minimized:
            return "skipped: window minimized (CDP capture would hang)"
        p = out / "d_cdp_noclip.png"
        return {"result": shoot_cdp(target_id, p, None), "png": _png_size(p)}

    if allow_viewport_reset:
        step("a_page_screenshot", a)
        step("b_failure_capture", b)
    if allow_cdp_capture:
        step("c_cdp_clip_scale2", c)
        step("d_cdp_noclip", d)
    time.sleep(final_wait_s)
    result["final_after_wait"] = _snap(page)
    return result


def _find_owned_port(account: str) -> tuple[int | None, dict | None, int]:
    """Порт автоматизации уже открытого профиля; профиль не поднимается."""
    from config.paths import get_ad_account_map_path
    from src.meta_campaign_agent.ad_account_map import load_ad_account_map
    from src.fb_ads_rpa.browser import MultiloginDirectCDPProvider, _find_mlx_profile_dir

    entries = [v for k, v in load_ad_account_map(get_ad_account_map_path()).items() if account in k]
    if len(entries) != 1:
        return None, {"stop": f"expected one map entry, got {len(entries)}"}, 2
    entry = entries[0]
    udd = _find_mlx_profile_dir(entry.profile_id)
    direct = MultiloginDirectCDPProvider()
    if udd is None or not direct._profile_process_running(str(udd), entry.profile_id):
        return None, {"stop": "profile is not running; this probe never starts it"}, 3
    port = direct._discover_owned_debug_port(str(udd))
    if port is None:
        return None, {"stop": "profile is open without an automation port (by hand)"}, 4
    return port, None, 0


@contextmanager
def _connect(cdp_url: str):
    """Голый connect_over_cdp; на выходе только отключение, браузер не закрывается."""
    from src.fb_ads_rpa.config import ensure_playwright_browsers_path
    from playwright.sync_api import sync_playwright

    ensure_playwright_browsers_path()
    with sync_playwright() as pw:
        yield pw.chromium.connect_over_cdp(cdp_url)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="ACL-1532: measure whether our captures change a running Multilogin "
                    "profile's screen values. By default it only READS (no capture). "
                    "Captures are opt-in: (a)/(b) via --allow-viewport-reset, known to "
                    "reset the viewport; (c)/(d) via --allow-cdp-capture, (c) known to "
                    "reset another session's device-metrics override, (d) unmeasured.")
    ap.add_argument("--account", required=True,
                    help="part of the account sheet name, e.g. 9943 (часть имени листа)")
    ap.add_argument("--out", required=True, help=OUT_HELP)
    ap.add_argument("--allow-viewport-reset", action="store_true", help=FLAG_HELP)
    ap.add_argument("--allow-cdp-capture", action="store_true", help=CDP_FLAG_HELP)
    args = ap.parse_args(argv)
    out = Path(args.out)
    refused = _out_refused(out)
    if refused:
        print(json.dumps({"stop": refused}))
        return 2

    port, stop, rc = _find_owned_port(args.account)
    if stop is not None:
        print(json.dumps(stop))
        return rc
    out.mkdir(parents=True, exist_ok=True)

    cdp_url = f"http://127.0.0.1:{port}"
    with _connect(cdp_url) as browser:
        context = browser.contexts[0]
        result = probe(
            context, context.pages, out,
            allow_viewport_reset=args.allow_viewport_reset,
            allow_cdp_capture=args.allow_cdp_capture,
            shoot_cdp=lambda target_id, path, clip: _cdp_shot(cdp_url, target_id, path, clip),
        )
        # Только отключаемся от CDP: браузер профиля остаётся открытым.
    if "stop" in result:
        print(json.dumps(result))
        return 5
    # Живой замер 03.10: у свёрнутого окна снимок (a) не дождался кадра, а
    # подмена размеров слетела только ПОСЛЕ отключения его сессии. Поэтому
    # ещё одно чтение — новым подключением, когда наших сессий уже нет.
    time.sleep(2)
    with _connect(cdp_url) as browser:
        pages = browser.contexts[0].pages
        result["after_detach"] = _snap(pages[0]) if pages else "no open tabs"
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
