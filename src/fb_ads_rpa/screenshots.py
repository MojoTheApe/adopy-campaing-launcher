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
    """Fast screenshot via CDP — avoids Playwright font/layout waits on heavy Meta pages."""
    try:
        session = page.context.new_cdp_session(page)
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
        return False


def capture(page: Page, label: str, *, base_dir: str | Path = "output/rpa_screenshots") -> str:
    root = screenshot_dir(base_dir)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)[:60]
    path = root / f"{stamp}_{safe}.png"
    # Prefer CDP first — Meta Ads Manager regularly times out Playwright screenshots
    if _capture_via_cdp(page, path):
        return str(path)
    try:
        page.screenshot(
            path=str(path),
            full_page=False,
            timeout=5000,
            animations="disabled",
            caret="hide",
        )
    except Exception as exc:
        log.warning("Screenshot skipped for %s (%s)", label, exc)
    return str(path)
