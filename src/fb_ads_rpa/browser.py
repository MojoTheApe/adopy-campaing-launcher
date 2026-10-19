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
from typing import Any, Callable, Protocol

from playwright.sync_api import Browser, Page, Playwright, sync_playwright

from src.meta_campaign_agent.ad_account_map import AccountMapEntry
from src.fb_ads_rpa import hand_window, profile_trouble
from src.fb_ads_rpa.config import ensure_playwright_browsers_path
from src.fb_ads_rpa.window_visibility import PerPage

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


def _find_mlx_profile_dir(profile_id: str, folder_id: str = "") -> Path | None:
    """Папка профиля Multilogin — по адресу `profiles/{ws}/{folder_id}/{profile_id}`.

    Раньше здесь был `rglob` по всему `~/mlx/profiles` (полные каталоги
    Chrome, ~10 с на каждый вызов, ACL-144). Теперь смотрим только имена на
    глубине не больше двух уровней под рабочим пространством и никуда не
    спускаемся глубже. Запасного рекурсивного обхода нет намеренно: профиль,
    которого нет в известной раскладке, считается не найденным (None), а не
    ищется «где-нибудь ещё» — так нельзя взять папку чужого профиля.
    Совпадение только по точному имени каталога; пустой id -> None.
    """
    profile_id = (profile_id or "").strip()
    if not profile_id or profile_id in {".", ".."} or any(c in profile_id for c in "/\\"):
        return None
    root = _mlx_home() / "profiles"
    folder_id = (folder_id or "").strip()
    try:
        if not root.is_dir():
            return None
        workspaces = [ws for ws in root.iterdir() if ws.is_dir()]
        # Известный folder_id важнее любого другого совпадения: ищем его по
        # всем рабочим пространствам раньше, чем смотрим остальные папки.
        if folder_id:
            for ws in workspaces:
                p = ws / folder_id / profile_id
                if p.is_dir():
                    return p
        for ws in workspaces:
            p = ws / profile_id
            if p.is_dir():
                return p
            for folder in ws.iterdir():
                if not folder.is_dir():
                    continue
                p = folder / profile_id
                if p.is_dir():
                    return p
    except OSError:
        return None
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


#: ACL-1402: вкладка → удался ли `_disable_background_throttling` в последний
#: раз ({"focus", "lifecycle", "at"}). Читает `window_visibility.visibility_diag`
#: — в телеметрию раздела и долгих ожиданий. `PerPage`: `id()` умершей
#: вкладки достаётся новой, serve.py живёт сутками (ревью PR #420, F5).
THROTTLE_STATUS = PerPage()


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
    status = {"focus": False, "lifecycle": False, "at": round(time.time(), 1)}
    THROTTLE_STATUS.set(page, status)
    try:
        session = context.new_cdp_session(page)
    except Exception as exc:
        status["error"] = type(exc).__name__
        return
    # Report the page as focused/active regardless of real window state.
    try:
        session.send("Emulation.setFocusEmulationEnabled", {"enabled": True})
        status["focus"] = True
    except Exception:
        pass
    # Keep the page out of the frozen/discarded lifecycle states.
    try:
        session.send("Page.setWebLifecycleState", {"state": "active"})
        status["lifecycle"] = True
    except Exception:
        pass


# Keep strong references to keep-alive CDP sessions: if the session object
# is garbage-collected python-side, its event handlers (screencast acks)
# die with it and Chrome may pause the cast.
_KEEPALIVE_SESSIONS: list = []
#: ACL-1402: вкладки, на которых скринкаст уже запущен (`PerPage`, F5).
_KEEPALIVE_PAGES = PerPage()


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
        _KEEPALIVE_PAGES.add(page)
    except Exception as exc:
        log.debug("keep_renderer_active skipped: %s", exc)


def ensure_renderer_kept_active(context, page) -> bool:
    """Скринкаст на вкладке, если его ещё нет (ACL-1402). True — запустили сейчас.

    Свежая вкладка (`fresh_tab`, перезагрузка) получала только снятие
    троттлинга, а скринкаст — лишь вкладка, к которой подключились.
    """
    if page in _KEEPALIVE_PAGES:
        return False
    _keep_renderer_active(context, page)
    return page in _KEEPALIVE_PAGES


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
        if id(context) in _OFFSCREEN:
            # PR #314, ревью: окно, которое мы сами вывели из свёрнутого за
            # край экрана (`_unminimize_before_attach` / `_restore_offscreen`),
            # разворачивать нельзя — «развернуть» Chrome делает на мониторе
            # человека, и окно профиля ложится поверх его работы (решение
            # 19.09 — так не должно быть). Ширину добираем размером, не
            # сдвигая окно с его места за краем.
            _widen_parked_window(session, window_id, page, was_minimized,
                                 state, bounds, dpr_before, inner_before, _page_scale)
            _patch_window_geometry(session, page)
            return
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
            _SCALE_REVERTED.add(id(page.context))
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

#: Контексты, где откат падения масштаба Mimic уже сделан: окно заново не растим.
_SCALE_REVERTED: set[int] = set()


def _widen_parked_window(session, window_id, page, was_minimized, state, bounds,
                         dpr_before, inner_before, page_scale) -> None:
    """Окно за краем экрана — шире размером, без «развернуть».

    Место окна (left/top) не трогаем: оно стоит там, куда его увёл робот
    (или куда велел `FB_RPA_WINDOW_LEFT/TOP`). Если страница уже не уже
    `MIN_EDITOR_WIDTH` — ничего не делаем. Масштаб Mimic может упасть от
    большего окна (17.09 06:51 S15) — тогда возвращаем прежнюю ширину.
    """
    if was_minimized:
        session.send("Browser.setWindowBounds",
                     {"windowId": window_id, "bounds": {"windowState": "normal"}})
        time.sleep(0.6)
        dpr_before, inner_before = page_scale()
    width = int(bounds.get("width") or 0)
    height = int(bounds.get("height") or 0)
    after = dict(bounds)
    note = "offscreen_wide_enough"
    dpr_after, inner_after = dpr_before, inner_before
    if inner_before and inner_before < MIN_EDITOR_WIDTH and width:
        target = min(int(width * MIN_EDITOR_WIDTH / inner_before) + 40, 4000)
        try:
            session.send("Browser.setWindowBounds", {"windowId": window_id, "bounds": {
                "width": target, "height": max(height, 900)}})
            time.sleep(1.0)
            after = (session.send("Browser.getWindowForTarget") or {}).get("bounds") or {}
        except Exception as exc:
            log.debug("offscreen widen refused: %s", exc)
        dpr_after, inner_after = page_scale()
        note = None
        if dpr_before and dpr_after and dpr_after < dpr_before * 0.85:
            try:
                session.send("Browser.setWindowBounds", {"windowId": window_id, "bounds": {
                    "width": width, "height": height or 900}})
                time.sleep(1.0)
            except Exception:
                pass
            note = "reverted_scale_drop"
            _SCALE_REVERTED.add(id(page.context))
            dpr_after, inner_after = page_scale()
    try:
        from src.fb_ads_rpa import run_telemetry as _rt

        _rt.record("window_resize", None if note else inner_after >= MIN_EDITOR_WIDTH,
                   note=note, diag={
            "before": {"width": width, "height": height, "state": state,
                       "dpr": dpr_before, "inner": inner_before},
            "target": "offscreen",
            "after": {"width": after.get("width"), "height": after.get("height"),
                      "state": after.get("windowState"), "dpr": dpr_after, "inner": inner_after},
        })
    except Exception:
        pass


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


def _home_point(bounds: dict) -> dict:
    """Куда вернуть окно в конце кампании: его прежние left/top.

    Окно, стоящее ровно на НАШЕЙ парковке (`left == -(width + 400)`, `top == 0`
    — формула `_restore_offscreen` / `_unminimize_before_attach`), — не дом:
    оно свёрнуто посреди прогона или осталось от прошлого (25.09 14:05
    `was: {left: -1776}` при ширине 1376). Такое возвращаем в видимую точку
    (0, 0), иначе из панели задач окно разворачивалось бы за экраном.

    Любое другое отрицательное положение — законный монитор слева или сверху
    от основного (ревью v3 PR #314): окно человека там и остаётся.
    """
    left, top = bounds.get("left"), bounds.get("top")
    width = int(bounds.get("width") or 0)
    if isinstance(left, int) and isinstance(top, int) and _parked_by_us(left, top, width):
        return {"left": 0, "top": 0}
    return {"left": left, "top": top}


