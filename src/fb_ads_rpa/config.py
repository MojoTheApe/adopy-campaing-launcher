"""RPA publish configuration from environment."""
from __future__ import annotations

import os
from pathlib import Path


def ensure_playwright_browsers_path() -> None:
    """Use a real Playwright browser install, not Cursor's empty sandbox cache."""
    default_root = Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright"
    default_has_chromium = any(default_root.glob("chromium-*/chrome-win64/chrome.exe"))

    custom = (os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or "").strip()
    if custom:
        custom_path = Path(custom)
        sandboxish = "cursor-sandbox-cache" in custom.lower()
        has_chromium = any(custom_path.glob("chromium-*/chrome-win64/chrome.exe"))
        if sandboxish or not has_chromium:
            custom = ""

    if custom:
        return

    if default_has_chromium:
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(default_root)
    else:
        os.environ.pop("PLAYWRIGHT_BROWSERS_PATH", None)


def get_publish_backend() -> str:
    """rpa (default) = Playwright Ads Manager; api = legacy Marketing API."""
    return (os.environ.get("FB_PUBLISH_BACKEND") or "rpa").strip().lower()


def get_browser_provider() -> str:
    """multilogin (API), multilogin_direct (Mimic CDP, no token), playwright_profile, dolphin."""
    explicit = (os.environ.get("FB_BROWSER_PROVIDER") or "").strip().lower()
    if explicit:
        return explicit
    token_a = (os.environ.get("MULTILOGIN_AUTOMATION_TOKEN_A") or "").strip()
    if not token_a:
        return "multilogin_direct"
    return "multilogin"


def get_dolphin_api_base() -> str:
    return (os.environ.get("DOLPHIN_API_BASE") or "http://127.0.0.1:3001").strip().rstrip("/")


def get_dolphin_api_token() -> str:
    return (os.environ.get("DOLPHIN_API_TOKEN") or "").strip()


def use_rpa_publish() -> bool:
    return get_publish_backend() != "api"
