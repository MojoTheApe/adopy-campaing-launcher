"""Screenshot helpers for RPA debugging."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import Page


def screenshot_dir(base: str | Path = "output/rpa_screenshots") -> Path:
    p = Path(base)
    p.mkdir(parents=True, exist_ok=True)
    return p


def capture(page: Page, label: str, *, base_dir: str | Path = "output/rpa_screenshots") -> str:
    root = screenshot_dir(base_dir)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)[:60]
    path = root / f"{stamp}_{safe}.png"
    page.screenshot(path=str(path), full_page=False)
    return str(path)
