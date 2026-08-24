"""Playwright browser session via antidetect profile (Multilogin / Dolphin)."""
from __future__ import annotations

import logging
import os
import shutil
import stat
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from playwright.sync_api import Browser, Page, Playwright, sync_playwright

from src.meta_campaign_agent.ad_account_map import AccountMapEntry
from src.fb_ads_rpa.config import ensure_playwright_browsers_path

log = logging.getLogger(__name__)

# Keep Playwright persistent-context sessions alive after hand-off so GC does not
# close the Chromium window when the RpaPublisher instance is discarded.
_HANDOFF_PLAYWRIGHT_SESSIONS: list[tuple[Any, Any]] = []

_MLX_OPEN_HINT = (
    "Open this Multilogin profile in the MLX Desktop app, then retry RPA "
    "(provider=multilogin_direct will attach via CDP). "
    "Or refresh MULTILOGIN_AUTOMATION_TOKEN_A. "
    "Do not use Playwright Chromium against an MLX profile — Chrome profile "
    "migration crashes (version downgrade / Access denied on GPU cache)."
)


def _mlx_home() -> Path:
    return Path(os.environ.get("MLX_HOME", str(Path.home() / "mlx")))


def _find_mlx_profile_dir(profile_id: str) -> Path | None:
    profiles_root = _mlx_home() / "profiles"
    if not profiles_root.is_dir():
        return None
    for p in profiles_root.rglob(profile_id):
        if p.is_dir():
            return p
    return None


def _folder_id_from_profile_dir(profile_dir: Path | str | None) -> str | None:
    """Infer MLX folder_id from on-disk path …/profiles/{ws}/{folder_id}/{profile_id}."""
    if not profile_dir:
        return None
    p = Path(profile_dir)
    try:
        folder = p.parent.name
        # UUIDs look like 8-4-4-4-12
        if len(folder) == 36 and folder.count("-") == 4:
            return folder
    except Exception:
        pass
    return None


def _profile_chrome_version(user_data_dir: str | Path) -> str | None:
    """Return Last Version / newest Snapshots folder name, e.g. '149.0.7827.55'."""
    udd = Path(user_data_dir)
    last = udd / "Last Version"
    try:
        if last.is_file():
            text = last.read_text(encoding="utf-8", errors="replace").strip()
            if text:
                return text.splitlines()[0].strip()
    except Exception:
        pass
    snaps = udd / "Snapshots"
    if snaps.is_dir():
        versions = [p.name for p in snaps.iterdir() if p.is_dir() and p.name[:1].isdigit()]
        if versions:
            return sorted(versions, key=lambda v: [int(x) if x.isdigit() else 0 for x in v.split(".")])[-1]
    return None


def _parse_chrome_major_minor(version: str | None) -> tuple[int, int] | None:
    if not version:
        return None
    parts = version.split(".")
    try:
        return int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
    except ValueError:
        return None


def _rmtree_force(path: Path) -> bool:
    """Best-effort delete; on Windows uses robocopy mirror for stubborn dirs."""
    if not path.exists():
        return True

    def _onerror(func, p, _exc_info):  # noqa: ANN001
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except Exception:
            pass

    try:
        shutil.rmtree(path, onerror=_onerror)
    except Exception:
        pass
    if not path.exists():
        return True
    if os.name == "nt":
        empty = path.parent / f"_empty_del_{os.getpid()}"
        try:
            empty.mkdir(exist_ok=True)
            subprocess.run(
                ["robocopy", str(empty), str(path), "/MIR", "/NFL", "/NDL", "/NJH", "/NJS", "/nc", "/ns", "/np"],
                check=False,
                capture_output=True,
            )
            try:
                empty.rmdir()
            except Exception:
                pass
            shutil.rmtree(path, onerror=_onerror)
        except Exception:
            pass
    return not path.exists()


def _cleanup_chrome_migration_leftovers(user_data_dir: str | Path) -> None:
    """Remove failed Chrome downgrade leftovers that poison the next launch."""
    udd = Path(user_data_dir)
    for p in list(udd.glob("*CHROME_DELETE*")):
        log.info("Removing stale Chrome migration dir: %s", p.name)
        _rmtree_force(p)
    # GPU/Shader caches often hold the Access Denied locks during migration.
    for name in ("GPUPersistentCache", "ShaderCache", "GrShaderCache", "GraphiteDawnCache"):
        p = udd / name
        if p.exists():
            _rmtree_force(p)


