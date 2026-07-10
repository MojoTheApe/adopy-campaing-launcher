"""Playwright browser session via antidetect profile (Multilogin / Dolphin)."""
from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
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

    def close(self, *, keep_browser_open: bool = False) -> None:
        """Close or disconnect from the browser.

        If keep_browser_open=True, Playwright disconnects without closing
        the browser window (hand-off mode — user continues manually).
        """
        try:
            if keep_browser_open:
                # disconnect() exists on Browser (CDP) but not on BrowserContext
                if hasattr(self.browser, "disconnect"):
                    self.browser.disconnect()
                # else: persistent context — just leave it open
            else:
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

    def close(self, entry: AccountMapEntry, *, keep_browser_open: bool = False) -> None:
        if keep_browser_open:
            # Hand-off mode: leave the Multilogin profile running so the user
            # can interact with the pre-filled form manually.
            self._started_profile_id = None
            return
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


class MultiloginDirectCDPProvider:
    """Bypass MLX automation API: find the running profile browser, restart it
    with --remote-debugging-port so Playwright can connect via CDP.

    Use when the automation token is expired but the desktop app is running.
    """

    CDP_PORT = 19222  # port we inject for remote debugging

    def __init__(self, *, headless: bool = False):
        self.headless = headless
        self._proc: subprocess.Popen | None = None  # type: ignore[type-arg]
        self._user_data_dir: str | None = None
        self._profile_id_cache: str | None = None

    def _find_profile_user_data_dir(self, profile_id: str) -> Path | None:
        """Search MLX profiles directory for a folder matching profile_id."""
        mlx_home = Path(os.environ.get("MLX_HOME", str(Path.home() / "mlx")))
        profiles_root = mlx_home / "profiles"
        if not profiles_root.is_dir():
            return None
        # Search up to 3 levels deep: profiles/<workspace>/<sub>/<profile_id>
        for p in profiles_root.rglob(profile_id):
            if p.is_dir():
                return p
        return None

    def _find_mlx_chrome(self) -> Path | None:
        """Find the MLX Mimic chrome.exe."""
        mlx_home = Path(os.environ.get("MLX_HOME", str(Path.home() / "mlx")))
        deps = mlx_home / "deps"
        if deps.is_dir():
            for chrome in sorted(deps.glob("mimic_*/chrome.exe"), reverse=True):
                if chrome.is_file():
                    return chrome
        return None

    def _kill_profile_browser(self, user_data_dir: str, profile_id: str = "") -> None:
        """Kill all MLX chrome.exe processes that own this user-data-dir.

        Chrome stores the path as 8.3 short names on Windows, so we match
        against the last segment (profile_id UUID) and the mlx/profiles substring.
        We also match against --client-session-id which always uses the full UUID.
        """
        import psutil  # type: ignore

        # Build multiple needles to catch both long-path and 8.3-path representations
        needles: list[str] = []
        udd_norm = user_data_dir.lower().replace("/", "\\")
        needles.append(udd_norm)

        # Last path segment = profile_id (UUID), present as-is in --user-data-dir
        last = udd_norm.rstrip("\\").rsplit("\\", 1)[-1]  # e.g. "a89cdc8a-..."
        if last:
            needles.append(last)  # matches both long and 8.3 forms

        # MLX also passes --client-session-id=<full UUID> — always full form
        if profile_id:
            needles.append(profile_id.lower())

        # Only kill mimic (MLX) chrome processes, not all Chrome instances
        mlx_home_norm = str(Path.home() / "mlx").lower().replace("/", "\\")

        for proc in psutil.process_iter(["pid", "name", "exe", "cmdline"]):
            try:
                if not (proc.info["name"] and "chrome" in proc.info["name"].lower()):
                    continue
                exe = (proc.info["exe"] or "").lower().replace("/", "\\")
                # Only target MLX mimic browser, not other Chromium instances
                if "mimic" not in exe and mlx_home_norm not in exe:
                    continue
                cmd = " ".join(proc.info["cmdline"] or []).lower().replace("/", "\\")
                if any(n in cmd for n in needles):
                    proc.kill()
            except Exception:
                pass

    def open(self, entry: AccountMapEntry, account_label: str) -> "BrowserConnection":
        profile_id = entry.profile_id
        if not profile_id:
            raise RuntimeError(f"{account_label}: profile_id required for DirectCDP provider")
        self._profile_id_cache = profile_id

        user_data_dir = self._find_profile_user_data_dir(profile_id)
        if user_data_dir is None:
            raise RuntimeError(
                f"Could not find MLX profile directory for profile_id={profile_id}. "
                "Check that MLX_HOME is set correctly in .env"
            )
        self._user_data_dir = str(user_data_dir)

        chrome = self._find_mlx_chrome()
        if chrome is None:
            raise RuntimeError("MLX Mimic browser (chrome.exe) not found under mlx/deps/")

        # Stop any running instance of this profile
        self._kill_profile_browser(self._user_data_dir, profile_id)
        time.sleep(2)  # let Chrome release the profile lock

        # Build the launch command — minimal flags + remote debugging port.
        # --disable-extensions: prevents the MLX Agent extension from trying
        # to authenticate and crashing the browser when the token is expired.
        cmd = [
            str(chrome),
            f"--user-data-dir={self._user_data_dir}",
            f"--remote-debugging-port={self.CDP_PORT}",
            "--remote-allow-origins=*",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-popup-blocking",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-extensions",
            "--disable-background-mode",
        ]
        if self.headless:
            cmd.append("--headless=new")

        log_dir = Path(self._user_data_dir).parent / "cdp_logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = open(log_dir / "chrome_stderr.txt", "w", encoding="utf-8")
        self._proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=log_file)
        # Poll until CDP port is accepting connections (up to 15 s)
        import socket
        deadline = time.time() + 15
        while time.time() < deadline:
            time.sleep(0.5)
            try:
                with socket.create_connection(("127.0.0.1", self.CDP_PORT), timeout=1):
                    break
            except OSError:
                continue
        else:
            # Capture any startup error from the log
            try:
                log_file.flush()
                err_snippet = (log_dir / "chrome_stderr.txt").read_text(encoding="utf-8", errors="replace")[-600:]
            except Exception:
                err_snippet = "(no log)"
            raise RuntimeError(
                f"CDP port {self.CDP_PORT} did not open after 15 s. "
                f"Chrome stderr tail:\n{err_snippet}"
            )

        cdp_url = f"http://127.0.0.1:{self.CDP_PORT}"
        conn = _connect_playwright(
            cdp_url=cdp_url,
            provider="multilogin_direct",
            debug_port=str(self.CDP_PORT),
            ws_endpoint=None,
        )
        # Mimic exits shortly after start when not launched via MLX launcher
        # (stderr: "Mimic: initial check failed"). Fail fast so caller can fallback.
        time.sleep(2)
        if self._proc and self._proc.poll() is not None:
            try:
                log_file.flush()
                err_snippet = (log_dir / "chrome_stderr.txt").read_text(encoding="utf-8", errors="replace")[-600:]
            except Exception:
                err_snippet = "(no log)"
            raise RuntimeError(
                "Mimic browser exited after start (license check failed). "
                "Launch the profile via MLX Desktop App or use PlaywrightProfileProvider. "
                f"stderr tail:\n{err_snippet}"
            )
        try:
            if hasattr(conn.browser, "is_connected") and not conn.browser.is_connected():
                raise RuntimeError("Mimic browser disconnected immediately after CDP attach")
        except Exception as exc:
            if "disconnected" in str(exc).lower():
                raise
        return conn

    def close(self, entry: AccountMapEntry, *, keep_browser_open: bool = False) -> None:
        if keep_browser_open:
            # Hand-off: leave browser running for user
            self._proc = None
            return
        if self._proc:
            try:
                self._proc.terminate()
            except Exception:
                pass
            self._proc = None
        if self._user_data_dir:
            self._kill_profile_browser(self._user_data_dir)
        self._user_data_dir = None
        self._profile_id_cache = None


