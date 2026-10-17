"""Human-like delays between RPA steps + human takeover detection."""
from __future__ import annotations

import os
import random
import time
from typing import TYPE_CHECKING

from src.fb_ads_rpa import job_cancel

if TYPE_CHECKING:
    from playwright.sync_api import Page

# Global flag — set to True to pause the RPA; set to False to resume. This is the
# dashboard's manual Pause button: it stops every job.
_HUMAN_TAKEOVER: bool = False
_TAKEOVER_POLL_S: float = 0.25

# Teach-mode (ACL-1142): RPA pauses on a specific step (e.g. Website click
# failed), the user does it manually, then RPA resumes. The state is kept PER
# JOB. With two builds at once a single module-level pair of variables could not
# say whose click was wanted, and "continue" released whichever job was waiting.
# Key = id of the job whose thread asks (job_cancel.current_job_id()); a thread
# outside any job (tests, scripts) uses the key "". An entry exists only while
# that job is waiting, and an entry also means "the human has control" for that
# job alone — the other jobs keep working.
#   {job_id: {"step": "website_click", "since": <epoch s>}}
_TEACH_WAITING_JOBS: dict[str, dict] = {}
# Captured data written back after a resume, kept per job until read.
_TEACH_RESULTS: dict[str, dict] = {}


def _job_key(job_id: str | None = None) -> str:
    if job_id is not None:
        return job_id
    return job_cancel.current_job_id() or ""


def _in_job() -> bool:
    return job_cancel.current_job_id() is not None


def request_takeover() -> None:
    """Signal from outside that a human wants to take control."""
    global _HUMAN_TAKEOVER
    _HUMAN_TAKEOVER = True


def release_takeover() -> None:
    """Resume RPA after human is done."""
    global _HUMAN_TAKEOVER
    _HUMAN_TAKEOVER = False


def is_takeover_active() -> bool:
    """Does the human hold control for the caller?

    A job thread sees the manual Pause button plus ITS OWN teach wait. A caller
    outside any job (the dashboard status endpoint) sees the button plus any
    teach wait.
    """
    if _HUMAN_TAKEOVER:
        return True
    if _in_job():
        return _job_key() in _TEACH_WAITING_JOBS
    return bool(_TEACH_WAITING_JOBS)


# ── Teach-mode helpers ─────────────────────────────────────────────────────────

def begin_teach_wait(step: str) -> None:
    """Called by RPA when it needs the human to act (e.g. Website click).

    Registers the wait for the calling job only. The entry doubles as the
    takeover for that job, so its pacing checks pause (see `is_takeover_active`).
    """
    key = _job_key()
    _TEACH_WAITING_JOBS[key] = {"step": step, "since": time.time()}
    _TEACH_RESULTS.pop(key, None)


def end_teach_wait(captured: dict | None = None, job_id: str | None = None) -> None:
    """Called externally (API, with `job_id`) or by RPA after human has acted.

    Releases exactly one job: `job_id` if given, else the calling job.
    """
    key = _job_key(job_id)
    _TEACH_WAITING_JOBS.pop(key, None)
    _TEACH_RESULTS[key] = captured or {}


def get_teach_state(job_id: str | None = None) -> dict:
    """Teach state of one job (`job_id`, or the calling job).

    A caller outside any job that names no job gets the dashboard-wide view:
    `waiting` if any job waits, `step` of the longest-waiting one, and `jobs`
    listing every waiting job_id with its step.
    """
    if job_id is None and not _in_job():
        waiting = sorted(_TEACH_WAITING_JOBS.items(), key=lambda kv: kv[1]["since"])
        first = waiting[0] if waiting else None
        return {
            "waiting": bool(waiting),
            "step": first[1]["step"] if first else "",
            "result": _TEACH_RESULTS.get(first[0] if first else "", {}),
            "jobs": [{"job_id": k, "step": v["step"]} for k, v in waiting],
        }
    key = _job_key(job_id)
    entry = _TEACH_WAITING_JOBS.get(key)
    return {
        "waiting": entry is not None,
        "step": entry["step"] if entry else "",
        "result": _TEACH_RESULTS.get(key, {}),
    }


def wait_for_teach_resume(max_wait_s: float = 600.0) -> bool:
    """Block the calling job's thread until the human resumes it (or timeout)."""
    import time as _t
    key = _job_key()
    deadline = _t.time() + max_wait_s
    while _t.time() < deadline:
        _t.sleep(0.5)
        job_cancel.check()
        job_cancel.heartbeat()   # тихое ожидание человека — не зависание
        if key not in _TEACH_WAITING_JOBS:
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
    if is_takeover_active():
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
        job_cancel.check()
        job_cancel.heartbeat()   # тихое ожидание человека — не зависание
        if not is_takeover_active():
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


# 21.09, решение пользователя: паузы между действиями были заметно длиннее
# нужного — «All edits saved» уже на экране, а робот всё ещё выжидает. Живость
# ввода держится на разбросе и на ритме набора, а не на длине пауз, поэтому
# крупные паузы укорочены примерно вдвое. Прежние вернёт FB_RPA_SLOW_PACING=1.
def _slow() -> bool:
    return os.environ.get("FB_RPA_SLOW_PACING", "").strip() in ("1", "true", "yes")


def _stop_if_tab_hung() -> None:
    """ACL-1702 (ревью PR #704, B2): вкладку закрыл сторож — не нажимать дальше.

    До `check_human_takeover`: тот спрашивает страницу. Отметки нет — одно
    чтение dict; есть — `meta_dom.FlowBudgetExceeded("tab_hung")` внутри
    потока кампании.
    """
    from src.fb_ads_rpa import tab_watchdog

    if tab_watchdog.peek_hung():
        from src.fb_ads_rpa import meta_dom

        meta_dom._flow_budget_hung_check()


def pause_short(page: "Page | None" = None) -> None:
    _stop_if_tab_hung()
    if check_human_takeover(page):
        wait_for_human_release(page)
    time.sleep(random.uniform(0.4, 0.9) if _slow() else random.uniform(0.25, 0.6))
    job_cancel.check()


def pause_medium(page: "Page | None" = None) -> None:
    _stop_if_tab_hung()
    if check_human_takeover(page):
        wait_for_human_release(page)
    time.sleep(random.uniform(1.0, 2.2) if _slow() else random.uniform(0.6, 1.3))
    job_cancel.check()


def pause_long(page: "Page | None" = None) -> None:
    _stop_if_tab_hung()
    if check_human_takeover(page):
        wait_for_human_release(page)
    time.sleep(random.uniform(2.5, 4.5) if _slow() else random.uniform(1.0, 1.8))
    job_cancel.check()


def pause_after_navigation(page: "Page | None" = None) -> None:
    _stop_if_tab_hung()
    if check_human_takeover(page):
        wait_for_human_release(page)
    time.sleep(random.uniform(2.0, 3.5) if _slow() else random.uniform(1.0, 1.7))
    job_cancel.check()
