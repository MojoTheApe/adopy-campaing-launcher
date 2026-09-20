"""Playwright browser session via antidetect profile (Multilogin / Dolphin)."""
from __future__ import annotations

import asyncio
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
    remote: bool = False

    def close(self, *, keep_browser_open: bool = False) -> None:
        """Close or disconnect from the browser.

        If keep_browser_open=True, Playwright disconnects without closing
        the browser window (hand-off mode — user continues manually).
        For CDP sessions we disconnect then stop the driver.
        For persistent contexts (PlaywrightProfile) we must NOT call
        playwright.stop() — that would tear down the Chromium window.
        """
        is_cdp = self.remote or hasattr(self.browser, "disconnect")
        try:
            if keep_browser_open:
                if hasattr(self.browser, "disconnect"):
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


def _new_background_page(context):
    """Новая вкладка, не забирающая экран.

    `context.new_page()` создаёт вкладку через `Target.createTarget` без флага
    `background`, и Chrome не просто делает её активной — он **поднимает само
    окно профиля**. Человек сидит в другом приложении, а ему на минуты
    выбрасывает Ads Manager. Вернуть фокус соседней вкладке этого не отменяет:
    окно уже всплыло (проверено вживую 12.09).

    У CDP флаг есть, и он ровно про это. Не вышло — возвращаем `None`, и
    вызывающий откроет вкладку обычным путём: своя вкладка нужнее тишины.
    """
    session = None
    target_id = None
    owned = None
    try:
        base = next(iter(context.pages), None)
        if base is None:
            return None
        session = context.new_cdp_session(base)
        params = {"url": "about:blank", "background": True}
        info = session.send("Target.getTargetInfo").get("targetInfo", {})
        if info.get("browserContextId"):
            params["browserContextId"] = info["browserContextId"]
        target_id = session.send("Target.createTarget", params).get("targetId")
        if not target_id:
            return None
        for _ in range(60):
            for candidate in list(context.pages):
                probe = None
                try:
                    probe = context.new_cdp_session(candidate)
                    info = probe.send("Target.getTargetInfo").get("targetInfo", {})
                    if info.get("targetId") == target_id:
                        owned = candidate
                        return owned
                finally:
                    if probe is not None:
                        try:
                            probe.detach()
                        except Exception:
                            pass
            # Dispatch Playwright's page events while waiting for our target.
            base.wait_for_timeout(50)
    except Exception:
        return None
    finally:
        if session is not None:
            if target_id and owned is None:
                try:
                    session.send("Target.closeTarget", {"targetId": target_id})
                except Exception:
                    pass
            try:
                session.detach()
            except Exception:
                pass
    return None