def _kill_chrome_for_user_data_dir(user_data_dir: str, profile_id: str = "", *, mlx_only: bool = False) -> None:
    """Kill Chrome/Chromium processes that hold this profile directory."""
    import psutil  # type: ignore

    needles: list[str] = []
    udd_norm = user_data_dir.lower().replace("/", "\\")
    needles.append(udd_norm)
    last = udd_norm.rstrip("\\").rsplit("\\", 1)[-1]
    if last:
        needles.append(last)
    if profile_id:
        needles.append(profile_id.lower())

    mlx_home_norm = str(_mlx_home()).lower().replace("/", "\\")

    for proc in psutil.process_iter(["pid", "name", "exe", "cmdline"]):
        try:
            if not (proc.info["name"] and "chrome" in proc.info["name"].lower()):
                continue
            exe = (proc.info["exe"] or "").lower().replace("/", "\\")
            cmd = " ".join(proc.info["cmdline"] or []).lower().replace("/", "\\")
            if not any(n in cmd for n in needles):
                continue
            if mlx_only:
                if "mimic" not in exe and mlx_home_norm not in exe:
                    continue
            else:
                # Include Playwright bundled Chromium and Mimic.
                if (
                    "mimic" not in exe
                    and mlx_home_norm not in exe
                    and "ms-playwright" not in exe
                    and "chromium" not in exe
                ):
                    # Still kill if cmdline clearly owns this MLX profile path.
                    if "mlx" not in cmd and "profiles" not in cmd:
                        continue
            proc.kill()
        except Exception:
            pass


def _find_mlx_chrome_for_profile(user_data_dir: str | Path | None = None) -> Path | None:
    """Prefer Mimic chrome.exe whose major version is >= profile Last Version."""
    deps = _mlx_home() / "deps"
    if not deps.is_dir():
        return None
    candidates = [p for p in deps.glob("mimic_*/chrome.exe") if p.is_file()]
    if not candidates:
        return None

    def _mimic_key(chrome: Path) -> tuple[int, int, str]:
        # mimic_150.4 -> (150, 4)
        name = chrome.parent.name  # mimic_150.4
        ver = name.replace("mimic_", "", 1)
        parts = ver.split(".")
        try:
            major = int(parts[0])
            minor = int(parts[1]) if len(parts) > 1 else 0
        except ValueError:
            major, minor = 0, 0
        return major, minor, name

    candidates.sort(key=_mimic_key, reverse=True)
    needed = _parse_chrome_major_minor(_profile_chrome_version(user_data_dir) if user_data_dir else None)
    if needed is None:
        return candidates[0]
    need_major, _need_minor = needed
    for chrome in candidates:
        major, _minor, _ = _mimic_key(chrome)
        if major >= need_major:
            return chrome
    return candidates[0]