#: Допуск на округление координат окна при масштабе экрана, px.
_PARK_TOLERANCE = 8


#: Места, куда наша парковка ставила окна в этом процессе (left, top).
#: Финальное ревью PR #314: окно на парковке потом расширяют
#: (`_widen_parked_window` — ширина растёт, left остаётся), и формула
#: `left == -(width + 400)` его больше не узнаёт: домом считалось место за
#: краем экрана, и `put_window_back` сворачивал окно туда — из панели задач
#: оно разворачивалось за экраном. Запомненное место от ширины не зависит.
_PARK_SPOTS: set[tuple[int, int]] = set()


def _park_spot(width: int) -> tuple[int, int]:
    """Место парковки по умолчанию для окна шириной `width` — и запомнить его."""
    spot = (-(width + 400), 0)
    _PARK_SPOTS.add(spot)
    return spot


def _parked_by_us(left: int, top: int, width: int) -> bool:
    """Стоит ли окно там, куда его уводит наша парковка по умолчанию.

    Два признака: формула парковки при нынешней ширине (окно, оставшееся с
    прошлого процесса) или место, куда парковка ставила окно в этом процессе
    (окно с тех пор расширили). Положение без них — законный монитор слева
    или сверху: такое окно человека не трогаем (ревью v3 PR #314).
    """
    if abs(left + width + 400) <= _PARK_TOLERANCE and abs(top) <= _PARK_TOLERANCE:
        return True
    return any(abs(left - x) <= _PARK_TOLERANCE and abs(top - y) <= _PARK_TOLERANCE
               for x, y in _PARK_SPOTS)


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
    # Дом, сохранённый раньше (при подключении или на прошлой границе
    # раздела), не затираем: окно, свёрнутое посреди прогона (Win+D), в
    # обычном состоянии уже стоит за краем, и эти координаты — не дом
    # (PR #314, повторное ревью: put_window_back ставил окно на −1800).
    _OFFSCREEN.setdefault(id(page.context), {"window_id": window_id, **_home_point(normal)})
    left, top = window_home()
    if left is None:
        # По умолчанию окно уезжает за левый край: пользователь держит профили
        # свёрнутыми и не хочет, чтобы они выскакивали поверх его работы.
        left, top = _park_spot(width)
    session.send("Browser.setWindowBounds", {"windowId": window_id, "bounds": {
        "left": left, "top": top, "width": width, "height": height}})
    time.sleep(1.0)
    return True


def front_for_login(page) -> dict:
    """Окно разлогиненного профиля — на экран, вперёд (ACL-1604).

    Робот держит окна свёрнутыми или за краем экрана, и человек не видел, что
    профиль ждёт входа: задание вставало с `profile_logged_out`, а окно было
    спрятано. Здесь только ЭТО окно: свёрнутое — в обычное состояние, стоящее
    за краем — на экран, вкладка — вперёд. Окно не закрывается, процесс не
    трогается, другие профили не затрагиваются. `put_window_back` после этого
    окно не сворачивает (запись о доме снята) — человеку нужно им пользоваться.

    Только через сессию БРАУЗЕРА (`new_browser_cdp_session`) и id вкладки: она
    с рендерером не говорит. Сессия на самой вкладке (`context.new_cdp_session`,
    `page.bring_to_front`) на зависшем рендерере висела 197 с (замер ACL-1320), а
    сюда зовут как раз после «вкладка не отвечает»: остановка аккаунта не должна
    ждать окна. Любой сбой — молча в журнал.
    """
    out = {"restored": False, "moved": False, "front": False}
    session = None
    try:
        context = page.context
        _OFFSCREEN.pop(id(context), None)
        session = context.browser.new_browser_cdp_session()
        url = str(getattr(page, "url", "") or "")
        infos = [t for t in ((session.send("Target.getTargets") or {}).get("targetInfos") or [])
                 if t.get("type") == "page"]
        mine = [t for t in infos if url and t.get("url") == url] or []
        target = (mine[0] if mine else {}).get("targetId")
        if target:
            info = session.send("Browser.getWindowForTarget", {"targetId": target}) or {}
            wid, bounds = info.get("windowId"), info.get("bounds") or {}
            if wid is not None:
                if bounds.get("windowState") == "minimized":
                    session.send("Browser.setWindowBounds", {"windowId": wid, "bounds": {"windowState": "normal"}})
                    out["restored"] = True
                    time.sleep(0.4)
                    bounds = (session.send("Browser.getWindowForTarget", {"targetId": target}) or {}).get("bounds") or bounds
                left = bounds.get("left")
                if out["restored"] or (isinstance(left, int) and left < 0):
                    width = max(int(bounds.get("width") or 0), 1200)
                    height = max(int(bounds.get("height") or 0), 800)
                    session.send("Browser.setWindowBounds", {"windowId": wid, "bounds": {
                        "left": 40, "top": 40, "width": width, "height": height}})
                    out["moved"] = True
            session.send("Target.activateTarget", {"targetId": target})
            out["front"] = True
    except Exception as exc:
        log.debug("front_for_login: %s", exc)
    finally:
        if session is not None:
            try:
                session.detach()
            except Exception:
                pass
    try:
        from src.fb_ads_rpa import run_telemetry as rt

        rt.record("profile_front_for_login", out["front"] or out["moved"] or out["restored"], diag=dict(out))
    except Exception:
        pass
    return out


def front_if_logged_out(page, kind: str) -> None:
    """Вызывается там, где робот только что узнал вход Facebook (kind)."""
    if kind == profile_trouble.LOGGED_OUT and page is not None:
        front_for_login(page)


def put_window_back(page) -> None:
    """Вернуть окно, выведенное за край экрана, на место и свернуть."""
    saved = _OFFSCREEN.pop(id(page.context), None)
    if not saved:
        return
    if window_home()[0] is not None:
        # Окно стоит там, где его попросили держать — не трогаем и не сворачиваем.
        return
    # ACL-1320: CDP-сессия на вкладке с зависшим рендерером висела 197 с.
    # Не отвечает — окно не трогаем (оно за краем экрана и никому не мешает;
    # профиль всё равно перезапустят или сменят вкладку).
    if page_answers(page, 3.0) is False:
        log.info("put_window_back skipped: the tab does not answer")
        return
    try:
        session = page.context.new_cdp_session(page)
    except Exception as exc:
        log.debug("put_window_back: no session: %s", exc)
        return
    wid = saved["window_id"]
    # PR #314, ревью: координаты Chrome не принимает у развёрнутого (и
    # свёрнутого) окна, а прежде отказ здесь уносил с собой и сворачивание —
    # окно оставалось развёрнутым на экране человека. Поэтому три шага, и
    # каждый сам по себе: обычное состояние → прежнее место → свернуть.
    try:
        session.send("Browser.setWindowBounds", {"windowId": wid, "bounds": {"windowState": "normal"}})
        time.sleep(0.3)
    except Exception as exc:
        log.debug("put_window_back normal: %s", exc)
    bounds = {k: saved[k] for k in ("left", "top") if isinstance(saved.get(k), int)}
    if bounds:
        try:
            session.send("Browser.setWindowBounds", {"windowId": wid, "bounds": bounds})
            time.sleep(0.3)
        except Exception as exc:
            log.debug("put_window_back place: %s", exc)
    try:
        session.send("Browser.setWindowBounds", {"windowId": wid, "bounds": {"windowState": "minimized"}})
    except Exception as exc:
        log.debug("put_window_back minimize: %s", exc)


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


class ProfileUnresponsive(RuntimeError):
    """Профиль не отвечает: ни его вкладка, ни свежая вкладка рядом (ACL-1320)."""


