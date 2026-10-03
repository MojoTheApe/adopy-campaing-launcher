"""ACL-1532: меняют ли наши снимки экранные значения профиля Multilogin.

Разовый замер только для чтения. Цепляется к УЖЕ открытому профилю по его
собственному порту автоматизации (тот же поиск порта, что у робота:
`MultiloginDirectCDPProvider._discover_owned_debug_port`), но без
`_connect_playwright`: тот разворачивает окно, расширяет его и снимает
троттлинг — для замера это недопустимые вмешательства. Профиль не
поднимается и не останавливается; закрыт или открыт руками без порта —
скрипт выходит.

Ничего не кликает, не печатает, не переходит и не перезагружает. Только
`page.evaluate` с чтением и четыре способа снимка:

  a) page.screenshot(full_page=False) — статус-снимок (account_spend);
  b) screenshots.capture — снимок упавшего шага (page_capture PNG не снимает,
     PNG отказа делает screenshots.capture: page.screenshot c animations=
     disabled, caret=hide, timeout 5 с);
  c) Page.captureScreenshot с clip и scale 2 через новую CDP-сессию (ACL-1529);
  d) Page.captureScreenshot без clip через новую CDP-сессию.

Свёрнутое окно скрипт не разворачивает: CDP-снимки (c, d) тогда
пропускаются — без кадра `Page.captureScreenshot` висит без таймаута.

Запуск:  python scripts/probe_screen_values.py --account 9943 --out out
Печатает JSON с замерами; номера кабинетов и URL с параметрами не печатает.
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

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


def _snap(page) -> dict:
    return page.evaluate(SNAPSHOT_JS)


def _window_state(context, page) -> dict:
    """Browser.getWindowForTarget — домен Browser, отвечает без отрисовки."""
    s = context.new_cdp_session(page)
    try:
        info = s.send("Browser.getWindowForTarget") or {}
        return dict(info.get("bounds") or {})
    finally:
        s.detach()


def _cdp_shot(context, page, path: Path, clip: dict | None) -> str:
    s = context.new_cdp_session(page)
    try:
        params = {"format": "png", "captureBeyondViewport": False}
        if clip is not None:
            params["clip"] = clip
        got = s.send("Page.captureScreenshot", params) or {}
        data = base64.b64decode(got.get("data") or "")
        path.write_bytes(data)
        return "ok" if data.startswith(b"\x89PNG") else "no png"
    finally:
        s.detach()


def _png_size(path: Path) -> list[int] | None:
    try:
        b = path.read_bytes()[:24]
        return [int.from_bytes(b[16:20], "big"), int.from_bytes(b[20:24], "big")]
    except Exception:
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", required=True, help="часть имени листа, напр. 9943")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    out = Path(args.out)
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
    result: dict = {"steps": []}
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        context = browser.contexts[0]
        pages = context.pages
        result["pages"] = [urlsplit(p.url).netloc + urlsplit(p.url).path for p in pages]
        page = pages[0]
        result["page"] = result["pages"][0]
        win = _window_state(context, page)
        result["window"] = {k: win.get(k) for k in ("width", "height", "windowState")}
        minimized = win.get("windowState") == "minimized"

        def step(name: str, fn) -> None:
            started = time.time()
            try:
                note = fn()
            except Exception as exc:  # noqa: BLE001
                note = f"error: {str(exc)[:160]}"
            time.sleep(0.5)
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
            return {"result": _cdp_shot(context, page, p, clip), "png": _png_size(p),
                    "clip_css": [clip["width"], clip["height"]]}

        def d():
            if minimized:
                return "skipped: window minimized (CDP capture would hang)"
            p = out / "d_cdp_noclip.png"
            return {"result": _cdp_shot(context, page, p, None), "png": _png_size(p)}

        step("a_page_screenshot", a)
        step("b_failure_capture", b)
        step("c_cdp_clip_scale2", c)
        step("d_cdp_noclip", d)
        time.sleep(3)
        result["final_after_3s"] = _snap(page)
        # Только отключаемся от CDP: браузер профиля остаётся открытым.
    # Живой замер 03.10: у свёрнутого окна снимок (a) не дождался кадра, а
    # подмена размеров слетела только ПОСЛЕ отключения его сессии. Поэтому
    # ещё одно чтение — новым подключением, когда наших сессий уже нет.
    time.sleep(2)
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        result["after_detach"] = _snap(browser.contexts[0].pages[0])
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
