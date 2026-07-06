"""Playwright browser session via antidetect profile (Multilogin / Dolphin)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from playwright.sync_api import Browser, Page, Playwright, sync_playwright

from src.meta_campaign_agent.ad_account_map import AccountMapEntry


@dataclass
class BrowserConnection:
    provider: str
    page: Page
    browser: Browser
    playwright: Playwright
    debug_port: str | None = None
    ws_endpoint: str | None = None

    def close(self) -> None:
        try:
            self.browser.close()
        except Exception:
            pass
        try:
            self.playwright.stop()
        except Exception:
            pass


class BrowserProvider(Protocol):
    def open(self, entry: AccountMapEntry, account_label: str) -> BrowserConnection: ...

    def close(self, entry: AccountMapEntry) -> None: ...


def _connect_playwright(*, cdp_url: str, provider: str, debug_port: str | None, ws_endpoint: str | None) -> BrowserConnection:
    pw = sync_playwright().start()
    browser = pw.chromium.connect_over_cdp(cdp_url)
    if browser.contexts:
        context = browser.contexts[0]
        page = context.pages[0] if context.pages else context.new_page()
    else:
        context = browser.new_context()
        page = context.new_page()
    return BrowserConnection(
        provider=provider,
        page=page,
        browser=browser,
        playwright=pw,
        debug_port=debug_port,
        ws_endpoint=ws_endpoint,
    )


class MultiloginBrowserProvider:
    """Start Multilogin profile with automation_type=playwright, connect via CDP."""

    def __init__(self, *, headless: bool = False):
        self.headless = headless
        self._started_profile_id: str | None = None
        self._launcher: Any = None

    def open(self, entry: AccountMapEntry, account_label: str) -> BrowserConnection:
        from src.meta_campaign_agent.multilogin_config import get_multilogin_instance
        from src.meta_campaign_agent.multilogin_launcher import MultiloginLauncher, MultiloginLauncherError

        if not entry.folder_id or not entry.profile_id:
            raise MultiloginLauncherError(f"{account_label}: folder_id and profile_id required")

        ml = get_multilogin_instance(entry.multilogin_instance)
        self._launcher = MultiloginLauncher(ml)
        started = self._launcher.start_profile(entry.folder_id, entry.profile_id, headless=self.headless)
        self._started_profile_id = started.profile_id

        if not started.port:
            self.close(entry)
            raise MultiloginLauncherError("Multilogin start did not return debug port")

        ws = started.ws_endpoint or ""
        if ws and not ws.startswith("/"):
            ws = f"/{ws}"
        cdp = f"http://127.0.0.1:{started.port}{ws}"

        try:
            return _connect_playwright(
                cdp_url=cdp,
                provider="multilogin",
                debug_port=started.port,
                ws_endpoint=started.ws_endpoint,
            )
        except Exception:
            self.close(entry)
            raise

    def close(self, entry: AccountMapEntry) -> None:
        if self._launcher and self._started_profile_id:
            try:
                self._launcher.stop_profile(self._started_profile_id)
            except Exception:
                pass
        self._started_profile_id = None


class DolphinBrowserProvider:
    """Start Dolphin{anty} profile via local API, connect Playwright over CDP."""

    def __init__(self, *, api_base: str | None = None, api_token: str | None = None):
        from src.fb_ads_rpa.config import get_dolphin_api_base, get_dolphin_api_token

        self.api_base = (api_base or get_dolphin_api_base()).rstrip("/")
        self.api_token = api_token if api_token is not None else get_dolphin_api_token()
        self._profile_id: str | None = None

    def _headers(self) -> dict[str, str]:
        h = {"Accept": "application/json"}
        if self.api_token:
            h["Authorization"] = f"Bearer {self.api_token}"
        return h

    def open(self, entry: AccountMapEntry, account_label: str) -> BrowserConnection:
        import requests

        profile_id = entry.profile_id
        if not profile_id:
            raise RuntimeError(f"{account_label}: profile_id required for Dolphin")

        url = f"{self.api_base}/v1.0/browser_profiles/{profile_id}/start?automation=1"
        r = requests.get(url, headers=self._headers(), timeout=120)
        body = r.json() if r.content else {}
        if r.status_code >= 400 or not body.get("success", True):
            raise RuntimeError(f"Dolphin start failed HTTP {r.status_code}: {body}")

        automation = body.get("automation") or {}
        port = str(automation.get("port") or "")
        ws = automation.get("wsEndpoint") or automation.get("ws_endpoint") or ""
        if not port:
            raise RuntimeError(f"Dolphin start missing automation port: {body}")

        self._profile_id = profile_id
        if ws:
            cdp = f"http://127.0.0.1:{port}{ws}" if ws.startswith("/") else ws
        else:
            cdp = f"http://127.0.0.1:{port}"

        return _connect_playwright(cdp_url=cdp, provider="dolphin", debug_port=port, ws_endpoint=ws or None)

    def close(self, entry: AccountMapEntry) -> None:
        if not self._profile_id:
            return
        import requests

        url = f"{self.api_base}/v1.0/browser_profiles/{self._profile_id}/stop"
        try:
            requests.get(url, headers=self._headers(), timeout=60)
        except Exception:
            pass
        self._profile_id = None


def get_browser_provider(name: str | None = None) -> BrowserProvider:
    from src.fb_ads_rpa.config import get_browser_provider as _default_name

    key = (name or _default_name()).strip().lower()
    if key == "dolphin":
        return DolphinBrowserProvider()
    if key == "multilogin":
        return MultiloginBrowserProvider()
    raise ValueError(f"Unknown browser provider {key!r} — use multilogin or dolphin")