def _front_page(context):
    """Какая вкладка сейчас на экране — чтобы вернуть её человеку.

    У Playwright нет понятия «активная вкладка», зато сама страница знает:
    видимая отвечает `visible`, остальные `hidden`. Спрашиваем до того, как
    создадим свою: после — ответят обе, потому что на нашей включена подделка
    фокуса.

    Ничего не нашли — значит и возвращать нечего; молчим, а не гадаем.
    """
    try:
        pages = list(context.pages or [])
    except Exception:
        return None
    for candidate in pages:
        try:
            if candidate.evaluate("() => document.visibilityState") == "visible":
                return candidate
        except Exception:
            continue
    return None


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
            # Ловим и `CancelledError`, а не только `Exception`.
            #
            # С Python 3.8 `asyncio.CancelledError` наследуется от
            # BaseException, а не от Exception — то есть прежний `except
            # Exception` его НЕ ловил. Когда сессия закрывается под нами
            # (закончился прогон, закрыли профиль), подтверждение кадра
            # отменяется на полпути, исключение улетает в машинерию asyncio,
            # и в лог падает «Exception in callback … CancelledError».
            #
            # Найдено 15.09 первым же настоящим исключением, приехавшим в
            # Sentry (PYTHON-N): среди двадцати двух тикетов за сутки оно
            # было единственным со стеком — всё остальное наши собственные
            # сообщения о шагах.
            #
            # Подтверждение кадра — дело необязательное: не дошло, значит не
            # дошло, скринкаст живёт ради того, чтобы Chrome не засыпал в
            # свёрнутом окне. Ронять из-за него шум в лог незачем.
            try:
                session.send("Page.screencastFrameAck", {"sessionId": params["sessionId"]})
            except (Exception, asyncio.CancelledError):
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
    session = None
    window_id = None
    was_minimized = False
    try:
        session = context.new_cdp_session(page)
        info = session.send("Browser.getWindowForTarget")
        window_id = info.get("windowId")
        bounds = info.get("bounds") or {}
        if window_id is None:
            return
        state = bounds.get("windowState")
        was_minimized = state == "minimized"
        # 17.09 19:57: окно растягивали до 1920×1040, а это больше реального
        # монитора пользователя — страница уезжала за правый край, даже если
        # окно перед прогоном развернули на весь экран. Размер экрана внутри
        # профиля Mimic подделан (screen.availWidth ему верить нельзя),
        # поэтому не задаём размер, а разворачиваем окно — Chrome сам берёт
        # настоящий экран. Уже развёрнутое окно не трогаем.
        if state in ("maximized", "fullscreen"):
            _patch_window_geometry(session, page)
            return

        def _page_scale() -> tuple[float, int]:
            try:
                got = page.evaluate("() => [devicePixelRatio, innerWidth]")
                return float(got[0] or 0), int(got[1] or 0)
            except Exception:
                return 0.0, 0

        dpr_before, inner_before = _page_scale()
        if was_minimized:
            session.send(
                "Browser.setWindowBounds",
                {"windowId": window_id, "bounds": {"windowState": "normal"}},
            )
            # 17.09 13:14: сразу после выхода из свёрнутого Chrome новое
            # состояние не принимал.
            time.sleep(0.6)
        after = dict(bounds)
        note = None
        for _ in range(2):
            try:
                session.send(
                    "Browser.setWindowBounds",
                    {"windowId": window_id, "bounds": {"windowState": "maximized"}},
                )
            except Exception as exc:
                log.debug("maximize refused: %s", exc)
            time.sleep(1.0)  # let Chrome actually relayout at the new size
            try:
                after = (session.send("Browser.getWindowForTarget") or {}).get("bounds") or {}
            except Exception:
                after = {}
            if after.get("windowState") == "maximized":
                break
        dpr_after, inner_after = _page_scale()
        # 17.09 06:51 S15: окно больше — Mimic пересчитал масштаб 0.9 → 0.5, и
        # мастер загрузки не открывался. Упал масштаб заметно — возвращаем
        # окну прежний размер.
        if dpr_before and dpr_after and dpr_after < dpr_before * 0.85:
            try:
                session.send(
                    "Browser.setWindowBounds",
                    {"windowId": window_id, "bounds": {"windowState": "normal"}},
                )
                time.sleep(0.6)
                session.send(
                    "Browser.setWindowBounds",
                    {"windowId": window_id,
                     "bounds": {"width": bounds.get("width"), "height": bounds.get("height")}},
                )
                time.sleep(1.0)
                after = (session.send("Browser.getWindowForTarget") or {}).get("bounds") or {}
            except Exception:
                pass
            note = "reverted_scale_drop"
            dpr_after, inner_after = _page_scale()
        try:
            from src.fb_ads_rpa import run_telemetry as _rt

            _rt.record("window_resize", None if note else after.get("windowState") == "maximized",
                       note=note, diag={
                "before": {"width": bounds.get("width"), "height": bounds.get("height"),
                           "state": state, "dpr": dpr_before, "inner": inner_before},
                "target": "maximized",
                "after": {"width": after.get("width"), "height": after.get("height"),
                          "state": after.get("windowState"), "dpr": dpr_after, "inner": inner_after},
            })
        except Exception:
            pass

        # Repair the impossible geometry this can leave behind. Measured
        # 2026-07-31: outerWidth 1280 while innerWidth was 1600 — a window
        # narrower than its own viewport, which no real browser window can
        # be, and a one-line check for any script that cares. It happens
        # because the CDP resize and the device-pixel-ratio scaling of a
        # Mimic profile disagree. Override the reported outer* values to
        # sit just outside the real viewport (normal chrome thickness).
        _patch_window_geometry(session, page)
    except Exception as exc:
        log.debug("ensure_wide_window skipped: %s", exc)
    finally:
        if session is not None:
            if was_minimized and window_id is not None:
                try:
                    session.send("Browser.setWindowBounds", {
                        "windowId": window_id, "bounds": {"windowState": "minimized"},
                    })
                except Exception as exc:
                    log.debug("restore minimized window failed: %s", exc)
            try:
                session.detach()
            except Exception:
                pass