def page_answers(page, timeout_s: float = 5.0) -> bool | None:
    """Отвечает ли вкладка: True / False (не за `timeout_s`) / None (не спросить).

    Та же проверка, что `meta_dom._page_responsive`: `wait_for_function` —
    вызов, таймаут которого соблюдает драйвер, и `() => true` отвечает на
    первом же вычислении, без кадров анимации. Ушедшую вкладку (закрыта,
    браузер отсоединён) не спрашивает вовсе — `page_liveness.answers`.
    """
    from src.fb_ads_rpa.page_liveness import answers

    got = answers(page, timeout_s)
    if got is False:
        # ACL-1402: прежде чем назвать вкладку зависшей — не скрыто ли её
        # окно (закрыто другими окнами, свёрнуто). Скрыто — будим без
        # переднего плана и спрашиваем ещё раз.
        try:
            from src.fb_ads_rpa import window_visibility

            woke = window_visibility.wake_if_hidden(page, where="page_answers", answered=False)
        except Exception:
            woke = {}
        if woke.get("woke"):
            got = answers(page, timeout_s)
    return got


def _open_tab_via_browser(context, timeout_s: float = 10.0, *, like_url: str = ""):
    """Новая фоновая вкладка через сессию самого браузера.

    `_new_background_page` открывает вкладку через сессию первой вкладки
    контекста — а это может быть та самая, что зависла. Сессия браузера
    (`new_browser_cdp_session`) с рендерером не говорит вовсе. `background`
    — чтобы окно профиля не поднималось поверх работы человека (решение
    19.09). Вкладку узнаём как ту, которой не было в `context.pages` до.
    `like_url` — адрес вкладки, рядом с которой открыть: по нему из списка
    целей браузера берётся её контекст (у Multilogin он один, но так вкладка
    точно попадёт в `context`). Сессию на самой вкладке не открываем:
    `context.new_cdp_session` на вкладке с зависшим рендерером висел 197 с
    (замер ACL-1320 на настоящем Chromium).
    """
    before = {id(p) for p in context.pages}
    session = context.browser.new_browser_cdp_session()
    try:
        params: dict = {"url": "about:blank", "background": True}
        if like_url:
            infos = (session.send("Target.getTargets") or {}).get("targetInfos") or []
            ids = {t.get("browserContextId") for t in infos
                   if t.get("type") == "page" and t.get("url") == like_url and t.get("browserContextId")}
            if len(ids) == 1:
                params["browserContextId"] = ids.pop()
        target = (session.send("Target.createTarget", params) or {}).get("targetId")
    finally:
        try:
            session.detach()
        except Exception:
            pass
    if not target:
        return None
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        for candidate in list(context.pages):
            if id(candidate) not in before:
                return candidate
        # ACL-1320: браузер отсоединился — `wait_for_event` падал бы сразу,
        # и цикл крутился бы до `deadline` без паузы.
        try:
            if context.browser is not None and context.browser.is_connected() is False:
                return None
        except Exception:
            return None
        since = time.time()
        try:
            context.wait_for_event("page", timeout=500)
            continue
        except Exception:
            pass
        left = 0.5 - (time.time() - since)
        if left > 0:
            time.sleep(left)
    return None


def fresh_tab(page, url: str, *, goto_timeout_s: float = 30.0):
    """Свежая вкладка того же профиля вместо зависшей (ACL-1320).

    Прогон cf563b4ba594 (26.09, Doc19): рендерер долгоживущей вкладки списка
    кампаний не отвечал ~14 минут, и ни пробуждение, ни перезагрузка той же
    вкладки не помогали. Новая вкладка — новый документ (и, как правило,
    новый процесс рендерера) в том же профиле: те же куки, прокси и отпечаток
    Multilogin. Никакого простого браузера.

    Порядок: новая фоновая вкладка через сессию браузера (ни одной сессии на
    зависшей) → она отвечает? → троттлинг снят, скринкаст, трекер мыши (как
    при подключении) → старая закрыта (`page.close(run_before_unload=False)`
    идёт через браузер, её рендерер не ждёт — и новая не делит с ней
    процесс) → переход на
    `url` → Meta в новой вкладке отвечает? Всё удалось — новая страница,
    иначе None: профиль не отвечает целиком, решать вызывающему (перезапуск
    профиля через MLX API).
    """
    from src.fb_ads_rpa import run_telemetry as rt

    context = page.context
    diag: dict = {"url": (url or "")[:160]}
    new = None
    try:
        from src.fb_ads_rpa.page_liveness import browser_gone

        if browser_gone(page):
            # ACL-1320: профиль остановлен — свежей вкладке взяться неоткуда.
            diag["why"] = "browser_gone"
            return None
        new = _open_tab_via_browser(context, like_url=page.url or "")
        if new is None:
            diag["why"] = "no_new_tab"
            return None
        if page_answers(new, 5.0) is False:
            diag["why"] = "new_tab_silent"
            return None
        _disable_background_throttling(context, new)
        _keep_renderer_active(context, new)
        try:
            from src.fb_ads_rpa.pacing import _install_mouse_tracker

            _install_mouse_tracker(new)
        except Exception:
            pass
        try:
            page.close(run_before_unload=False)
            diag["old_closed"] = True
        except Exception as exc:
            diag["old_closed"] = False
            diag["close_error"] = str(exc)[:80]
        if url:
            try:
                new.goto(url, wait_until="domcontentloaded", timeout=int(goto_timeout_s * 1000))
            except Exception as exc:
                # ACL-1320: свежая вкладка упёрлась во вход Facebook или в
                # сеть профиля — это не зависание, и перезапуск не поможет.
                kind, detail = profile_trouble.classify_error(exc)
                if kind == profile_trouble.NETWORK:
                    profile_trouble.note_net_error(new, detail)
                if kind:
                    diag["why"], diag["detail"] = kind, detail
                    front_if_logged_out(new, kind)
                    raise profile_trouble.ProfileStop(kind, detail) from exc
                if "timeout" not in type(exc).__name__.lower() and "Timeout" not in str(exc):
                    raise
                # Ревью PR #323 (F10): медленный переход сам по себе — не повод
                # перезапускать профиль. Старая вкладка уже закрыта; новая
                # отвечает — работаем в ней (раунд `_open_campaign` сам
                # вернёт её на список и нажмёт Create).
                if page_answers(new, 5.0) is False:
                    diag["why"] = "meta_silent_in_new_tab"
                    return None
                diag["why"], diag["note"] = "", "goto_timeout_kept"
                return new
            profile_trouble.note_nav_ok(new)
            kind, detail = profile_trouble.classify_page(new, history=False)
            if kind:
                diag["why"], diag["detail"] = kind, detail
                front_if_logged_out(new, kind)
                raise profile_trouble.ProfileStop(kind, detail)
        if page_answers(new, 5.0) is False:
            diag["why"] = "meta_silent_in_new_tab"
            return None
        diag["why"] = ""
        return new
    except profile_trouble.ProfileStop:
        raise
    except Exception as exc:
        diag["why"] = f"{type(exc).__name__}: {str(exc)[:80]}"
        return None
    finally:
        try:
            rt.record("fresh_tab", not diag.get("why"), note=diag.get("why") or "ok", diag=diag)
        except Exception:
            pass


def wake_page_quietly(page) -> dict:
    """Разбудить вкладку перед первым Create — не выводя окно вперёд (ACL-1320).

    Прогон cf563b4ba594: первое нажатие Create шло во вкладку, которая с
    прошлого прогона жила в фоне, и Chrome мог держать её придержанной или
    замороженной. Будим так же, как `wake_page`, но без `bring_to_front`:
    окно профиля по решению владельца (19.09) остаётся за краем экрана и не
    должно всплывать поверх его работы. Свёрнутое окно — в обычное состояние
    за краем экрана (`_restore_offscreen`), троттлинг и заморозка сняты
    заново (`_disable_background_throttling`). Вызывать только для вкладки,
    которая отвечает: CDP-сессия на зависшей висит (см. `fresh_tab`).
    """
    out: dict = {"minimized": None, "restored": False}
    context = page.context
    try:
        session = context.new_cdp_session(page)
        try:
            bounds = (session.send("Browser.getWindowForTarget") or {}).get("bounds") or {}
        finally:
            try:
                session.detach()
            except Exception:
                pass
        out["minimized"] = bounds.get("windowState") == "minimized"
        if out["minimized"]:
            out["restored"] = _restore_offscreen(context, page)
    except Exception as exc:
        out["error"] = str(exc)[:80]
    _disable_background_throttling(context, page)
    return out


