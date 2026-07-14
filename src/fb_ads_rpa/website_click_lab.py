"""
Website Click Lab — real-time stepwise solver for Conversion Location → Website.

Runs strategies A→F one-by-one against an already-open Multilogin CDP browser,
verifies after each step, and streams status for the dashboard.

API:
    POST /rpa-website-lab/prepare   — connect CDP, open/expand menu if needed
    POST /rpa-website-lab/step      — {step: "A"|"B"|"C"|"D"|"E"|"F"}
    POST /rpa-website-lab/run-all   — run A→F until success (background)
    GET  /rpa-website-lab/status
    POST /rpa-website-lab/disconnect
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from playwright.sync_api import Page, sync_playwright

from src.fb_ads_rpa.click_recorder import _find_cdp_url
from src.fb_ads_rpa.meta_dom import (
    _click_website_via_js_row,
    _click_website_via_keyboard,
    _expand_conversion_location_single,
    _find_website_option_bbox,
    _is_website_option_visible,
    _mouse_click_chain,
    _wait_for_single_expanded,
    click_conversion_location_dropdown,
    conversion_menu_is_open,
    is_website_conversion_selected,
    wait_conversion_menu_open,
)

_SHOT_DIR = Path(__file__).resolve().parents[2] / "output" / "rpa_screenshots"

# Priority order for run-all (user recommendation: A+B+F first, then C/D/E)
STEP_ORDER = ("A", "B", "F", "C", "D", "E")

STEP_META = {
    "A": {
        "title": "Coordinate click (center)",
        "desc": "page.mouse.click on Website row center / force position",
    },
    "B": {
        "title": "Full mouse chain",
        "desc": "mousemove → mousedown → mouseup with 50–100ms pauses",
    },
    "F": {
        "title": "Radio circle click",
        "desc": "Click left radio circle of Website row (not the label text)",
    },
    "C": {
        "title": "dispatchEvent / React path",
        "desc": "Native MouseEvent bubbles on Website-only row + radio child",
    },
    "D": {
        "title": "Keyboard navigation",
        "desc": "ArrowDown until Website → Enter / Space",
    },
    "E": {
        "title": "Capture golden locator",
        "desc": "Dump Playwright-best locator for the visible Website option (no click)",
    },
}


@dataclass
class StepResult:
    step: str
    ok: bool
    detail: str = ""
    website_selected: bool = False
    menu_open: bool = False
    bbox: dict | None = None
    locator_hint: str | None = None
    screenshot: str | None = None
    ts: str = ""


@dataclass
class LabState:
    connected: bool = False
    running: bool = False
    cdp_url: str | None = None
    prepared: bool = False
    website_selected: bool = False
    menu_open: bool = False
    current_step: str | None = None
    message: str = ""
    error: str | None = None
    steps: list[dict[str, Any]] = field(default_factory=list)
    winner: str | None = None
    golden_locator: dict | None = None


_state = LabState()
_lock = threading.Lock()
_pw = None
_browser = None
_page: Page | None = None
_thread: threading.Thread | None = None


def _snapshot() -> dict[str, Any]:
    with _lock:
        return {
            "ok": _state.error is None or _state.connected,
            "connected": _state.connected,
            "running": _state.running,
            "cdp_url": _state.cdp_url,
            "prepared": _state.prepared,
            "website_selected": _state.website_selected,
            "menu_open": _state.menu_open,
            "current_step": _state.current_step,
            "message": _state.message,
            "error": _state.error,
            "winner": _state.winner,
            "golden_locator": _state.golden_locator,
            "steps": list(_state.steps),
            "step_order": list(STEP_ORDER),
            "step_meta": STEP_META,
        }


def get_status() -> dict[str, Any]:
    # Refresh live verification if connected
    page = _page
    if page is not None and _state.connected:
        try:
            selected = is_website_conversion_selected(page)
            menu = conversion_menu_is_open(page)
            with _lock:
                _state.website_selected = selected
                _state.menu_open = menu
        except Exception:
            pass
    return _snapshot()


def _shot(tag: str) -> str | None:
    page = _page
    if page is None:
        return None
    try:
        _SHOT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = _SHOT_DIR / f"{stamp}_lab_{tag}.png"
        page.screenshot(path=str(path))
        return str(path)
    except Exception:
        return None


def _verify(page: Page) -> tuple[bool, bool]:
    selected = is_website_conversion_selected(page)
    menu = conversion_menu_is_open(page)
    # Success = Website selected. Menu may stay open briefly; prefer selected.
    return selected, menu


def _ensure_menu_ready(page: Page) -> str:
    """Make sure Conversion Location menu is open and Single→Website visible."""
    if is_website_conversion_selected(page):
        return "already_website"
    if not conversion_menu_is_open(page):
        click_conversion_location_dropdown(page)
        wait_conversion_menu_open(page)
        time.sleep(0.4)
    if not _is_website_option_visible(page):
        _expand_conversion_location_single(page)
        _wait_for_single_expanded(page, timeout_s=5.0)
    if _is_website_option_visible(page):
        return "website_visible"
    return "website_not_visible"


def connect(*, cdp_url: str | None = None) -> dict[str, Any]:
    global _pw, _browser, _page
    with _lock:
        if _state.connected and _page is not None:
            return get_status() | {"ok": True, "message": "Already connected"}

    url = cdp_url or _find_cdp_url()
    if not url:
        return {
            "ok": False,
            "error": "No CDP browser found. Open Multilogin / start dry-run first.",
        }

    try:
        _pw = sync_playwright().start()
        _browser = _pw.chromium.connect_over_cdp(url)
        ctx = _browser.contexts[0] if _browser.contexts else None
        if not ctx or not ctx.pages:
            raise RuntimeError("No open pages — open Ads Manager first")
        _page = ctx.pages[0]
        _page.bring_to_front()
        with _lock:
            _state.connected = True
            _state.cdp_url = url
            _state.error = None
            _state.message = f"Connected to {url}"
            _state.steps = []
            _state.winner = None
            _state.prepared = False
        return get_status() | {"ok": True}
    except Exception as exc:
        disconnect()
        return {"ok": False, "error": str(exc)}


def disconnect(*, clear_running: bool = True) -> dict[str, Any]:
    global _pw, _browser, _page
    with _lock:
        _state.connected = False
        if clear_running:
            _state.running = False
        _state.prepared = False
        if clear_running:
            _state.message = "Disconnected"
    _page = None
    try:
        if _browser:
            _browser.close()
    except Exception:
        pass
    _browser = None
    try:
        if _pw:
            _pw.stop()
    except Exception:
        pass
    _pw = None
    return get_status() | {"ok": True}


def prepare() -> dict[str, Any]:
    """Connect (if needed) and open Conversion Location → Single so Website is visible."""
    if not _state.connected or _page is None:
        conn = connect()
        if not conn.get("ok") and not _state.connected:
            return conn

    page = _page
    assert page is not None
    try:
        status = _ensure_menu_ready(page)
        selected, menu = _verify(page)
        shot = _shot("prepare")
        with _lock:
            _state.prepared = status in ("website_visible", "already_website")
            _state.website_selected = selected
            _state.menu_open = menu
            _state.message = (
                "Website already selected — lab not needed"
                if status == "already_website"
                else f"Prepared: {status}"
            )
            if selected:
                _state.winner = "already"
        return get_status() | {
            "ok": True,
            "prepare_status": status,
            "screenshot": shot,
        }
    except Exception as exc:
        with _lock:
            _state.error = str(exc)
            _state.message = f"Prepare failed: {exc}"
        return get_status() | {"ok": False, "error": str(exc)}


def _step_A(page: Page) -> StepResult:
    bbox = _find_website_option_bbox(page)
    if not bbox:
        return StepResult("A", False, "bbox not found — is Website visible?")
    try:
        page.mouse.click(float(bbox["cx"]), float(bbox["cy"]))
        time.sleep(0.55)
    except Exception as exc:
        return StepResult("A", False, f"mouse.click failed: {exc}", bbox=bbox)
    selected, menu = _verify(page)
    return StepResult(
        "A",
        selected,
        f"clicked center ({bbox['cx']:.0f},{bbox['cy']:.0f})",
        website_selected=selected,
        menu_open=menu,
        bbox=bbox,
    )


def _step_B(page: Page) -> StepResult:
    bbox = _find_website_option_bbox(page)
    if not bbox:
        return StepResult("B", False, "bbox not found")
    try:
        _mouse_click_chain(page, float(bbox["cx"]), float(bbox["cy"]))
        time.sleep(0.55)
    except Exception as exc:
        return StepResult("B", False, f"mouse chain failed: {exc}", bbox=bbox)
    selected, menu = _verify(page)
    return StepResult(
        "B",
        selected,
        f"mousemove→down→up at ({bbox['cx']:.0f},{bbox['cy']:.0f})",
        website_selected=selected,
        menu_open=menu,
        bbox=bbox,
    )


def _step_F(page: Page) -> StepResult:
    bbox = _find_website_option_bbox(page)
    if not bbox:
        return StepResult("F", False, "bbox not found")
    try:
        _mouse_click_chain(page, float(bbox["radio_x"]), float(bbox["radio_y"]))
        time.sleep(0.55)
        selected, menu = _verify(page)
        if not selected:
            page.mouse.click(float(bbox["radio_x"]), float(bbox["radio_y"]))
            time.sleep(0.55)
            selected, menu = _verify(page)
    except Exception as exc:
        return StepResult("F", False, f"radio click failed: {exc}", bbox=bbox)
    return StepResult(
        "F",
        selected,
        f"radio at ({bbox['radio_x']:.0f},{bbox['radio_y']:.0f})",
        website_selected=selected,
        menu_open=menu,
        bbox=bbox,
    )


def _step_C(page: Page) -> StepResult:
    ok_click = _click_website_via_js_row(page)
    selected, menu = _verify(page)
    return StepResult(
        "C",
        selected,
        "JS MouseEvent on Website row" + (" (dispatched)" if ok_click else " (no target)"),
        website_selected=selected,
        menu_open=menu,
    )


def _step_D(page: Page) -> StepResult:
    ok_click = _click_website_via_keyboard(page)
    selected, menu = _verify(page)
    return StepResult(
        "D",
        selected or ok_click,
        "ArrowDown / Enter / Space",
        website_selected=selected,
        menu_open=menu,
    )


def _step_E(page: Page) -> StepResult:
    """Capture golden locator hints — does not click."""
    hint = page.evaluate("""() => {
      const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
      const viz = (el) => !!(el && el.offsetParent !== null);
      const own = (el) => norm(
        Array.from(el.childNodes).filter(n => n.nodeType === 3).map(n => n.textContent).join('')
        || (el.childElementCount === 0 ? (el.textContent || '') : '')
        || el.getAttribute('aria-label') || ''
      );
      const out = [];
      for (const el of document.querySelectorAll(
        'div, span, label, li, [role=radio], [role=option], [role=menuitemradio]'
      )) {
        if (!viz(el) || (own(el) !== 'website' && own(el) !== 'sitio web')) continue;
        const r = el.getBoundingClientRect();
        if (r.width < 4 || r.height < 4 || r.height > 80) continue;
        out.push({
          tag: el.tagName,
          role: el.getAttribute('role') || '',
          aria_label: el.getAttribute('aria-label') || '',
          aria_checked: el.getAttribute('aria-checked') || '',
          own_text: own(el),
          full_text: norm(el.textContent || '').slice(0, 80),
          x: Math.round(r.x), y: Math.round(r.y),
          w: Math.round(r.width), h: Math.round(r.height),
          playwright_hints: [
            "get_by_role('radio', name=re.compile(r'^Website$'))",
            "get_by_text('Website', exact=True)",
            f"mouse.click({Math.round(r.x + 18)}, {Math.round(r.y + r.height/2)})  # radio",
          ],
        });
      }
      out.sort((a, b) => (a.h - 36)**2 - (b.h - 36)**2);
      return out[0] || null;
    }""")
    selected, menu = _verify(page)
    with _lock:
        _state.golden_locator = hint
    return StepResult(
        "E",
        bool(hint),
        "captured golden locator" if hint else "Website DOM node not found",
        website_selected=selected,
        menu_open=menu,
        locator_hint=str(hint.get("playwright_hints") if hint else None),
        bbox=hint,
    )


_STEP_FNS: dict[str, Callable[[Page], StepResult]] = {
    "A": _step_A,
    "B": _step_B,
    "F": _step_F,
    "C": _step_C,
    "D": _step_D,
    "E": _step_E,
}


def run_step(step: str) -> dict[str, Any]:
    step = (step or "").strip().upper()
    if step not in _STEP_FNS:
        return {"ok": False, "error": f"Unknown step {step!r}. Use A–F."}

    if not _state.connected or _page is None:
        conn = connect()
        if not conn.get("ok") and not _state.connected:
            return conn

    page = _page
    assert page is not None

    # Re-prepare menu if Website not selected and option not visible
    if step != "E" and not is_website_conversion_selected(page):
        if not _is_website_option_visible(page):
            prep = _ensure_menu_ready(page)
            if prep == "website_not_visible" and step != "D":
                return {
                    "ok": False,
                    "error": "Website option not visible — open Conversion location → Single first",
                    **get_status(),
                }

    with _lock:
        _state.current_step = step
        _state.message = f"Running step {step}: {STEP_META[step]['title']}"
        _state.error = None

    try:
        result = _STEP_FNS[step](page)
        result.ts = datetime.now().isoformat(timespec="seconds")
        result.screenshot = _shot(f"step_{step}")
        # Final verify
        selected, menu = _verify(page)
        result.website_selected = selected
        result.menu_open = menu
        if step != "E":
            result.ok = selected

        entry = {
            "step": result.step,
            "title": STEP_META[step]["title"],
            "ok": result.ok,
            "detail": result.detail,
            "website_selected": result.website_selected,
            "menu_open": result.menu_open,
            "bbox": result.bbox,
            "locator_hint": result.locator_hint,
            "screenshot": result.screenshot,
            "ts": result.ts,
        }
        with _lock:
            _state.steps.append(entry)
            _state.website_selected = selected
            _state.menu_open = menu
            _state.current_step = None
            if selected and step != "E":
                _state.winner = step
                _state.message = f"✓ Step {step} WORKED — Website selected"
            elif step == "E" and result.ok:
                _state.message = f"Step E captured locator (see golden_locator)"
            else:
                _state.message = f"Step {step} did not select Website — try next"

        return get_status() | {"ok": True, "step_result": entry}
    except Exception as exc:
        with _lock:
            _state.error = str(exc)
            _state.current_step = None
            _state.message = f"Step {step} error: {exc}"
        return get_status() | {"ok": False, "error": str(exc)}


def _run_all_loop(stop_on_success: bool = True, cdp_url: str | None = None) -> None:
    """Owns Playwright on this thread (required — sync API is not cross-thread)."""
    global _pw, _browser, _page
    try:
        # Fresh connection on this worker thread
        disconnect(clear_running=False)
        conn = connect(cdp_url=cdp_url)
        if not conn.get("ok") and not _state.connected:
            with _lock:
                _state.running = False
                _state.error = conn.get("error") or "connect failed"
                _state.message = _state.error or "connect failed"
            return

        prepare()
        if _state.website_selected:
            with _lock:
                _state.running = False
                _state.message = "Already Website — nothing to do"
            return

        for step in STEP_ORDER:
            with _lock:
                if not _state.running:
                    break
            result = run_step(step)
            if stop_on_success and result.get("step_result", {}).get("website_selected"):
                with _lock:
                    _state.running = False
                    _state.message = f"✓ Winner: step {step}"
                return
            time.sleep(0.6)

        with _lock:
            _state.running = False
            if not _state.winner:
                _state.message = (
                    "All steps finished — Website still not selected. "
                    "Use Record clicks + manual Website click."
                )
    except Exception as exc:
        with _lock:
            _state.running = False
            _state.error = str(exc)
            _state.message = f"run-all failed: {exc}"


def run_all(*, stop_on_success: bool = True, cdp_url: str | None = None) -> dict[str, Any]:
    global _thread
    with _lock:
        if _state.running:
            return get_status() | {"ok": False, "error": "Lab already running"}
        _state.running = True
        _state.steps = []
        _state.winner = None
        _state.message = "Starting live lab A→B→F→C→D→E…"

    _thread = threading.Thread(
        target=_run_all_loop,
        kwargs={"stop_on_success": stop_on_success, "cdp_url": cdp_url},
        daemon=True,
    )
    _thread.start()
    time.sleep(0.4)
    return get_status() | {"ok": True}