class PlaywrightProfileProvider:
    """Launch Playwright's own Chromium with the MLX profile directory.

    Bypasses MLX Mimic entirely — avoids the license-check crash.
    Facebook cookies are preserved because we reuse the same user-data-dir.
    Works even when the MLX Desktop App is logged out.
    """

    def __init__(self, *, headless: bool = False):
        self.headless = headless
        self._context = None  # BrowserContext (persistent)
        self._playwright = None
        self._user_data_dir: str | None = None

    def _find_profile_user_data_dir(self, profile_id: str) -> Path | None:
        mlx_home = Path(os.environ.get("MLX_HOME", str(Path.home() / "mlx")))
        profiles_root = mlx_home / "profiles"
        if not profiles_root.is_dir():
            return None
        for p in profiles_root.rglob(profile_id):
            if p.is_dir():
                return p
        return None

    def _kill_profile_browser(self, user_data_dir: str, profile_id: str = "") -> None:
        """Kill any running browser that holds a lock on this profile directory."""
        import psutil  # type: ignore

        needles: list[str] = []
        udd_norm = user_data_dir.lower().replace("/", "\\")
        needles.append(udd_norm)
        last = udd_norm.rstrip("\\").rsplit("\\", 1)[-1]
        if last:
            needles.append(last)
        if profile_id:
            needles.append(profile_id.lower())

        mlx_home_norm = str(Path.home() / "mlx").lower().replace("/", "\\")

        for proc in psutil.process_iter(["pid", "name", "exe", "cmdline"]):
            try:
                if not (proc.info["name"] and "chrome" in proc.info["name"].lower()):
                    continue
                exe = (proc.info["exe"] or "").lower().replace("/", "\\")
                if "mimic" not in exe and mlx_home_norm not in exe:
                    continue
                cmd = " ".join(proc.info["cmdline"] or []).lower().replace("/", "\\")
                if any(n in cmd for n in needles):
                    proc.kill()
            except Exception:
                pass

    def open(self, entry: AccountMapEntry, account_label: str) -> BrowserConnection:
        profile_id = entry.profile_id
        if not profile_id:
            raise RuntimeError(f"{account_label}: profile_id required for PlaywrightProfile provider")

        user_data_dir = self._find_profile_user_data_dir(profile_id)
        if user_data_dir is None:
            raise RuntimeError(
                f"MLX profile directory not found for profile_id={profile_id}"
            )
        self._user_data_dir = str(user_data_dir)

        # Kill any running MLX browser for this profile (releases profile lock)
        self._kill_profile_browser(self._user_data_dir, profile_id)
        time.sleep(2)

        self._playwright = sync_playwright().start()
        # launch_persistent_context opens Playwright's own Chromium with existing profile data
        context = self._playwright.chromium.launch_persistent_context(
            user_data_dir=self._user_data_dir,
            headless=self.headless,
            args=[
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-popup-blocking",
                "--disable-extensions",
            ],
        )
        self._context = context
        page = context.pages[0] if context.pages else context.new_page()
        # BrowserConnection expects a Browser but we have a BrowserContext — wrap it
        return BrowserConnection(
            provider="playwright_profile",
            page=page,
            browser=context,  # type: ignore[arg-type]
            playwright=self._playwright,
            debug_port=None,
            ws_endpoint=None,
        )

    def close(self, entry: AccountMapEntry, *, keep_browser_open: bool = False) -> None:
        if keep_browser_open:
            self._context = None
            self._playwright = None
            return
        if self._context:
            try:
                self._context.close()
            except Exception:
                pass
            self._context = None
        if self._playwright:
            try:
                self._playwright.stop()
            except Exception:
                pass
            self._playwright = None
        self._user_data_dir = None


def get_browser_provider(name: str | None = None) -> BrowserProvider:
    from src.fb_ads_rpa.config import get_browser_provider as _default_name

    key = (name or _default_name()).strip().lower()
    if key == "dolphin":
        return DolphinBrowserProvider()
    if key in ("multilogin", "multilogin_api"):
        return MultiloginBrowserProvider()
    if key in ("multilogin_direct", "direct", "direct_cdp"):
        return MultiloginDirectCDPProvider()
    if key in ("playwright_profile", "pw_profile"):
        return PlaywrightProfileProvider()
    raise ValueError(f"Unknown browser provider {key!r} — use multilogin, multilogin_direct, playwright_profile, or dolphin")