def _downgrade_launch_error(exc: BaseException) -> bool:
    err = str(exc).lower()
    return (
        "chrome version downgrade" in err
        or "user data migration" in err
        or ("access is denied" in err and "chromedelete" in err.replace("_", "").replace(" ", ""))
        or ("access is denied" in err and "gpupersistentcache" in err.lower())
    )


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
        For CDP sessions we disconnect then stop the driver.
        For persistent contexts (PlaywrightProfile) we must NOT call
        playwright.stop() — that would tear down the Chromium window.
        """
        is_cdp = hasattr(self.browser, "disconnect")
        try:
            if keep_browser_open:
                if is_cdp:
                    self.browser.disconnect()
                # else: persistent context — leave window open; skip stop below
            else:
                self.browser.close()
        except Exception:
            pass
        # Stopping Playwright after CDP disconnect is safe (remote browser stays).
        # Stopping after launch_persistent_context closes the browser — skip when handing off.
        if keep_browser_open and not is_cdp:
            return
        try:
            self.playwright.stop()
        except Exception:
            pass
        if getattr(_pw_tls, "playwright", None) is self.playwright:
            _pw_tls.playwright = None
        _reset_asyncio_running_loop()


class BrowserProvider(Protocol):
    def open(self, entry: AccountMapEntry, account_label: str) -> BrowserConnection: ...

    def close(self, entry: AccountMapEntry) -> None: ...


# Per-thread sync Playwright driver. A failed CDP attach that leaves the driver
# running makes the next sync_playwright().start() raise
# "Playwright Sync API inside the asyncio loop".
_pw_tls = threading.local()


def _reset_asyncio_running_loop() -> None:
    """Clear any asyncio running-loop mark so Playwright sync API can start.

    Covers: inherited ContextVar (some 3.14 builds), threading.local TLS, and
    the CPython _asyncio._set_running_loop used by get_running_loop().
    """
    try:
        import asyncio
        import asyncio.events as _ae

        _rl = getattr(_ae, "_running_loop", None)
        if _rl is not None and hasattr(_rl, "set"):
            _rl.set(None)
        elif _rl is not None and hasattr(_rl, "loop_pid"):
            _rl.loop_pid = (None, None)
        setter = getattr(_ae, "_set_running_loop", None) or getattr(asyncio, "_set_running_loop", None)
        if callable(setter):
            setter(None)
    except Exception:
        pass


def _stop_thread_playwright() -> None:
    """Stop a leaked sync Playwright driver in this thread (if any)."""
    pw = getattr(_pw_tls, "playwright", None)
    _pw_tls.playwright = None
    if pw is None:
        return
    try:
        pw.stop()
    except Exception:
        pass
    _reset_asyncio_running_loop()


def _disable_background_throttling(context, page) -> None:
    """Stop Chrome throttling the page when its window is minimised or behind
    another one.

    Confirmed live 2026-07-29 (Test139): with the browser window in the
    background, identical steps ran 10-50x slower — the first ad's whole
    sequence took 7-80s per step, while the second ad's name step took 2m52s
    and its creative upload took 12 minutes, then "failed". Chrome throttles
    background renderers (timers clamped, rAF stopped, rendering paused), and
    Playwright waits on exactly those signals for visibility/actionability,
    so it stalls until its timeout and reports a failure that has nothing to
    do with the page's actual content.

    We attach over CDP to a Multilogin profile, so we can't pass the usual
    `--disable-renderer-backgrounding` launch flags — but these two CDP
    commands achieve the same thing on a live target.
    """
    try:
        session = context.new_cdp_session(page)
    except Exception:
        return
    # Report the page as focused/active regardless of real window state.
    try:
        session.send("Emulation.setFocusEmulationEnabled", {"enabled": True})
    except Exception:
        pass
    # Keep the page out of the frozen/discarded lifecycle states.
    try:
        session.send("Page.setWebLifecycleState", {"state": "active"})
    except Exception:
        pass


# Keep strong references to keep-alive CDP sessions: if the session object
# is garbage-collected python-side, its event handlers (screencast acks)
# die with it and Chrome may pause the cast.
_KEEPALIVE_SESSIONS: list = []


def _keep_renderer_active(context, page) -> None:
    """Force the renderer to keep producing frames while minimised.

    Measured live 2026-07-30 on the DQ204 profile (window minimised, focus
    emulation + active lifecycle already applied):
        rAF 26/s, timers 5.2/s        <- still throttled ~2x
        + tiny CDP screencast:
        rAF 43.6/s, timers 9.3/s      <- near the restored-window reference
        restored window reference:
        rAF 59.9/s, timers 10/s

    Focus emulation fools document.hasFocus(), but Windows occlusion still
    clamps background timers to ~200ms and stops begin-frames — which is
    what made minimised runs randomly slow ("the whole test didn't load").
    Starting a screencast obliges Chrome to schedule frames regardless of
    occlusion; a 320x200 jpeg every 2nd frame is negligible overhead, and
    frames are ACKed so the cast never stalls on backpressure.
    """
    try:
        session = context.new_cdp_session(page)

        def _ack(params: dict) -> None:
            try:
                session.send("Page.screencastFrameAck", {"sessionId": params["sessionId"]})
            except Exception:
                pass

        session.on("Page.screencastFrame", _ack)
        session.send(
            "Page.startScreencast",
            {"format": "jpeg", "quality": 20, "maxWidth": 320, "maxHeight": 200,
             "everyNthFrame": 2},
        )
        _KEEPALIVE_SESSIONS.append(session)
    except Exception as exc:
        log.debug("keep_renderer_active skipped: %s", exc)


def _patch_window_geometry(session, page) -> None:
    """Make window.outer* consistent with the viewport.

    Measured 2026-07-31 on the live profile: outerWidth 1280 while
    innerWidth was 1600 — a window narrower than its own content area,
    which no real browser window can be, and a one-line tell for any
    script that looks. It comes from the Mimic profile's device-pixel
    ratio disagreeing with the reported window bounds. Applied both to
    future documents and to the one already open.
    """
    src = """
        (() => {
          const fix = () => {
            try {
              const iw = window.innerWidth, ih = window.innerHeight;
              if (!iw || !ih) return;
              if (window.outerWidth >= iw && window.outerHeight >= ih) return;
              Object.defineProperty(window, 'outerWidth',
                { get: () => window.innerWidth, configurable: true });
              Object.defineProperty(window, 'outerHeight',
                { get: () => window.innerHeight + 88, configurable: true });
            } catch (e) {}
          };
          fix();
          window.addEventListener('resize', fix, { passive: true });
        })();
    """
    try:
        session.send("Page.addScriptToEvaluateOnNewDocument", {"source": src})
    except Exception:
        pass
    try:
        page.evaluate(src)
    except Exception:
        pass


def _ensure_wide_window(context, page) -> None:
    """Force the browser window's layout to a wide desktop size, even if it
    is currently minimised or was left in narrow restored bounds.

    Confirmed live 2026-07-29 (Test142): the flow's DOM heuristics assume a
    desktop-width layout — in a ~1000px window Meta truncates tree-row
    names (breaking exact-text matching) and the tree panel is narrower
    than coordinate heuristics expect. The window had been un-maximised at
    some point, so minimising it kept a NARROW layout, and every
    additional-ad step failed in seconds. Win32 ShowWindow(maximise) +
    ShowWindow(minimise) did NOT relayout Chrome; CDP Browser.setWindowBounds
    does. If the window was minimised, it is re-minimised afterwards so the
    user-visible state (and the background-throttling test mode) is
    preserved — layout keeps the last non-minimised size.
    """
    try:
        session = context.new_cdp_session(page)
        info = session.send("Browser.getWindowForTarget")
        window_id = info.get("windowId")
        bounds = info.get("bounds") or {}
        if window_id is None:
            return
        was_minimized = bounds.get("windowState") == "minimized"
        width = bounds.get("width") or 0
        if not was_minimized and width >= 1280:
            # Nothing to resize — but the geometry patch must still run:
            # the outerWidth<innerWidth anomaly comes from the profile's
            # device-pixel-ratio, not from our resize, so it is present
            # even on a window that was already wide.
            _patch_window_geometry(session, page)
            return
        session.send(
            "Browser.setWindowBounds",
            {"windowId": window_id, "bounds": {"windowState": "normal"}},
        )
        session.send(
            "Browser.setWindowBounds",
            {"windowId": window_id,
             "bounds": {"left": 0, "top": 0, "width": 1920, "height": 1040}},
        )
        time.sleep(1.0)  # let Chrome actually relayout at the new size

        # Repair the impossible geometry this can leave behind. Measured
        # 2026-07-31: outerWidth 1280 while innerWidth was 1600 — a window
        # narrower than its own viewport, which no real browser window can
        # be, and a one-line check for any script that cares. It happens
        # because the CDP resize and the device-pixel-ratio scaling of a
        # Mimic profile disagree. Override the reported outer* values to
        # sit just outside the real viewport (normal chrome thickness).
        _patch_window_geometry(session, page)
        if was_minimized:
            session.send(
                "Browser.setWindowBounds",
                {"windowId": window_id, "bounds": {"windowState": "minimized"}},
            )
    except Exception as exc:
        log.debug("ensure_wide_window skipped: %s", exc)


def _connect_playwright(*, cdp_url: str, provider: str, debug_port: str | None, ws_endpoint: str | None) -> BrowserConnection:
    ensure_playwright_browsers_path()
    # Prior failed attach may have left sync Playwright's loop running in-thread.
    _stop_thread_playwright()
    _reset_asyncio_running_loop()
    pw = sync_playwright().start()
    _pw_tls.playwright = pw
    try:
        browser = pw.chromium.connect_over_cdp(cdp_url)
        if browser.contexts:
            context = browser.contexts[0]
            page = context.pages[0] if context.pages else context.new_page()
        else:
            context = browser.new_context()
            page = context.new_page()
        _disable_background_throttling(context, page)
        _ensure_wide_window(context, page)
        _keep_renderer_active(context, page)
        # Install human-activity tracker on the page
        try:
            from src.fb_ads_rpa.pacing import _install_mouse_tracker
            _install_mouse_tracker(page)
        except Exception:
            pass
        return BrowserConnection(
            provider=provider,
            page=page,
            browser=browser,
            playwright=pw,
            debug_port=debug_port,
            ws_endpoint=ws_endpoint,
        )
    except Exception:
        _stop_thread_playwright()
        raise


class MultiloginBrowserProvider:
    """Start Multilogin profile with automation_type=playwright, connect via CDP."""

    def __init__(self, *, headless: bool = False):
        self.headless = headless
        self._started_profile_id: str | None = None
        self._launcher: Any = None

    def open(self, entry: AccountMapEntry, account_label: str) -> BrowserConnection:
        from src.meta_campaign_agent.multilogin_config import get_multilogin_instance
        from src.meta_campaign_agent.multilogin_launcher import MultiloginLauncher, MultiloginLauncherError

        if not entry.profile_id:
            raise MultiloginLauncherError(f"{account_label}: profile_id is required")

        ml = get_multilogin_instance(entry.multilogin_instance)
        self._launcher = MultiloginLauncher(ml)
        started = self._launcher.start_profile(entry.folder_id, entry.profile_id, headless=self.headless)
        self._started_profile_id = started.profile_id

        if not started.port:
            # Leave profile running — do not stop on a port resolution glitch.
            self.close(entry, keep_browser_open=True)
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
            # Never stop_profile here: profile may have been already running
            # (reuse) or user needs the window left open after a CDP blip.
            self.close(entry, keep_browser_open=True)
            raise

    def close(self, entry: AccountMapEntry, *, keep_browser_open: bool = False) -> None:
        # Policy: never stop Multilogin after campaign upload — user verifies FB login.
        # keep_browser_open=False is ignored for stop_profile (compat only).
        _ = keep_browser_open
        self._started_profile_id = None
        self._launcher = None


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
        return _find_mlx_profile_dir(profile_id)

    def _find_mlx_chrome(self, user_data_dir: str | None = None) -> Path | None:
        """Find MLX Mimic chrome.exe (prefer version >= profile Last Version)."""
        return _find_mlx_chrome_for_profile(user_data_dir)

    def _kill_profile_browser(self, user_data_dir: str, profile_id: str = "") -> None:
        """Kill all MLX chrome.exe processes that own this user-data-dir."""
        _kill_chrome_for_user_data_dir(user_data_dir, profile_id, mlx_only=True)

    def _cdp_accepting(self, port: int | None = None) -> bool:
        import socket

        p = port or self.CDP_PORT
        try:
            with socket.create_connection(("127.0.0.1", p), timeout=1):
                return True
        except OSError:
            return False

    def _profile_process_running(self, user_data_dir: str, profile_id: str = "") -> bool:
        """True if an MLX Mimic chrome process is using this profile dir."""
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
                    return True
            except Exception:
                continue
        return False

    def _discover_debug_port(self, user_data_dir: str, profile_id: str = "") -> int | None:
        """Find CDP port of an already-running Mimic (DevToolsActivePort or cmdline)."""
        import psutil  # type: ignore

        # Chrome writes DevToolsActivePort into the profile dir when debugging is on
        for candidate in (
            Path(user_data_dir) / "DevToolsActivePort",
            Path(user_data_dir) / "Default" / "DevToolsActivePort",
        ):
            try:
                if candidate.is_file():
                    first = candidate.read_text(encoding="utf-8", errors="replace").splitlines()[0].strip()
                    if first.isdigit():
                        port = int(first)
                        if self._cdp_accepting(port):
                            return port
            except Exception:
                pass

        # Parse --remote-debugging-port=NNNN from Mimic cmdline
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
                cmdline = proc.info["cmdline"] or []
                cmd = " ".join(cmdline).lower().replace("/", "\\")
                if not any(n in cmd for n in needles):
                    continue
                for arg in cmdline:
                    if isinstance(arg, str) and "--remote-debugging-port=" in arg:
                        try:
                            port = int(arg.split("=", 1)[1].strip())
                        except ValueError:
                            continue
                        if port > 0 and self._cdp_accepting(port):
                            return port
            except Exception:
                continue

        if self._cdp_accepting(self.CDP_PORT):
            return self.CDP_PORT
        return None

    def _try_attach_existing(
        self, profile_id: str, user_data_dir: str
    ) -> "BrowserConnection | None":
        """Reuse an already-running Mimic for this profile (do not kill/restart)."""
        import logging

        log = logging.getLogger(__name__)
        if not self._profile_process_running(user_data_dir, profile_id):
            return None
        port = self._discover_debug_port(user_data_dir, profile_id)
        if port is None:
            log.info(
                "Profile %s is running but no CDP debug port found — will relaunch with debugging",
                profile_id,
            )
            return None
        try:
            cdp_url = f"http://127.0.0.1:{port}"
            conn = _connect_playwright(
                cdp_url=cdp_url,
                provider="multilogin_direct",
                debug_port=str(port),
                ws_endpoint=None,
            )
            log.info("Reusing already-running Mimic profile %s on CDP %s", profile_id, port)
            return conn
        except Exception as exc:
            log.warning("Could not attach to running Mimic (%s) — will relaunch", exc)
            return None

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

        chrome = self._find_mlx_chrome(self._user_data_dir)
        if chrome is None:
            raise RuntimeError("MLX Mimic browser (chrome.exe) not found under mlx/deps/")

        # Prefer attaching to an already-open profile instead of killing it.
        reused = self._try_attach_existing(profile_id, self._user_data_dir)
        if reused is not None:
            self._proc = None  # we did not spawn this process
            return reused

        # SAFETY GATE (user directive 2026-08-24). Everything below relaunches
        # Mimic ourselves, and the command we build carries NO --proxy-server
        # and passes --disable-extensions, which switches off the MLX Agent —
        # the component that applies the profile's proxy and part of its
        # masking. Nothing else supplies one: the profile's Chrome Preferences
        # has no `proxy` key and the proxy columns in ad_account_map.csv are
        # empty. So this branch would put the ad account on Facebook from the
        # machine's real IP, unmasked — the same signature that got the
        # PlaywrightProfile fallback gated off on 2026-07-30, just wearing
        # Mimic's binary.
        #
        # The attach branch above is safe and is the normal case: MLX starts
        # profiles with --proxy-server plus a debug port, and we merely connect
        # to it. Reaching here means the profile is either closed or open
        # without a debug port, and the fix is for a human to open it in MLX
        # Desktop — not for us to quietly relaunch it stripped.
        allow = (os.environ.get("FB_RPA_ALLOW_UNPROXIED_RELAUNCH") or "").strip().lower()
        if allow not in ("1", "true", "yes", "on"):
            running = self._profile_process_running(self._user_data_dir, profile_id)
            why = (
                "the profile is open but exposes no CDP debug port"
                if running
                else "the profile is not running"
            )
            raise RuntimeError(
                f"{account_label}: refusing to relaunch this Multilogin profile. "
                f"DirectCDP can only attach to a profile MLX itself started, and {why}. "
                "Relaunching it here would start the browser WITHOUT the profile's proxy "
                "and with the MLX Agent disabled, putting the ad account on Facebook from "
                "this machine's real IP. "
                "Open the profile in the Multilogin Desktop app and retry. "
                "(Override only if you understand the risk: FB_RPA_ALLOW_UNPROXIED_RELAUNCH=1.)"
            )
        log.warning(
            "Relaunching Mimic for profile %s WITHOUT proxy or MLX Agent "
            "(FB_RPA_ALLOW_UNPROXIED_RELAUNCH is set) — the account will appear from this "
            "machine's real IP.",
            profile_id,
        )

        # Stop any running instance of this profile only when we must relaunch
        # (process up without our CDP port, or nothing running).
        self._kill_profile_browser(self._user_data_dir, profile_id)
        time.sleep(2)  # let Chrome release the profile lock
        _cleanup_chrome_migration_leftovers(self._user_data_dir)

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
        # Never kill Mimic after campaign upload — user verifies FB login in the window.
        _ = keep_browser_open
        self._proc = None
        self._user_data_dir = None
        self._profile_id_cache = None


class MultiloginAttachOnlyProvider:
    """Connect to Multilogin via MLX automation API when the user clicks Publish.

    Does NOT fall back to vanilla Playwright Chromium or DirectCDP relaunch.
    MLX exposes CDP only when the profile is started with automation_type=playwright;
    a profile opened manually in MLX Desktop has no debug port. In that case we close
    the manual Mimic window locally and restart the same profile through the MLX API.
    """

    def __init__(self) -> None:
        self._direct = MultiloginDirectCDPProvider()
        self._launcher: Any = None
        self._api_started = False

    def _start_via_mlx_api(
        self,
        entry: AccountMapEntry,
        account_label: str,
        profile_id: str,
        udd: str,
    ) -> Any:
        from src.meta_campaign_agent.multilogin_config import get_multilogin_instance
        from src.meta_campaign_agent.multilogin_launcher import (
            MultiloginLauncher,
            MultiloginLauncherError,
        )

        folder_id = (entry.folder_id or "").strip() or _folder_id_from_profile_dir(udd)
        if not folder_id:
            raise RuntimeError(
                f"{account_label}: folder_id missing in ad_account_map.csv and "
                f"could not infer it from MLX profile path for {profile_id}"
            )
        if not (entry.folder_id or "").strip():
            log.info(
                "%s: inferred folder_id=%s from MLX profile dir (add to ad_account_map.csv)",
                account_label,
                folder_id,
            )

        ml = get_multilogin_instance(entry.multilogin_instance)
        self._launcher = MultiloginLauncher(ml)
        last_exc: MultiloginLauncherError | None = None
        for attempt in range(3):
            try:
                started = self._launcher.start_profile(
                    folder_id, profile_id, headless=False
                )
                if started.port:
                    self._api_started = True
                    return started
                last_exc = MultiloginLauncherError(
                    "MLX API did not return a CDP port after start"
                )
            except MultiloginLauncherError as exc:
                last_exc = exc
                msg = str(exc).lower()
                # A manually (Quick) launched profile holds MLX's lock and has no CDP.
                # "can't lock profile" / "already running" both mean: MLX must release
                # this profile before it can start it with automation enabled.
                lock_conflict = (
                    "can't lock profile" in msg
                    or "cant lock profile" in msg
                    or "cannot lock" in msg
                    or "can't lock" in msg
                    or "browser process is running" in msg
                    or "already running" in msg
                    or "profile_already_running" in msg
                )
                if attempt < 2 and lock_conflict:
                    # Gracefully stop ONLY this profile via MLX API. This releases MLX
                    # Desktop's lock, preserves the profile's cookies/login, and does
                    # NOT touch any of the user's other open profiles.
                    log.warning(
                        "Profile %s is locked (opened manually in MLX). Stopping it via "
                        "MLX API and restarting with automation enabled…",
                        profile_id,
                    )
                    try:
                        self._launcher.stop_profile(profile_id, folder_id=folder_id)
                    except MultiloginLauncherError as stop_exc:
                        # Last resort: kill Mimic for THIS profile's dir only (scoped).
                        log.warning(
                            "MLX stop_profile failed (%s); killing Mimic for this "
                            "profile only (other profiles untouched)",
                            stop_exc,
                        )
                        _kill_chrome_for_user_data_dir(udd, profile_id, mlx_only=True)
                    time.sleep(3)
                    continue
                raise RuntimeError(
                    f"Не удалось запустить профиль {account_label!r} через MLX automation. "
                    f"MLX API: {exc}"
                ) from exc
        raise RuntimeError(
            f"MLX API не вернул CDP-порт для {account_label!r}. "
            f"Последняя ошибка: {last_exc}"
        )

    def open(self, entry: AccountMapEntry, account_label: str) -> BrowserConnection:
        profile_id = entry.profile_id
        if not profile_id:
            raise RuntimeError(f"{account_label}: profile_id is required")

        user_data_dir = _find_mlx_profile_dir(profile_id)
        if user_data_dir is None:
            raise RuntimeError(
                f"MLX profile directory not found for profile_id={profile_id}"
            )
        udd = str(user_data_dir)

        reused = self._direct._try_attach_existing(profile_id, udd)
        if reused is not None:
            log.info("Attached to already-open Multilogin profile %s", profile_id)
            return reused

        # Manually (Quick) launched MLX profiles expose no CDP port, so we cannot
        # attach. _start_via_mlx_api gracefully stops ONLY this profile via the MLX
        # API (releasing MLX's lock, keeping cookies) and restarts it with automation
        # enabled. It does NOT kill other open profiles.
        if self._direct._profile_process_running(udd, profile_id):
            log.info(
                "Profile %s is open in MLX manual mode (no CDP). Will restart it via "
                "MLX automation API (other profiles untouched).",
                profile_id,
            )

        started = self._start_via_mlx_api(entry, account_label, profile_id, udd)
        ws = started.ws_endpoint or ""
        if ws and not ws.startswith("/"):
            ws = f"/{ws}"
        cdp = f"http://127.0.0.1:{started.port}{ws}"
        log.info(
            "Connected via MLX automation CDP to profile %s on port %s",
            profile_id,
            started.port,
        )
        return _connect_playwright(
            cdp_url=cdp,
            provider="multilogin_attach",
            debug_port=started.port,
            ws_endpoint=started.ws_endpoint,
        )

    def close(self, entry: AccountMapEntry, *, keep_browser_open: bool = False) -> None:
        # Never stop the Multilogin profile — user keeps their window.
        self._launcher = None
        self._api_started = False


class PlaywrightProfileProvider:
    """Launch Playwright Chromium with the MLX profile directory (last-resort fallback).

    Prefer Multilogin API or DirectCDP. This path often fails when Mimic wrote a
    newer profile (Chrome downgrade migration / Access denied on GPU cache).
    """

    def __init__(self, *, headless: bool = False):
        self.headless = headless
        self._context = None  # BrowserContext (persistent)
        self._playwright = None
        self._user_data_dir: str | None = None

    def _find_profile_user_data_dir(self, profile_id: str) -> Path | None:
        return _find_mlx_profile_dir(profile_id)

    def _kill_profile_browser(self, user_data_dir: str, profile_id: str = "") -> None:
        """Kill Mimic and Playwright Chromium processes holding this profile."""
        _kill_chrome_for_user_data_dir(user_data_dir, profile_id, mlx_only=False)

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
        profile_ver = _profile_chrome_version(self._user_data_dir)

        # Kill any browser for this profile (releases profile lock)
        self._kill_profile_browser(self._user_data_dir, profile_id)
        time.sleep(2)
        _cleanup_chrome_migration_leftovers(self._user_data_dir)

        ensure_playwright_browsers_path()
        _stop_thread_playwright()
        _reset_asyncio_running_loop()
        self._playwright = sync_playwright().start()
        _pw_tls.playwright = self._playwright
        try:
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
        except Exception as exc:
            try:
                self._playwright.stop()
            except Exception:
                pass
            self._playwright = None
            if getattr(_pw_tls, "playwright", None) is not None:
                _pw_tls.playwright = None
            _reset_asyncio_running_loop()
            if _downgrade_launch_error(exc) or "browser has been closed" in str(exc).lower():
                raise RuntimeError(
                    f"Playwright Chromium cannot open MLX profile "
                    f"(profile Chrome {profile_ver or 'unknown'}). {_MLX_OPEN_HINT} "
                    f"Original error: {exc}"
                ) from exc
            raise
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
            # Pin refs at module level — job runner drops RpaPublisher after close().
            if self._context is not None or self._playwright is not None:
                _HANDOFF_PLAYWRIGHT_SESSIONS.append((self._context, self._playwright))
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
    if key in ("multilogin_attach", "attach", "attach_only"):
        return MultiloginAttachOnlyProvider()
    if key in ("playwright_profile", "pw_profile"):
        return PlaywrightProfileProvider()
    raise ValueError(
        f"Unknown browser provider {key!r} — use multilogin_attach, multilogin, "
        f"multilogin_direct, playwright_profile, or dolphin"
    )