def _visibility(page) -> dict:
    """Видимость вкладки и окна профиля для строки раздела (ACL-1402). Не бросает."""
    try:
        from src.fb_ads_rpa import window_visibility

        return window_visibility.visibility_diag(page)
    except Exception as exc:
        return {"error": str(exc)[:80]}


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

    # ACL-1673 (7641d7b3d86a): на ушедшей вкладке ширину окна не спрашиваем и
    # окно не двигаем — CDP-вызовы зря ждут, а пустые границы читались как
    # «окно слишком узкое». Одна строка с причиной, без `window_too_small`.
    from src.fb_ads_rpa.page_liveness import tab_gone

    if tab_gone(page):
        rt.record("window_bounds", False, note="page_closed", diag={"section": section})
        return False

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
    enlarged = None
    # ACL-1727 (4db2039f760d): окно вывели из свёрнутого, но оно вернулось в
    # обычный размер (внутри 1117 < 1168). Сначала увеличиваем, потом
    # измеряем ещё раз, и только после этого решаем про `window_too_small`.
    if not readable(after) and _small_but_alive(after):
        try:
            enlarged = _enlarge_small_window(page)
        except Exception as exc:
            log.debug("editor window enlarge failed: %s", exc)
        if enlarged:
            after = rt.window_state(page)
    ok = readable(after)
    diag = {"section": section, "minimum_width": MIN_EDITOR_WIDTH,
            "before": before, "after": after, "visibility": _visibility(page)}
    if enlarged:
        diag["enlarged"] = True
        diag["enlarged_to"] = enlarged
    rt.record("window_bounds", ok, diag=diag)
    if not ok:
        rt.record("window_too_small", False, diag=diag)
    return ok


def _small_but_alive(state: dict) -> bool:
    """Окно не свёрнуто, ширина страницы измерена, но меньше минимума."""
    inner = state.get("inner")
    return (
        not state.get("minimized")
        and isinstance(inner, (list, tuple)) and len(inner) == 2
        and all(type(v) in (int, float) and v > 0 for v in inner)
        and inner[0] < MIN_EDITOR_WIDTH
    )


def _enlarge_small_window(page) -> dict | None:
    """Увеличить уже обычное (не свёрнутое) окно, не выводя его на чужой экран.

    ACL-1727, ревью: ветку выбираем по настоящим границам окна, а не по
    `_OFFSCREEN`. Свёрнутое окно, которое было развёрнуто на весь экран,
    после нашего `windowState: normal` возвращается на экран в старом размере
    (854×502), хотя контекст числится в `_OFFSCREEN`.
    - стоит за краем экрана (наша парковка, `left + width <= 0`) — растёт
      пропорционально, как в `_widen_parked_window`, и ставится на парковку
      заново по новой ширине: правый край остаётся за экраном;
    - стоит на экране — разворачивается на месте (Chrome берёт тот же монитор);
    - уже развёрнуто или на весь экран — не трогаем (на маленьком экране
      шире не станет, а из полноэкранного режима окно не выводим).
    Падение масштаба Mimic (dpr < 0.85 прежнего) откатываем, как
    `_ensure_wide_window`; окно, откат которого сделали они, не трогаем.
    Возвращает размер окна после увеличения или None.
    """
    if id(page.context) in _SCALE_REVERTED:
        return None
    session = None
    try:
        session = page.context.new_cdp_session(page)
        info = session.send("Browser.getWindowForTarget") or {}
        window_id = info.get("windowId")
        bounds = info.get("bounds") or {}
        state = bounds.get("windowState")
        if window_id is None or state in ("minimized", "maximized", "fullscreen"):
            return None
        left, top = bounds.get("left"), bounds.get("top")
        width = int(bounds.get("width") or 0)
        height = int(bounds.get("height") or 0)
        try:
            got = page.evaluate("() => [devicePixelRatio, innerWidth]")
            dpr_before, inner_before = float(got[0] or 0), int(got[1] or 0)
        except Exception:
            dpr_before, inner_before = 0.0, 0
        parked = (isinstance(left, int) and isinstance(top, int) and width > 0
                  and (_parked_by_us(left, top, width) or left + width <= 0))
        if parked:
            if not inner_before:
                return None
            new_width = min(int(width * MIN_EDITOR_WIDTH / inner_before) + 40, 4000)
            new_left, new_top = _park_spot(new_width)
            target = {"left": new_left, "top": new_top, "width": new_width,
                      "height": max(height, 900)}
        else:
            target = {"windowState": "maximized"}
        session.send("Browser.setWindowBounds", {"windowId": window_id, "bounds": target})
        time.sleep(1.0)
        new = (session.send("Browser.getWindowForTarget") or {}).get("bounds") or {}
        try:
            got = page.evaluate("() => [devicePixelRatio, innerWidth]")
            dpr_after = float(got[0] or 0)
        except Exception:
            dpr_after = 0.0
        if dpr_before and dpr_after and dpr_after < dpr_before * 0.85:
            _SCALE_REVERTED.add(id(page.context))
            back = {"left": left, "top": top, "width": width, "height": height} if parked else None
            session.send("Browser.setWindowBounds",
                         {"windowId": window_id, "bounds": {"windowState": "normal"}})
            if back is None:
                back = {"width": width, "height": height}
            time.sleep(0.6)
            session.send("Browser.setWindowBounds", {"windowId": window_id, "bounds": back})
            time.sleep(1.0)
            return None
        return {"width": new.get("width"), "height": new.get("height"),
                "state": new.get("windowState")}
    finally:
        if session is not None:
            try:
                session.detach()
            except Exception:
                pass


def _unminimize_before_attach(browser) -> list[dict]:
    """Свёрнутые окна профиля — в обычное состояние, за край экрана.

    ACL-1313, прогон 25.09 07:54–08:01 (Doc21): подключение к профилю, окно
    которого было свёрнуто, простояло 7 минут и пошло дальше ровно в тот
    момент, когда владелец развернул окно. Свёрнутое окно Chrome не рисует
    кадров, а открытие вкладки и снимки ждут именно их. Команды домена
    Browser обрабатывает сам браузер, без отрисовки, — они отвечают и
    здесь, поэтому окно выводится из свёрнутого ими, до любой работы со
    страницей. Место — как у `_restore_offscreen` (решение 19.09: окна не
    должны выскакивать поверх работы человека); в конце кампании
    `put_window_back` сворачивает окно обратно.
    """
    done: list[dict] = []
    session = None
    try:
        session = browser.new_browser_cdp_session()
        targets = (session.send("Target.getTargets") or {}).get("targetInfos") or []
        seen: set = set()
        for t in targets:
            if t.get("type") != "page":
                continue
            try:
                info = session.send("Browser.getWindowForTarget", {"targetId": t.get("targetId")}) or {}
            except Exception:
                continue
            wid = info.get("windowId")
            bounds = info.get("bounds") or {}
            if wid is None or wid in seen:
                continue
            seen.add(wid)
            if bounds.get("windowState") != "minimized":
                continue
            session.send("Browser.setWindowBounds", {"windowId": wid, "bounds": {"windowState": "normal"}})
            time.sleep(0.5)
            normal = (session.send("Browser.getWindowForTarget", {"targetId": t.get("targetId")}) or {}).get("bounds") or {}
            width = max(int(normal.get("width") or 0), MIN_EDITOR_WIDTH + 200)
            height = max(int(normal.get("height") or 0), 900)
            left, top = window_home()
            if left is None:
                left, top = _park_spot(width)
            session.send("Browser.setWindowBounds", {"windowId": wid, "bounds": {
                "left": left, "top": top, "width": width, "height": height}})
            done.append({"window": wid, "was": _home_point(normal)})
    except Exception as exc:
        done.append({"error": f"{type(exc).__name__}: {str(exc)[:80]}"})
    finally:
        if session is not None:
            try:
                session.detach()
            except Exception:
                pass
    if done:
        try:
            from src.fb_ads_rpa import run_telemetry as rt

            rt.record("window_unminimized_on_attach", not any("error" in d for d in done), diag={"windows": done})
        except Exception:
            pass
    return done