# Actual successful desktop viewport from ACL-1214's recorded reproduction.
# Browser bounds/screen.availWidth may be spoofed by the profile: use innerWidth.
MIN_EDITOR_WIDTH = 1168


#: Окна, которые робот вывел из свёрнутого состояния за край экрана:
#: id контекста → (windowId, прежние left/top). Возвращаются в конце кампании.
_OFFSCREEN: dict[int, dict] = {}


def window_home() -> tuple[int | None, int | None]:
    """Куда ставить окно профиля на время кампании.

    20.09: замер по 3288 шагам показал, что свёрнутое окно больше не
    тормозит прогон (медиана шага 11.8 с против 12.6 с на развёрнутом) —
    обходы торможения делают своё дело. Поэтому место окна теперь вопрос
    удобства, а не скорости: по умолчанию оно уезжает за край экрана, но
    `FB_RPA_WINDOW_LEFT` / `FB_RPA_WINDOW_TOP` ставят его куда угодно,
    например на второй монитор. Сворачивание прогон не останавливает:
    робот сам поднимает окно на границе следующего раздела.
    """
    raw_left = os.environ.get("FB_RPA_WINDOW_LEFT", "").strip()
    raw_top = os.environ.get("FB_RPA_WINDOW_TOP", "").strip()
    if not raw_left:
        return None, None
    try:
        return int(raw_left), int(raw_top or 0)
    except ValueError:
        log.warning("FB_RPA_WINDOW_LEFT/TOP: ожидались числа, получено %r/%r", raw_left, raw_top)
        return None, None


def _restore_offscreen(context, page) -> bool:
    """Свёрнутое окно — в обычное состояние, но за левым краем экрана.

    19.09, решение пользователя: окна профилей держат свёрнутыми, и
    выскакивать на экран они не должны. А в свёрнутое окно клики по
    координатам не попадают (цель, локации, тексты). Поэтому окно «живёт»
    за краем экрана, пока идёт кампания, и сворачивается обратно в конце
    (`put_window_back`).
    """
    session = context.new_cdp_session(page)
    info = session.send("Browser.getWindowForTarget") or {}
    window_id = info.get("windowId")
    bounds = info.get("bounds") or {}
    if window_id is None or bounds.get("windowState") != "minimized":
        return False
    session.send("Browser.setWindowBounds", {"windowId": window_id, "bounds": {"windowState": "normal"}})
    time.sleep(0.6)
    normal = (session.send("Browser.getWindowForTarget") or {}).get("bounds") or {}
    width = max(int(normal.get("width") or 0), MIN_EDITOR_WIDTH + 200)
    height = max(int(normal.get("height") or 0), 900)
    _OFFSCREEN[id(page.context)] = {"window_id": window_id,
                                    "left": normal.get("left"), "top": normal.get("top")}
    left, top = window_home()
    if left is None:
        # По умолчанию окно уезжает за левый край: пользователь держит профили
        # свёрнутыми и не хочет, чтобы они выскакивали поверх его работы.
        left, top = -(width + 400), 0
    session.send("Browser.setWindowBounds", {"windowId": window_id, "bounds": {
        "left": left, "top": top, "width": width, "height": height}})
    time.sleep(1.0)
    return True


