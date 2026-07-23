"""Codegen Recorder — open the Playwright Inspector on an already-open
Multilogin browser (via CDP) instead of launching a fresh, separate browser.

Why this exists: `playwright codegen <url>` always launches its own new
Chromium. That's unsafe for this project (a fresh login mid-recording once
leaked a plaintext Facebook password into the recorded file — see
docs/agent10-post-conversion-location-handoff.md) and pointless for
antidetect fingerprinting (Meta would see a brand new "device" instead of
the Multilogin profile's established one).

page.pause() opens the same Inspector/codegen UI on a page reached via
connect_over_cdp() — i.e. on the browser window Multilogin already has open
and already logged in. No second browser, no fresh login screen. See
docs/agent10-codegen-via-multilogin.md for the full design.

Recording this way (a human clicking through the real UI at normal pace) is
also inherently safer for the account than repeated scripted automation
runs — it looks like ordinary manual usage. This module does not add an
anti-detection throttle for that reason; the only cooldown here is to stop
a double-click on the dashboard button from spawning two overlapping
Inspector sessions on the same page.

Usage from dashboard:
    POST /rpa-codegen/start  {cdp_url?: str}
    POST /rpa-codegen/stop
    GET  /rpa-codegen/status
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from playwright.sync_api import sync_playwright

from src.fb_ads_rpa.click_recorder import _find_cdp_url

log = logging.getLogger(__name__)

_MIN_SECONDS_BETWEEN_STARTS = 5.0


@dataclass
class CodegenState:
    running: bool = False
    cdp_url: str | None = None
    started_at: str | None = None
    error: str | None = None
    message: str = ""


_state = CodegenState()
_lock = threading.Lock()
_thread: threading.Thread | None = None
_last_start_ts: float = 0.0
_active_browser: Any = None  # set while the Inspector is open, for stop_codegen()


def get_status() -> dict[str, Any]:
    with _lock:
        return {
            "ok": True,
            "running": _state.running,
            "cdp_url": _state.cdp_url,
            "started_at": _state.started_at,
            "error": _state.error,
            "message": _state.message,
        }


def _codegen_loop(cdp_url: str) -> None:
    global _active_browser
    pw = None
    try:
        pw = sync_playwright().start()
        browser = pw.chromium.connect_over_cdp(cdp_url)
        with _lock:
            _active_browser = browser
        ctx = browser.contexts[0] if browser.contexts else None
        if not ctx or not ctx.pages:
            raise RuntimeError("No open pages in browser — open Ads Manager first")
        page = ctx.pages[0]
        page.bring_to_front()
        with _lock:
            _state.message = "Inspector open — click Record, then click through the UI manually"
        # Blocks until the user closes/resumes the Inspector, or stop_codegen()
        # disconnects the browser from the other thread (raises here, caught
        # below). The Multilogin window itself is never closed by this call.
        page.pause()
    except Exception as exc:
        with _lock:
            _state.error = str(exc)
            _state.message = f"Codegen error: {exc}"
    finally:
        with _lock:
            _active_browser = None
        if pw is not None:
            try:
                pw.stop()
            except Exception:
                pass
        with _lock:
            _state.running = False
            if not _state.error:
                _state.message = "Inspector closed — copy the generated code before starting again"


def start_codegen(*, cdp_url: str | None = None) -> dict[str, Any]:
    global _thread, _last_start_ts
    with _lock:
        if _state.running:
            return {"ok": False, "error": "Codegen already running", **get_status()}
        now = time.time()
        if now - _last_start_ts < _MIN_SECONDS_BETWEEN_STARTS:
            return {"ok": False, "error": "Started a moment ago — wait a few seconds and retry"}

    url = cdp_url or _find_cdp_url()
    if not url:
        return {
            "ok": False,
            "error": "No open browser with CDP found. Open the Multilogin profile first.",
        }

    with _lock:
        _last_start_ts = time.time()
        _state.running = True
        _state.cdp_url = url
        _state.error = None
        _state.started_at = datetime.now().isoformat(timespec="seconds")
        _state.message = "Connecting…"

    _thread = threading.Thread(target=_codegen_loop, args=(url,), daemon=True)
    _thread.start()
    time.sleep(0.8)
    return get_status() | {"ok": _state.error is None}


def stop_codegen() -> dict[str, Any]:
    """Disconnect (not close) the CDP session to unblock page.pause().

    Uses browser.disconnect() rather than browser.close() — for a CDP
    connection, disconnect() ends Playwright's side only and leaves the
    actual Multilogin window open, the same distinction browser.py already
    relies on elsewhere in this codebase (BrowserConnection.close).
    """
    with _lock:
        browser = _active_browser
        if not _state.running or browser is None:
            return {"ok": False, "error": "Codegen is not running", **get_status()}

    try:
        browser.disconnect()
    except Exception as exc:
        log.debug("disconnect() during stop_codegen failed (may already be closing): %s", exc)

    if _thread and _thread.is_alive():
        _thread.join(timeout=5)

    return get_status() | {"ok": True}