def _connect_playwright(*, cdp_url: str, provider: str, debug_port: str | None, ws_endpoint: str | None,
                        new_page: bool = False, check_responsive: bool = False) -> BrowserConnection:
    ensure_playwright_browsers_path()
    # Prior failed attach may have left sync Playwright's loop running in-thread.
    _stop_thread_playwright()
    _reset_asyncio_running_loop()
    pw = sync_playwright().start()
    _pw_tls.playwright = pw
    try:
        browser = pw.chromium.connect_over_cdp(cdp_url)
        try:
            # ACL-1702: сторож вкладки задания откроет к этому браузеру СВОЁ
            # соединение CDP — адрес запоминаем за потоком задания сразу после
            # подключения, до первых вызовов страницы (они тоже могут зависнуть).
            from src.fb_ads_rpa import tab_watchdog

            tab_watchdog.note_endpoint(cdp_url)
        except Exception:
            pass
        restored = [d for d in _unminimize_before_attach(browser) if "window" in d]
        if browser.contexts:
            context = browser.contexts[0]
            # Чья вкладка была на экране до нас — чтобы вернуть её обратно.
            prior = _front_page(context) if new_page else None
            if new_page:
                page = _new_background_page(context) or context.new_page()
            else:
                page = context.pages[0] if context.pages else context.new_page()
                # ACL-1320: к уже открытому профилю — сначала ограниченный
                # вопрос «отвечает ли вкладка». Всё ниже (троттлинг, размер
                # окна, скринкаст) — CDP-вызовы без таймаута, и на зависшей
                # вкладке подключение висело бы вместе с ними. Не отвечает —
                # свежая вкладка рядом; не отвечает и она — профиль целиком
                # (`ProfileUnresponsive`, перезапуск через MLX API).
                if check_responsive and page_answers(page, 5.0) is False:
                    # ACL-1320: не отвечает — сперва диагноз. Вход в Facebook
                    # или сеть профиля — остановка аккаунта (ни свежей
                    # вкладки, ни перезапуска профиля через MLX API).
                    kind, detail = profile_trouble.classify_page(page)
                    if kind:
                        front_if_logged_out(page, kind)
                        raise profile_trouble.ProfileStop(kind, detail)
                    replacement = fresh_tab(page, page.url if (page.url or "").startswith("http") else "")
                    if replacement is None:
                        raise ProfileUnresponsive("the profile's tabs do not answer")
                    page = replacement
        else:
            context = browser.new_context()
            prior = None
            page = context.new_page()
        if restored:
            # В конце кампании `put_window_back` вернёт окно на место и свернёт.
            _OFFSCREEN[id(context)] = {"window_id": restored[0]["window"], **restored[0]["was"]}
        try:
            from src.fb_ads_rpa import window_visibility

            window_visibility.remember_cdp_url(context, cdp_url)
        except Exception:
            pass
        _disable_background_throttling(context, page)
        _ensure_wide_window(context, page)
        from src.fb_ads_rpa import run_telemetry as rt
        rt.record("window_bounds", None, diag={"section": "attach", "after": rt.window_state(page),
                                               "visibility": _visibility(page)})
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
            # ACL-1337: Windows отдаёт командную строку профиля с короткими
            # именами 8.3 (C:\Users\EVGENI~1\mlx\profiles\B18D03~1\…), а ждём мы
            # длинный путь. abspath короткие имена не раскрывает, и профиль,
            # поднятый самим роботом, выглядел «открытым руками» — аккаунт
            # останавливался (27.09, 6540 и 9943 на stage 47152af). realpath
            # на Windows раскрывает их в длинные.
            value = value.strip('"')
            try:
                value = os.path.realpath(value)
            except (OSError, ValueError):
                value = os.path.abspath(value)
            return os.path.normcase(value).replace("\\", "/").rstrip("/").lower()

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
        self, profile_id: str, user_data_dir: str, *, strict_profile: bool = False, new_page: bool = False,
        check_responsive: bool = False,
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
                # Только когда просили: чтение спенда цепляется без неё.
                **({"check_responsive": True} if check_responsive else {}),
            )
            log.info("Reusing already-running Mimic profile %s on CDP %s", profile_id, port)
            return conn
        except (ProfileUnresponsive, profile_trouble.ProfileStop):
            # Вызывающий просил проверить живость — ему и решать (ACL-1320).
            raise
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


def _exc_text(exc: BaseException, limit: int = 120) -> str:
    """Текст ошибки для телеметрии и Sentry: сначала вычистить, потом резать.

    Ревью PR #396 (F1), как PR #323 (F11): вычиститель узнаёт только полный
    uuid профиля, а обрезка до него оставила бы начало uuid в строке.
    """
    text = str(exc)
    try:
        from src.fb_ads_rpa.run_telemetry import scrub

        text = scrub(text)
    except Exception:
        text = ""
    return f"{type(exc).__name__}: {text[:limit]}"


def _record_profile_restart(entry: AccountMapEntry, profile_id: str, *, ok: bool, where: str,
                            error: str = "") -> None:
    """Строка телеметрии `profile_restarted_unresponsive` (ACL-1320)."""
    try:
        from src.fb_ads_rpa import run_telemetry as rt

        # Ревью PR #323 (F11): ни id профиля, ни его начала — вычиститель
        # ловит только полный uuid; хватает рабочего пространства.
        diag = {"where": where, "workspace": (getattr(entry, "multilogin_instance", "") or "A")}
        if error:
            diag["error"] = error
        rt.record("profile_restarted_unresponsive", ok, note=where, diag=diag)
    except Exception:
        pass


#: ACL-1320: профили, которые робот в этом процессе сам поднял через MLX API
#: или к которым цеплялся по порту автоматизации (его даёт только запуск с
#: автоматизацией). Только такой профиль без порта робот вправе остановить и
#: поднять заново: профиль, открытый руками, может держать сессию Facebook,
#: которую Multilogin ещё не сохранил (живой прогон 26.09 — владелец входил в
#: новые аккаунты, пока робот останавливал их окна).
#:
#: Профиль → процессы Mimic, которые робот поднял или к которым цеплялся.
#: Профиль робота мог умереть, а человек — открыть его заново руками: тогда
#: процесс уже другой, и это окно человека (живой прогон 26.09 15:50:
#: Facebook_9943 после неудачного перезапуска). Пустое множество — процессы
#: прочитать не удалось; тогда решает само членство.
_ROBOT_PROFILES: dict[str, frozenset[int]] = {}


def _profile_pids(user_data_dir: str, profile_id: str) -> frozenset[int]:
    """Главные процессы Mimic этого профиля (без `--type=`), пусто — не прочесть."""
    try:
        import psutil  # type: ignore
    except Exception:
        return frozenset()
    udd = (user_data_dir or "").lower().replace("/", "\\").rstrip("\\")
    # Как `_profile_process_running`: полный путь, его последняя часть (MLX
    # укорачивает пути) и id профиля.
    needles = [n for n in (udd, udd.rsplit("\\", 1)[-1], (profile_id or "").lower()) if n]
    if not needles:
        return frozenset()
    found: set[int] = set()
    try:
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                if "chrome" not in (proc.info.get("name") or "").lower():
                    continue
                cmd = " ".join(proc.info.get("cmdline") or []).lower().replace("/", "\\")
                if "--type=" in cmd or not any(n in cmd for n in needles):
                    continue
                found.add(int(proc.info["pid"]))
            except Exception:
                continue
    except Exception:
        return frozenset()
    return frozenset(found)


def _mark_robot_profile(profile_id: str, user_data_dir: str = "") -> None:
    if profile_id:
        _ROBOT_PROFILES[profile_id] = _profile_pids(user_data_dir, profile_id)


def _robot_profile(profile_id: str, user_data_dir: str = "") -> bool:
    """Профиль робота — и работает всё тот же процесс, что робот поднимал."""
    if not profile_id or profile_id not in _ROBOT_PROFILES:
        return False
    known = _ROBOT_PROFILES.get(profile_id) or frozenset()
    if not known:
        return True
    now = _profile_pids(user_data_dir, profile_id)
    return not now or bool(now & known)


def _open_by_hand(profile_id: str, account_label: str, where: str,
                  verdict: "hand_window.Verdict | None" = None) -> profile_trouble.ProfileStop:
    """Профиль открыт руками — не останавливаем, говорим человеку (ACL-1320).

    ACL-1562: сюда же — когда переоткрытие выключено на машине или мягкая
    остановка через MLX API окно не закрыла (`verdict`/`where` — почему).
    """
    why = f" ({verdict.reason})" if verdict is not None else ""
    log.warning(
        "%s: profile %s is open by hand (no automation port) - not reopened%s; "
        "the job stops with %s (%s)", account_label, profile_id, why, profile_trouble.OPEN_BY_HAND, where)
    return profile_trouble.ProfileStop(profile_trouble.OPEN_BY_HAND, where)