def put_window_back(page) -> None:
    """Вернуть окно, выведенное за край экрана, на место и свернуть."""
    saved = _OFFSCREEN.pop(id(page.context), None)
    if not saved:
        return
    if window_home()[0] is not None:
        # Окно стоит там, где его попросили держать — не трогаем и не сворачиваем.
        return
    try:
        session = page.context.new_cdp_session(page)
        bounds = {k: saved[k] for k in ("left", "top") if isinstance(saved.get(k), int)}
        if bounds:
            session.send("Browser.setWindowBounds", {"windowId": saved["window_id"], "bounds": bounds})
            time.sleep(0.3)
        session.send("Browser.setWindowBounds",
                     {"windowId": saved["window_id"], "bounds": {"windowState": "minimized"}})
    except Exception as exc:
        log.debug("put_window_back failed: %s", exc)


def wake_page(page) -> dict:
    """Разбудить зависшую вкладку профиля перед перезагрузкой (19.09).

    19.09 вечером на всех четырёх профилях Meta после Create часами висела
    в «Loading creation», и простая перезагрузка не помогала. Окна профилей
    пользователь держит свёрнутыми, а наша вкладка — фоновая: Chrome
    придерживает таймеры и сеть таких страниц. Поэтому перед перезагрузкой:
    свёрнутое окно — в обычное состояние за краем экрана (как на границе
    раздела), вкладка — вперёд в своём окне, троттлинг снимается заново
    (после перезагрузки страницы Chrome мог его вернуть). В конце кампании
    окно, как и раньше, сворачивается обратно (`put_window_back`).
    """
    out = {"minimized": None, "restored": False, "front": False}
    context = page.context
    try:
        session = context.new_cdp_session(page)
        bounds = (session.send("Browser.getWindowForTarget") or {}).get("bounds") or {}
        out["minimized"] = bounds.get("windowState") == "minimized"
        if out["minimized"]:
            out["restored"] = _restore_offscreen(context, page)
    except Exception as exc:
        log.debug("wake_page window: %s", exc)
    try:
        page.bring_to_front()
        out["front"] = True
    except Exception as exc:
        log.debug("wake_page front: %s", exc)
    _disable_background_throttling(context, page)
    return out


def ensure_editor_window(page, section: str) -> bool:
    """Check at section boundaries; recover once, then stop without replaying drafts."""
    from src.fb_ads_rpa import run_telemetry as rt

    def readable(state: dict) -> bool:
        inner = state.get("inner")
        # 19.09 (SA_arc, CL_ml, CR_ait): свёрнутое окно по-прежнему сообщает
        # прежнюю ширину страницы (1707), и проверка считала его годным. А
        # клики по координатам в свёрнутое окно не попадают: не выбиралась
        # цель, не раскрывались локации, не добавлялась страна.
        if state.get("minimized"):
            return False
        return (
            isinstance(inner, (list, tuple)) and len(inner) == 2
            and all(type(v) in (int, float) and v > 0 for v in inner)
            and inner[0] >= MIN_EDITOR_WIDTH
        )

    before = rt.window_state(page)
    after = before
    if not readable(before):
        moved = False
        if before.get("minimized"):
            try:
                moved = _restore_offscreen(page.context, page)
            except Exception as exc:
                log.debug("offscreen restore failed: %s", exc)
        if not moved:
            try:
                _ensure_wide_window(page.context, page)
            except Exception as exc:
                log.debug("editor window recovery failed: %s", exc)
        after = rt.window_state(page)
    ok = readable(after)
    diag = {"section": section, "minimum_width": MIN_EDITOR_WIDTH,
            "before": before, "after": after}
    rt.record("window_bounds", ok, diag=diag)
    if not ok:
        rt.record("window_too_small", False, diag=diag)
    return ok


