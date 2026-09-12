"""Human-like delays between RPA steps + human takeover detection."""
from __future__ import annotations

import random
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.sync_api import Page

# Global flag — set to True to pause the RPA; set to False to resume.
_HUMAN_TAKEOVER: bool = False
_TAKEOVER_POLL_S: float = 0.25

# Teach-mode: RPA pauses on a specific step (e.g. Website click failed),
# user does it manually, then RPA resumes and captures the successful state.
_TEACH_WAITING: bool = False    # RPA is waiting for human action
_TEACH_STEP: str = ""           # e.g. "website_click"
_TEACH_RESULT: dict = {}        # captured data written back by RPA after resume


def request_takeover() -> None:
    """Signal from outside that a human wants to take control."""
    global _HUMAN_TAKEOVER
    _HUMAN_TAKEOVER = True


def release_takeover() -> None:
    """Resume RPA after human is done."""
    global _HUMAN_TAKEOVER
    _HUMAN_TAKEOVER = False


def is_takeover_active() -> bool:
    return _HUMAN_TAKEOVER


# ── Teach-mode helpers ─────────────────────────────────────────────────────────

def begin_teach_wait(step: str) -> None:
    """Called by RPA when it needs the human to act (e.g. Website click).

    `_HUMAN_TAKEOVER` belongs in the `global` list below. Without it the
    assignment made a local variable and the module flag never changed: the
    comment promised that pacing checks would pause, and they did not. RPA
    asked the human to click and kept acting in the same browser.
    """
    global _TEACH_WAITING, _TEACH_STEP, _TEACH_RESULT, _HUMAN_TAKEOVER
    _TEACH_WAITING = True
    _TEACH_STEP = step
    _TEACH_RESULT = {}
    _HUMAN_TAKEOVER = True  # also set takeover so pacing checks pause


def end_teach_wait(captured: dict | None = None) -> None:
    """Called externally (API) or by RPA after human has acted."""
    global _TEACH_WAITING, _TEACH_STEP, _TEACH_RESULT, _HUMAN_TAKEOVER
    _TEACH_WAITING = False
    _TEACH_STEP = ""
    _TEACH_RESULT = captured or {}
    _HUMAN_TAKEOVER = False


def get_teach_state() -> dict:
    return {
        "waiting": _TEACH_WAITING,
        "step": _TEACH_STEP,
        "result": _TEACH_RESULT,
    }


def wait_for_teach_resume(max_wait_s: float = 600.0) -> bool:
    """Block the RPA thread until the human resumes (or timeout)."""
    import time as _t
    deadline = _t.time() + max_wait_s
    while _t.time() < deadline:
        _t.sleep(0.5)
        if not _TEACH_WAITING:
            return True
    end_teach_wait()
    return False


def _get_mouse_pos(page: "Page") -> tuple[float, float] | None:
    """Return current mouse position via CDP."""
    try:
        pos = page.evaluate("""() => {
            return window._rpa_last_mouse || null;
        }""")
        if pos and isinstance(pos, dict):
            return (pos.get("x", 0), pos.get("y", 0))
    except Exception:
        pass
    return None


def _install_mouse_tracker(page: "Page") -> None:
    """Inject a JS listener that records mouse moves — one-time setup."""
    try:
        already = page.evaluate("() => !!window._rpa_mouse_tracker_installed")
        if already:
            return
        page.evaluate("""() => {
            window._rpa_last_mouse = {x: -1, y: -1};
            window._rpa_mouse_moved = false;
            window._rpa_mouse_tracker_installed = true;
            document.addEventListener('mousemove', function(e) {
                window._rpa_last_mouse = {x: e.clientX, y: e.clientY};
                window._rpa_mouse_moved = true;
            }, {passive: true});
            document.addEventListener('keydown', function(e) {
                // Ignore modifier-only presses (Shift, Ctrl, Alt, Meta)
                if (!['Shift','Control','Alt','Meta'].includes(e.key)) {
                    window._rpa_mouse_moved = true;
                }
            }, {passive: true});
        }""")
    except Exception:
        pass


def check_human_takeover(page: "Page | None" = None) -> bool:
    """Return True if a human seems to be active (flag set or mouse moved).

    If True, the RPA should pause and wait until released.
    """
    if _HUMAN_TAKEOVER:
        return True
    if page is None:
        return False
    try:
        moved = page.evaluate("() => { const v = window._rpa_mouse_moved; window._rpa_mouse_moved = false; return !!v; }")
        if moved:
            return True
    except Exception:
        pass
    return False


def wait_for_human_release(page: "Page | None" = None, *, max_wait_s: float = 300.0) -> bool:
    """Block until human releases control (or timeout).

    Returns True if released, False if timed out.
    """
    deadline = time.time() + max_wait_s
    print("[RPA] ⏸ Human takeover detected — pausing. Move mouse away or call release_takeover().")
    while time.time() < deadline:
        time.sleep(_TAKEOVER_POLL_S)
        # Reset mouse-moved flag and wait a bit to see if it moves again
        if page:
            try:
                page.evaluate("() => { window._rpa_mouse_moved = false; }")
            except Exception:
                pass
        time.sleep(0.5)
        if not _HUMAN_TAKEOVER:
            if page:
                try:
                    moved = page.evaluate("() => !!window._rpa_mouse_moved")
                    if moved:
                        continue  # still moving
                except Exception:
                    pass
            print("[RPA] ▶ Resuming after human pause.")
            return True
    print("[RPA] ⚠ Human takeover timed out — aborting.")
    return False


def pause_short(page: "Page | None" = None) -> None:
    if check_human_takeover(page):
        wait_for_human_release(page)
    time.sleep(random.uniform(0.4, 0.9))


def pause_medium(page: "Page | None" = None) -> None:
    if check_human_takeover(page):
        wait_for_human_release(page)
    time.sleep(random.uniform(1.0, 2.2))


def pause_long(page: "Page | None" = None) -> None:
    if check_human_takeover(page):
        wait_for_human_release(page)
    time.sleep(random.uniform(2.5, 4.5))


def pause_after_navigation(page: "Page | None" = None) -> None:
    if check_human_takeover(page):
        wait_for_human_release(page)
    time.sleep(random.uniform(2.0, 3.5))