#: ACL-1388: сколько секунд ждать, пока браузер окна, остановленного через
#: MLX API, действительно закроется, прежде чем поднимать профиль заново.
HAND_WINDOW_EXIT_WAIT_S = 20

#: ACL-1562: строка для человека в панели задания (и на строке аккаунта
#: карты), когда робот закрывает окно, открытое руками, и поднимает профиль
#: заново. Говорится ДО остановки: человек видит, почему окно закрылось.
HAND_WINDOW_NOTICE = "The {account} window was opened by hand — reopening it with automation"

#: ACL-1562: хвост причины остановки, когда мягкая остановка через MLX API не
#: закрыла окно. Окно остаётся как было — ничего не убиваем.
HAND_WINDOW_STOP_FAILED = "Multilogin did not close the hand-opened window"

#: ACL-1606: после мягкой остановки Multilogin ещё недолго держит профиль
#: («locked» / «still running»). Запуск повторяем с короткой паузой; сумма
#: пауз — ~30 с. Часы не нужны: бюджет задан самим списком.
HAND_WINDOW_START_BACKOFF_S = (1, 1, 2, 2, 3, 3, 4, 4, 5, 5)

#: ACL-1606: причина, если Multilogin так и не отпустил профиль.
HAND_WINDOW_NOT_RELEASED = ("Multilogin did not release the profile after closing its window "
                            "— open it again in Multilogin (if it is held on another machine "
                            "or by another user, close it there)")


class ReopenCancelled(RuntimeError):
    """Stop нажат, пока ждали освобождения профиля: профиль не поднят (ACL-1606)."""


def _start_still_held(exc: BaseException) -> bool:
    """Запуск отказан, потому что Multilogin ещё держит профиль (ACL-1606)."""
    msg = str(exc).lower()
    return any(mark in msg for mark in (
        "already running", "profile_already_running", "browser process is running",
        "can't lock", "cant lock", "cannot lock"))


def _stop_clock() -> float:
    """Часы для длительности мягкой остановки (тесты подменяют)."""
    return time.monotonic()


def _record_hand_window(entry: AccountMapEntry, *, ok: bool, where: str, note: str,
                        verdict: "hand_window.Verdict", error: str = "",
                        stop_s: float | None = None, retries: int | None = None,
                        waited_s: float | None = None) -> None:
    """Строка телеметрии `profile_hand_window` и крошка Sentry (ACL-1388).

    `note` — чем кончилось (`reopened`, `disabled`, `mlx_stop_failed`…), в
    `diag` — где это было. Без id профиля, как у `_record_profile_restart`.
    ACL-1562: `stop_s` — сколько шла мягкая остановка через MLX API (вместе
    с ожиданием, пока процессы окна закроются). ACL-1606: `retries` / `waited_s` —
    сколько раз запуск повторяли, пока Multilogin держал профиль, и сколько
    секунд ждали (поля `diag`, нового шага нет).
    """
    diag = {"where": where, "workspace": (getattr(entry, "multilogin_instance", "") or "A"),
            **verdict.diag()}
    if stop_s is not None:
        diag["stop_s"] = round(stop_s, 1)
    if retries:
        diag["start_retries"] = retries
        diag["start_waited_s"] = round(waited_s or 0, 1)
    if error:
        diag["error"] = error[:160]
    try:
        from src.fb_ads_rpa import run_telemetry as rt

        rt.record("profile_hand_window", ok, note=note, diag=diag)
    except Exception:
        pass
    try:
        import sentry_sdk  # type: ignore

        # Без поднятого клиента крошка ничего не делает; с ним — ляжет в
        # следующее событие (остановку аккаунта или отказ кампании).
        sentry_sdk.add_breadcrumb(category="profile", level="info" if ok else "warning",
                                  message=f"hand_window:{note}", data={"ok": ok, **diag})
    except Exception:
        pass


def _tell_job(text: str) -> None:
    """Строка в панели задания публикации (та же, что у сжатия креативов)."""
    try:
        from src.fb_ads_rpa.creative_compress import report_notice

        report_notice(text)
    except Exception:
        pass


def _hand_window_gone(udd: str, profile_id: str, pids: frozenset[int]) -> bool:
    """Процессы окна, открытого руками, закрылись после остановки (ACL-1388)."""
    for attempt in range(HAND_WINDOW_EXIT_WAIT_S + 1):
        if not (_profile_pids(udd, profile_id) & pids):
            return True
        if attempt < HAND_WINDOW_EXIT_WAIT_S:
            time.sleep(1)
    return False


