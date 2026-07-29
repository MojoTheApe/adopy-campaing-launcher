"""Screenshot helpers for RPA debugging."""
from __future__ import annotations

import base64
import logging
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import Page

log = logging.getLogger(__name__)


def screenshot_dir(base: str | Path = "output/rpa_screenshots") -> Path:
    p = Path(base)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _capture_via_cdp(page: Page, path: Path) -> bool:
    """Screenshot via CDP. LAST resort — see capture() for why.

    Reuses one CDP session per page instead of opening a new one per shot
    (this runs ~100 times per campaign).
    """
    try:
        session = getattr(page, "_rpa_shot_cdp", None)
        if session is None:
            session = page.context.new_cdp_session(page)
            try:
                page._rpa_shot_cdp = session  # type: ignore[attr-defined]
            except Exception:
                pass
        # captureBeyondViewport=False avoids tiled/glitched images on huge MLX viewports
        result = session.send(
            "Page.captureScreenshot",
            {"format": "png", "fromSurface": True, "captureBeyondViewport": False},
        )
        data = result.get("data")
        if not data:
            return False
        path.write_bytes(base64.b64decode(data))
        return True
    except Exception as exc:
        log.debug("CDP screenshot failed: %s", exc)
        try:
            page._rpa_shot_cdp = None  # type: ignore[attr-defined]
        except Exception:
            pass
        return False


def capture(page: Page, label: str, *, base_dir: str | Path = "output/rpa_screenshots") -> str:
    root = screenshot_dir(base_dir)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)[:60]
    path = root / f"{stamp}_{safe}.png"

    # Playwright FIRST, CDP only as a fallback.
    #
    # This order looks backwards — Meta's editor does make Playwright
    # screenshots time out on font/layout waits, which is why CDP used to
    # come first. But the two failure modes are not comparable: a
    # Playwright timeout is BOUNDED (it raises after `timeout` and the flow
    # continues), whereas sync Playwright's CDPSession.send() takes no
    # timeout at all — if the browser never answers Page.captureScreenshot
    # the call blocks FOREVER. Confirmed live 2026-07-29: a dashboard run
    # froze for 11+ minutes right after `after_destination_sweep` with the
    # RPA thread blocked and zero CPU, while a fresh CDP session answered
    # the same page in 0.2s. A debug screenshot must never be able to hang
    # the whole run, so the bounded path goes first and the unbounded one
    # only runs when the bounded one already failed.
    try:
        page.screenshot(
            path=str(path),
            full_page=False,
            timeout=5000,
            animations="disabled",
            caret="hide",
        )
        return str(path)
    except Exception as exc:
        log.debug("Playwright screenshot failed for %s (%s); trying CDP", label, exc)

    if not _capture_via_cdp(page, path):
        log.warning("Screenshot skipped for %s (both paths failed)", label)
    return str(path)