def _connect_playwright(*, cdp_url: str, provider: str, debug_port: str | None, ws_endpoint: str | None, new_page: bool = False) -> BrowserConnection:
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
            # Чья вкладка была на экране до нас — чтобы вернуть её обратно.
            prior = _front_page(context) if new_page else None
            if new_page:
                page = _new_background_page(context) or context.new_page()
            else:
                page = context.pages[0] if context.pages else context.new_page()
        else:
            context = browser.new_context()
            prior = None
            page = context.new_page()
        _disable_background_throttling(context, page)
        _ensure_wide_window(context, page)
        from src.fb_ads_rpa import run_telemetry as rt
        rt.record("window_bounds", None, diag={"section": "attach", "after": rt.window_state(page)})
        _keep_renderer_active(context, page)
        # Новая вкладка в Chrome сразу становится активной, и окно человека на
        # минуты уезжает на нашу страницу. Возвращаем то, на что он смотрел:
        # наша вкладка уходит в фон, но продолжает работать — торможение
        # фоновых вкладок здесь уже снято двумя вызовами выше, ради этого они
        # и делались.
        if prior is not None and prior is not page:
            try:
                prior.bring_to_front()
            except Exception:
                pass
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
            remote=True,
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

    def _discover_owned_debug_port(self, user_data_dir: str) -> int | None:
        """Require a listening socket owned by this profile's Mimic process.

        A global port or stale DevToolsActivePort file does not prove ownership.
        If process inspection is unavailable, refuse the attach.
        """
        import psutil

        def norm(value: str) -> str:
            return os.path.normcase(os.path.abspath(value.strip('"'))).replace("\\", "/").rstrip("/").lower()

        wanted = norm(user_data_dir)
        for proc in psutil.process_iter(["name", "exe", "cmdline"]):
            try:
                exe = (proc.info.get("exe") or "").lower().replace("\\", "/")
                if "mimic" not in exe and "/mlx/" not in exe:
                    continue
                args = proc.info.get("cmdline") or []
                udd = next((a.split("=", 1)[1] for a in args if a.startswith("--user-data-dir=")), "")
                if not udd and "--user-data-dir" in args:
                    udd = args[args.index("--user-data-dir") + 1]
                if not udd or norm(udd) != wanted:
                    continue
                raw = next((a.split("=", 1)[1] for a in args if a.startswith("--remote-debugging-port=")), "")
                port = int(raw) if raw else 0
                if port == 0:
                    for path in (Path(user_data_dir) / "DevToolsActivePort", Path(user_data_dir) / "Default/DevToolsActivePort"):
                        if path.is_file():
                            port = int(path.read_text(encoding="utf-8").splitlines()[0])
                            break
                if port > 0 and any(c.status == psutil.CONN_LISTEN and c.laddr.port == port for c in proc.net_connections(kind="tcp")) and self._cdp_accepting(port):
                    return port
            except (OSError, ValueError, IndexError, psutil.Error):
                continue
        return None

    # ── Публичный путь «прицепись, но не поднимай» ───────────────────────
    #
    # Публикация и чтение хотят от браузера противоположного. Публикации нужен
    # браузер любой ценой: не вышло прицепиться — останови профиль и подними
    # заново с автоматизацией. Чтению наоборот: прицепись к тому, что открыто,
    # а если открытого нет — **остановись**. Перезапуск разлогинивает аккаунт
    # (так вышло на x01) и умеет терять прокси; ради одного числа за вчера это
    # неприемлемо.
    #
    # Публичного способа сказать второе не было, и чтение звало приватные
    # методы этого класса напрямую из чужого модуля (`acl-108e`). Теперь
    # говорит — а приватные методы остаются приватными.

    def attach_existing_only(
        self, profile_id: str, user_data_dir: str, *, new_page: bool = False
    ) -> "BrowserConnection | None":
        """Прицепиться к уже открытому профилю. Никогда его не поднимать.

        `None` значит «открытого окна с отладочным портом нет», и это всё, что
        здесь происходит: ни остановки профиля, ни запуска.

        `strict_profile` зафиксирован намеренно: он требует, чтобы порт
        принадлежал именно этому профилю. Свободный поиск годится публикации,
        которая профиль только что подняла сама; чтению он позволил бы
        прицепиться к чужому окну и прочитать чужой кабинет.
        """
        return self._try_attach_existing(
            profile_id, user_data_dir, strict_profile=True, new_page=new_page
        )

    def profile_is_running(self, user_data_dir: str, profile_id: str = "") -> bool:
        """Есть ли живой процесс Mimic на этой папке профиля.

        Нужно, чтобы отличить «профиль закрыт» от «профиль открыт руками в MLX
        Desktop»: у второго нет отладочного порта, прицепиться не к чему, и
        человеку надо сказать именно это, а не «профиль не найден».
        """
        return self._profile_process_running(user_data_dir, profile_id)

    def _try_attach_existing(
        self, profile_id: str, user_data_dir: str, *, strict_profile: bool = False, new_page: bool = False
    ) -> "BrowserConnection | None":
        """Reuse an already-running Mimic for this profile (do not kill/restart)."""
        import logging

        log = logging.getLogger(__name__)
        if not self._profile_process_running(user_data_dir, profile_id):
            return None
        port = self._discover_owned_debug_port(user_data_dir) if strict_profile else self._discover_debug_port(user_data_dir, profile_id)
        if port is None:
            log.info(
                "Profile %s is running but no CDP debug port found — attachment unavailable",
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
                new_page=new_page,
            )
            log.info("Reusing already-running Mimic profile %s on CDP %s", profile_id, port)
            return conn
        except Exception as exc:
            log.warning("Could not attach to running Mimic (%s)", exc)
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
        *,
        allow_restart: bool = True,
        allow_kill: bool = True,
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
                if lock_conflict and not allow_restart:
                    raise RuntimeError(
                        f"{account_label}: profile is already running; no stop or restart was performed. "
                        "Close it in Multilogin Desktop before opening it with automation."
                    ) from exc
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
                        if not allow_kill:
                            # Чтение спенда окно не убивает никогда: жёсткий
                            # перезапуск однажды разлогинил x01 (ACL-1234).
                            raise RuntimeError(
                                f"{account_label}: the profile is open in MLX Desktop and Multilogin "
                                f"could not stop it ({stop_exc}). Nothing was closed. Close the window "
                                "in MLX Desktop and read again."
                            ) from stop_exc
                        # Last resort: kill Mimic for THIS profile's dir only (scoped).
                        log.warning(
                            "MLX stop_profile failed (%s); killing Mimic for this "
                            "profile only (other profiles untouched)",
                            stop_exc,
                        )
                        _kill_chrome_for_user_data_dir(udd, profile_id, mlx_only=True)
                    else:
                        # Said out loud so a log can tell the stop from the
                        # kill: for weeks every run went through the kill and
                        # nothing in the log distinguished the two.
                        log.info("Profile %s stopped through the MLX API", profile_id)
                    time.sleep(3)
                    continue
                raise RuntimeError(
                    f"Could not start profile {account_label!r} through MLX automation. "
                    f"MLX API: {exc}"
                ) from exc
        raise RuntimeError(
            f"MLX API returned no CDP port for {account_label!r}. "
            f"Last error: {last_exc}"
        )

    def open(self, entry: AccountMapEntry, account_label: str, *,
             allow_missing_dir: bool = False, allow_restart: bool = True,
             allow_kill: bool = True) -> BrowserConnection:
        profile_id = entry.profile_id
        if not profile_id:
            raise RuntimeError(f"{account_label}: profile_id is required")

        user_data_dir = _find_mlx_profile_dir(profile_id)
        if user_data_dir is None and not allow_missing_dir:
            raise RuntimeError(
                f"MLX profile directory not found for profile_id={profile_id}"
            )
        udd = str(user_data_dir) if user_data_dir is not None else ""

        reused = (self._direct._try_attach_existing(profile_id, udd, strict_profile=not allow_restart)
                  if udd else None)
        if reused is not None:
            log.info("Attached to already-open Multilogin profile %s", profile_id)
            return reused

        # Manually (Quick) launched MLX profiles expose no CDP port, so we cannot
        # attach. _start_via_mlx_api gracefully stops ONLY this profile via the MLX
        # API (releasing MLX's lock, keeping cookies) and restarts it with automation
        # enabled. It does NOT kill other open profiles.
        if self._direct._profile_process_running(udd or profile_id, profile_id):
            if not allow_restart:
                raise RuntimeError(f"{account_label}: profile is already running; no restart was performed")
            log.info(
                "Profile %s is open in MLX manual mode (no CDP). Will restart it via "
                "MLX automation API (other profiles untouched).",
                profile_id,
            )

        started = self._start_via_mlx_api(entry, account_label, profile_id, udd,
                                          allow_restart=allow_restart, allow_kill=allow_kill)
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