class MultiloginAttachOnlyProvider:
    """Connect to Multilogin via MLX automation API when the user clicks Publish.

    Does NOT fall back to vanilla Playwright Chromium or DirectCDP relaunch.
    MLX exposes CDP only when the profile is started with automation_type=playwright;
    a profile opened manually in MLX Desktop has no debug port.

    ACL-1320 (live run 2026-09-26): such a hand-opened window is never stopped -
    stopping it can lose a Facebook session the owner has just created and
    Multilogin has not saved yet. The job fails fast with
    `profile_open_by_hand` ("close its window, then press Publish again").
    Only a profile the robot itself started (or attached to over its
    automation port) in this process is stopped and restarted through the
    MLX API when it has no debug port.

    ACL-1388/1562: when publishing (and, with `reopen_hand_window=True`, when
    reading spend/countries or pressing "open profile"), a hand-opened window
    IS closed at once - graceful MLX API stop of that profile only, never a
    kill, no other profile touched - and reopened with automation. No age,
    foreground or input-silence conditions and no wait (ACL-1562). If the
    graceful stop fails, the stop above with the reason; nothing is killed.
    """

    def __init__(self) -> None:
        self._direct = MultiloginDirectCDPProvider()
        self._launcher: Any = None
        self._api_started = False
        #: ACL-1606: `() -> bool` — задание остановлено; проверяется между повторами запуска.
        self.cancel_check: "Callable[[], bool] | None" = None

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
                    _mark_robot_profile(profile_id, udd)
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
                if lock_conflict and not _robot_profile(profile_id, udd):
                    # ACL-1320: MLX держит профиль, а робот его не поднимал —
                    # значит, открыт руками. Не останавливаем.
                    raise _open_by_hand(profile_id, account_label, "mlx_lock") from exc
                if attempt < 2 and lock_conflict:
                    # Gracefully stop ONLY this profile via MLX API. This releases MLX
                    # Desktop's lock, preserves the profile's cookies/login, and does
                    # NOT touch any of the user's other open profiles.
                    log.warning(
                        "Profile %s (started by the robot earlier) is locked without automation. "
                        "Stopping it via MLX API and restarting with automation enabled…",
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
             allow_kill: bool = True, reopen_hand_window: bool | None = None,
             on_hand_reopen: "Callable[[], None] | None" = None) -> BrowserConnection:
        """Открыть профиль для робота.

        ACL-1518: `reopen_hand_window=True` — окно, открытое руками, закрыть и
        поднять заново тем же путём, что у Publish (только мягкая остановка
        через MLX API), даже при `allow_kill=False`. Так читают спенд и
        страны и так работает «open profile». `None` — как прежде: решает
        `allow_kill`. ACL-1562: без условий и без ожидания; `on_hand_reopen`
        зовётся один раз прямо перед мягкой остановкой (строка на карте).
        """
        profile_id = entry.profile_id
        if not profile_id:
            raise RuntimeError(f"{account_label}: profile_id is required")

        user_data_dir = _find_mlx_profile_dir(profile_id)
        if user_data_dir is None and not allow_missing_dir:
            raise RuntimeError(
                f"MLX profile directory not found for profile_id={profile_id}"
            )
        udd = str(user_data_dir) if user_data_dir is not None else ""

        unresponsive = False
        try:
            reused = (self._direct._try_attach_existing(profile_id, udd, strict_profile=not allow_restart,
                                                        check_responsive=allow_restart)
                      if udd else None)
        except ProfileUnresponsive:
            # ACL-1320: открытый профиль не отвечает — ни его вкладка, ни
            # свежая. Ждать старую вкладку бессмысленно: останавливаем профиль
            # через MLX API его же рабочего пространства и поднимаем заново.
            log.warning("Profile %s is open but does not answer; restarting it through the MLX API",
                        profile_id)
            reused = None
            unresponsive = True
        # Ревью PR #323 (F6): публикация цепляется по нестрогому поиску порта
        # (старый DevToolsActivePort, общий 19222). Профилем робота считаем
        # его только если порт слушает процесс именно этого профиля.
        owned = self._owns_port(udd)
        # ACL-1388/1562: окно, открытое руками, робот закрывает и поднимает
        # заново сам и сразу — при публикации. Чтение спенда без явной просьбы
        # (`allow_kill=False`) окно человека не закрывает и останавливается.
        # ACL-1518: чтение и «open profile» просят того же явно
        # (`reopen_hand_window=True`) — убивать им по-прежнему нельзя, а мягкая
        # остановка не убивает.
        reopen_by_hand = allow_restart and (allow_kill if reopen_hand_window is None
                                            else bool(reopen_hand_window))
        if reused is not None:
            log.info("Attached to already-open Multilogin profile %s", profile_id)
            if owned:
                _mark_robot_profile(profile_id, udd)
            return reused
        if unresponsive:
            if not owned:
                # Порт не доказан как порт этого профиля — окно могло быть
                # открыто руками: тот же путь переоткрытия (ACL-1388/1562).
                if reopen_by_hand:
                    return self._reopen_hand_opened(entry, account_label, profile_id, udd, "unowned_port",
                                                    on_reopen=on_hand_reopen)
                raise _open_by_hand(profile_id, account_label, "unowned_port")
            # У него есть свой порт автоматизации — это профиль робота.
            _mark_robot_profile(profile_id, udd)
            self._stop_profile(entry, account_label, profile_id, udd)

        # Manually (Quick) launched MLX profiles expose no CDP port, so we cannot
        # attach. ACL-1320: such a window is NOT stopped any more - it may hold a
        # Facebook session the owner has just created and Multilogin has not saved
        # yet; the job stops with `profile_open_by_hand`. Only a profile the robot
        # started itself earlier is restarted through the MLX API here (graceful
        # stop of ONLY this profile, cookies kept, other profiles untouched).
        elif self._direct._profile_process_running(udd or profile_id, profile_id):
            if not allow_restart:
                raise RuntimeError(f"{account_label}: profile is already running; no restart was performed")
            if not _robot_profile(profile_id, udd):
                if reopen_by_hand:
                    return self._reopen_hand_opened(entry, account_label, profile_id, udd,
                                                    "no_automation_port", on_reopen=on_hand_reopen)
                raise _open_by_hand(profile_id, account_label, "no_automation_port")
            log.info(
                "Profile %s (started by the robot earlier) has no CDP port. Will restart it via "
                "MLX automation API (other profiles untouched).",
                profile_id,
            )

        if not unresponsive:
            try:
                started = self._start_via_mlx_api(entry, account_label, profile_id, udd,
                                                  allow_restart=allow_restart, allow_kill=allow_kill)
            except profile_trouble.ProfileStop as stop:
                # Процесса не видно, а MLX держит профиль — тоже окно руками.
                if (reopen_by_hand and stop.code == profile_trouble.OPEN_BY_HAND
                        and stop.detail == "mlx_lock"):
                    return self._reopen_hand_opened(entry, account_label, profile_id, udd, "mlx_lock",
                                                    on_reopen=on_hand_reopen)
                raise
            return self._connect_started(started, profile_id)
        # Ревью PR #323 (F7): робот сам остановил не отвечавший профиль — не
        # поднялся он, значит профиль мог остаться закрытым. Это та же
        # остановка, что у `restart_unresponsive`: строка телеметрии и
        # `profile_restart_failed` с текстом для человека.
        try:
            started = self._start_via_mlx_api(entry, account_label, profile_id, udd,
                                              allow_restart=allow_restart, allow_kill=allow_kill)
            conn = self._connect_started(started, profile_id)
        except profile_trouble.ProfileStop:
            raise
        except Exception as exc:
            detail = _exc_text(exc)
            _record_profile_restart(entry, profile_id, ok=False, where="attach", error=detail)
            raise profile_trouble.ProfileStop(profile_trouble.RESTART_FAILED, detail) from exc
        _record_profile_restart(entry, profile_id, ok=True, where="attach")
        return conn

    def _cancel_requested(self) -> bool:
        """Задание остановлено кнопкой Stop (публикатор ставит `cancel_check`)."""
        check = getattr(self, "cancel_check", None)
        try:
            return bool(check()) if callable(check) else False
        except Exception:
            return False

    def _owns_port(self, udd: str) -> bool:
        """Отладочный порт слушает процесс Mimic именно этого профиля (F6)."""
        if not udd:
            return False
        try:
            return self._direct._discover_owned_debug_port(udd) is not None
        except Exception:
            return False

    def _connect_started(self, started: Any, profile_id: str) -> BrowserConnection:
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

    def _reopen_hand_opened(self, entry: AccountMapEntry, account_label: str, profile_id: str,
                            udd: str, where: str, *,
                            on_reopen: "Callable[[], None] | None" = None) -> BrowserConnection:
        """Закрыть окно, открытое руками, и поднять профиль для робота (ACL-1388).

        ACL-1562 (решение владельца 04.10): сразу, без условий — возраст окна,
        передний план и тишина ввода больше не проверяются, ожидания нет.
        Остался только машинный выключатель (`hand_window.enabled`).

        Обязательно и неизменно: закрытие — только мягкая остановка ЭТОГО
        профиля через MLX API (`allow_kill=False`): процесс не убиваем, другие
        профили не трогаем, простой браузер не открываем. Остановка не прошла
        или окно не закрылось — `profile_open_by_hand` с причиной, окно
        остаётся как было. Вход в Facebook проверяет обычный путь к списку
        кампаний: разлогинен — `profile_logged_out`, без повторов.
        """
        verdict = hand_window.assess()
        if not verdict.safe:
            _record_hand_window(entry, ok=False, where=where, note=verdict.reason, verdict=verdict)
            raise _open_by_hand(profile_id, account_label, where, verdict)
        pids = _profile_pids(udd, profile_id)
        log.warning("%s: profile %s is open by hand - closing it through the MLX API and reopening "
                    "it for the robot (%s)", account_label, profile_id, where)
        # Строка для человека — до остановки: он видит, почему окно закрылось.
        _tell_job(HAND_WINDOW_NOTICE.format(account=account_label))
        if on_reopen is not None:
            try:
                on_reopen()
            except Exception:
                pass
        started_at = _stop_clock()
        try:
            self._stop_profile(entry, account_label, profile_id, udd, allow_kill=False)
        except Exception as exc:
            # Не закрылось через API — окно человека остаётся как было.
            detail = _exc_text(exc)
            _record_hand_window(entry, ok=False, where=where, note="mlx_stop_failed", verdict=verdict,
                                error=detail, stop_s=_stop_clock() - started_at)
            raise _open_by_hand(profile_id, account_label,
                                f"{where}; {HAND_WINDOW_STOP_FAILED}: {detail}", verdict) from exc
        # Ревью PR #563 (F4): если процессы окна прочитать не удалось (`pids`
        # пусто — psutil не ответил, или окно держит только замок MLX), это
        # ожидание проходит сразу, и запуск может опередить освобождение
        # профиля в Multilogin. Страхует пауза 3 с в `_stop_profile`; не
        # хватило — запуск (`allow_kill=False`) кончается остановкой
        # `profile_restart_failed` с причиной. Убийства нет ни в одной ветке.
        gone = _hand_window_gone(udd, profile_id, pids)
        stop_s = _stop_clock() - started_at
        if not gone:
            _record_hand_window(entry, ok=False, where=where, note="still_running", verdict=verdict,
                                stop_s=stop_s)
            raise _open_by_hand(profile_id, account_label,
                                f"{where}; {HAND_WINDOW_STOP_FAILED} within "
                                f"{HAND_WINDOW_EXIT_WAIT_S} s", verdict)
        # ACL-1606: профиль НЕ помечаем профилем робота (ревью PR #607, F1): окно
        # уже закрыто, список процессов пуст, и метка «робота» осталась бы до
        # конца процесса — позже окно, открытое руками, Publish остановил бы
        # и убил. Помечать и не нужно: запуск идёт с `allow_restart=False`, и
        # замок Multilogin даёт ошибку раньше, чем срабатывает вопрос «чей
        # профиль». Метку ставит сам удачный запуск.
        # Окно уже закрыто: не поднялось — это отказ перезапуска, профиль
        # может остаться закрытым (как у `restart_unresponsive`). Пока
        # Multilogin ещё держит профиль, повторяем запуск (до ~30 с). Только
        # `allow_kill=False` и `allow_restart=False`: лишних остановок нет,
        # других профилей не трогаем, простой браузер не открываем.
        retries = 0
        waited_s = 0.0
        while True:
            # Ревью PR #607 (F2): Stop нажат — профиль больше не поднимаем.
            if self._cancel_requested():
                _record_hand_window(entry, ok=False, where=where, note="cancelled", verdict=verdict,
                                    error="stopped by the user", stop_s=stop_s, retries=retries,
                                    waited_s=waited_s)
                raise ReopenCancelled(f"{account_label}: stopped by the user; the profile was not reopened")
            try:
                started = self._start_via_mlx_api(entry, account_label, profile_id, udd,
                                                  allow_restart=False, allow_kill=False)
                conn = self._connect_started(started, profile_id)
                break
            except Exception as exc:
                detail = _exc_text(exc)
                if _start_still_held(exc) and retries < len(HAND_WINDOW_START_BACKOFF_S):
                    pause = HAND_WINDOW_START_BACKOFF_S[retries]
                    retries += 1
                    waited_s += pause
                    log.info("%s: Multilogin still holds profile %s after the stop; start retry %d in %s s",
                             account_label, profile_id, retries, pause)
                    time.sleep(pause)
                    continue
                if _start_still_held(exc):
                    detail = f"{HAND_WINDOW_NOT_RELEASED}: {detail}"
                _record_hand_window(entry, ok=False, where=where, note="restart_failed", verdict=verdict,
                                    error=detail, stop_s=stop_s, retries=retries, waited_s=waited_s)
                raise profile_trouble.ProfileStop(profile_trouble.RESTART_FAILED, detail) from exc
        _record_hand_window(entry, ok=True, where=where, note="reopened", verdict=verdict, stop_s=stop_s,
                            retries=retries, waited_s=waited_s)
        return conn

    def _stop_profile(self, entry: AccountMapEntry, account_label: str, profile_id: str, udd: str,
                      *, allow_kill: bool = True) -> None:
        """Остановить профиль через MLX API его рабочего пространства (ACL-1320).

        Токен — по `entry.multilogin_instance` (A = Inhouse, B = KLD), как у
        запуска. `start_profile` на уже запущенном с автоматизацией профиле
        возвращает его старый порт — то есть тот же зависший браузер, —
        поэтому сначала явная остановка. Не остановился через API — как и в
        пути «открыт руками в MLX»: закрыть Mimic только этого профиля.
        ACL-1388: `allow_kill=False` (окно, открытое руками) — не закрывать
        ничего сверх API, а отдать ошибку API наверх.
        """
        from src.meta_campaign_agent.multilogin_config import get_multilogin_instance
        from src.meta_campaign_agent.multilogin_launcher import (
            MultiloginLauncher,
            MultiloginLauncherError,
        )

        folder_id = (entry.folder_id or "").strip() or _folder_id_from_profile_dir(udd)
        launcher = MultiloginLauncher(get_multilogin_instance(entry.multilogin_instance))
        try:
            launcher.stop_profile(profile_id, folder_id=folder_id)
            log.info("%s: profile %s stopped through the MLX API", account_label, profile_id)
        except MultiloginLauncherError as exc:
            if not allow_kill:
                log.warning("%s: MLX stop failed (%s); nothing was closed", account_label, exc)
                raise
            log.warning("%s: MLX stop failed (%s); closing Mimic of this profile only", account_label, exc)
            if udd:
                _kill_chrome_for_user_data_dir(udd, profile_id, mlx_only=True)
        time.sleep(3)

    def restart_unresponsive(self, entry: AccountMapEntry, account_label: str) -> BrowserConnection:
        """Перезапустить профиль, который не отвечает (ACL-1320).

        Зовёт публикатор, когда кампания не открылась и свежая вкладка не
        помогла (`run_recorded_campaign_flow` → "profile_unresponsive").
        Остановка и запуск — через MLX API со своим токеном рабочего
        пространства, подключение — по новому порту. Простой браузер — никогда.
        """
        profile_id = entry.profile_id
        if not profile_id:
            raise RuntimeError(f"{account_label}: profile_id is required")
        # Публикатор зовёт это только для профиля, к которому робот был
        # подключён по порту автоматизации, — профиль робота.
        user_data_dir = _find_mlx_profile_dir(profile_id)
        udd = str(user_data_dir) if user_data_dir is not None else ""
        _mark_robot_profile(profile_id, udd)
        try:
            self._stop_profile(entry, account_label, profile_id, udd)
            started = self._start_via_mlx_api(entry, account_label, profile_id, udd)
            conn = self._connect_started(started, profile_id)
        except Exception as exc:
            _record_profile_restart(entry, profile_id, ok=False, where="campaign_open",
                                    error=_exc_text(exc))
            raise
        _record_profile_restart(entry, profile_id, ok=True, where="campaign_open")
        return conn

    def close_finished_profile(self, entry: AccountMapEntry, account_label: str) -> str:
        """Закрыть профиль, чьи кампании собрались чисто (ACL-1444).

        Решение «чисто ли и сохранён ли черновик» принимает публикатор; здесь —
        только «вправе ли робот закрыть это окно»: профиль, который робот сам
        поднял или к которому цеплялся по порту автоматизации, и работает
        всё тот же процесс (`_robot_profile`, как у ACL-1320/1388). Окно,
        открытое руками, не трогаем. Закрытие — мягкая остановка ЭТОГО
        профиля через MLX API, `allow_kill=False`: не остановился через API —
        окно остаётся, ничего не убиваем.

        Возвращает `closed` / `hand_opened` / `stop_failed` / `no_profile`.
        """
        profile_id = (entry.profile_id or "").strip()
        if not profile_id:
            return "no_profile"
        user_data_dir = _find_mlx_profile_dir(profile_id)
        udd = str(user_data_dir) if user_data_dir is not None else ""
        if not _robot_profile(profile_id, udd):
            log.info("%s: profile %s was not started by the robot - leaving its window open",
                     account_label, profile_id)
            return "hand_opened"
        try:
            self._stop_profile(entry, account_label, profile_id, udd, allow_kill=False)
        except Exception as exc:
            log.warning("%s: could not close the finished profile through the MLX API (%s); "
                        "the window stays open", account_label, _exc_text(exc))
            return "stop_failed"
        _ROBOT_PROFILES.pop(profile_id, None)
        self._launcher = None
        self._api_started = False
        return "closed"

    def close(self, entry: AccountMapEntry, *, keep_browser_open: bool = False) -> None:
        # Never stop the Multilogin profile — user keeps their window.
        self._launcher = None
        self._api_started = False


def stop_profile_on_request(entry: AccountMapEntry, account_label: str) -> str:
    """Кнопка «Close profile» на дешборде (ACL-1444): закрыть профиль аккаунта.

    Человек сам просит закрыть окно, поэтому здесь не важно, кто его открыл.
    Способ тот же, что у робота: мягкая остановка только этого профиля
    через MLX API его рабочего пространства, без kill (`allow_kill=False`);
    ошибка API уходит наверх, и окно остаётся как было. «Уже остановлен»
    MLX считает успехом. Работает ли на профиле задание, проверяет вызывающий.
    """
    profile_id = (entry.profile_id or "").strip()
    if not profile_id:
        raise RuntimeError(f"{account_label}: profile_id is required")
    user_data_dir = _find_mlx_profile_dir(profile_id)
    udd = str(user_data_dir) if user_data_dir is not None else ""
    MultiloginAttachOnlyProvider()._stop_profile(entry, account_label, profile_id, udd, allow_kill=False)
    _ROBOT_PROFILES.pop(profile_id, None)
    return "closed"


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
