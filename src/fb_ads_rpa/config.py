"""RPA publish configuration from environment."""
from __future__ import annotations

import os


def get_publish_backend() -> str:
    """rpa (default) = Playwright Ads Manager; api = legacy Marketing API."""
    return (os.environ.get("FB_PUBLISH_BACKEND") or "rpa").strip().lower()


def get_browser_provider() -> str:
    """multilogin (default) or dolphin."""
    return (os.environ.get("FB_BROWSER_PROVIDER") or "multilogin").strip().lower()


def get_dolphin_api_base() -> str:
    return (os.environ.get("DOLPHIN_API_BASE") or "http://127.0.0.1:3001").strip().rstrip("/")


def get_dolphin_api_token() -> str:
    return (os.environ.get("DOLPHIN_API_TOKEN") or "").strip()


def use_rpa_publish() -> bool:
    return get_publish_backend() != "api"
