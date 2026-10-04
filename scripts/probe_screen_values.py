"""ACL-1532: меняют ли наши снимки экранные значения профиля Multilogin.

Разовый замер. Цепляется к УЖЕ открытому профилю по его собственному порту
автоматизации (тот же поиск порта, что у робота:
`MultiloginDirectCDPProvider._discover_owned_debug_port`), но без
`_connect_playwright`: тот разворачивает окно, расширяет его и снимает
троттлинг — для замера это недопустимые вмешательства. Профиль не
поднимается и не останавливается; закрыт или открыт руками без порта —
скрипт выходит.

Ничего не кликает, не печатает, не переходит и не перезагружает. Только
`page.evaluate` с чтением и способы снимка:

  a) page.screenshot(full_page=False) — статус-снимок (account_spend);
  b) screenshots.capture — снимок упавшего шага (page_capture PNG не снимает,
     PNG отказа делает screenshots.capture: page.screenshot c animations=
     disabled, caret=hide, timeout 5 с);
  c) Page.captureScreenshot с clip и scale 2 через новую CDP-сессию (ACL-1529);
  d) Page.captureScreenshot без clip через новую CDP-сессию.

ВНИМАНИЕ: снимки (a) и (b) СБРАСЫВАЮТ вьюпорт профиля. Живой замер ACL-1532
на 9943 (03.10, см. #532 и #535): после (a) вьюпорт ушёл с 1919x1079 на
3837x2157 device px и так и остался. Поэтому (a) и (b) выполняются только с
явным флагом `--allow-viewport-reset`; по умолчанию скрипт делает только
чтения и снимки (c) и (d). Без флага он ничего не меняет — насколько это
известно на сегодня: (c) и (d) как раз и меряются.

Свёрнутое окно скрипт не разворачивает: CDP-снимки (c, d) тогда
пропускаются — без кадра `Page.captureScreenshot` висит. Каждый из них ещё и
ограничен по времени (`CDP_SHOT_TIMEOUT_S`): при зависании вызов бросается,
CDP-сессия отключается.

Запуск:  python scripts/probe_screen_values.py --account 9943 --out output/acl1532_probe
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
        try:
            await asyncio.wait_for(session.detach(), 5)
        except Exception:  # noqa: BLE001 — отключение соединения ниже всё равно снимет сессию
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
                s = await ctx.new_cdp_session(page)
                info = (await s.send("Target.getTargetInfo") or {}).get("targetInfo") or {}
                if info.get("targetId") != target_id:
                    await s.detach()
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


def probe(context, pages, out: Path, *, allow_viewport_reset: bool, shoot_cdp,
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
    step("c_cdp_clip_scale2", c)
    step("d_cdp_noclip", d)
    time.sleep(final_wait_s)
    result["final_after_wait"] = _snap(page)
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="ACL-1532: measure whether our captures change a running Multilogin "
                    "profile's screen values. By default only reads plus CDP captures (c) "
                    "and (d); captures (a) and (b) are known to reset the viewport and run "
                    "only with --allow-viewport-reset.")
    ap.add_argument("--account", required=True,
                    help="part of the account sheet name, e.g. 9943 (часть имени листа)")
    ap.add_argument("--out", required=True, help=OUT_HELP)
    ap.add_argument("--allow-viewport-reset", action="store_true", help=FLAG_HELP)
    args = ap.parse_args(argv)
    out = Path(args.out)
    refused = _out_refused(out)
    if refused:
        print(json.dumps({"stop": refused}))
        return 2
    out.mkdir(parents=True, exist_ok=True)

    from config.paths import get_ad_account_map_path
    from src.meta_campaign_agent.ad_account_map import load_ad_account_map
    from src.fb_ads_rpa.browser import MultiloginDirectCDPProvider, _find_mlx_profile_dir
    from src.fb_ads_rpa.config import ensure_playwright_browsers_path
    from playwright.sync_api import sync_playwright

    entries = [v for k, v in load_ad_account_map(get_ad_account_map_path()).items() if args.account in k]
    if len(entries) != 1:
        print(json.dumps({"stop": f"expected one map entry, got {len(entries)}"}))
        return 2
    entry = entries[0]
    udd = _find_mlx_profile_dir(entry.profile_id)
    direct = MultiloginDirectCDPProvider()
    if udd is None or not direct._profile_process_running(str(udd), entry.profile_id):
        print(json.dumps({"stop": "profile is not running; this probe never starts it"}))
        return 3
    port = direct._discover_owned_debug_port(str(udd))
    if port is None:
        print(json.dumps({"stop": "profile is open without an automation port (by hand)"}))
        return 4

    ensure_playwright_browsers_path()
    cdp_url = f"http://127.0.0.1:{port}"
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(cdp_url)
        context = browser.contexts[0]
        result = probe(
            context, context.pages, out,
            allow_viewport_reset=args.allow_viewport_reset,
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
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(cdp_url)
        pages = browser.contexts[0].pages
        result["after_detach"] = _snap(pages[0]) if pages else "no open tabs"
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
