"""Окно профиля закрыто другими окнами — видно ли это и как разбудить (ACL-1402).

Живой прогон 30.09 (Facebook_1443, fba2c3f5): `website_selected` 12:20:11 →
`conversion_event` 12:25:28 UTC при обычных 40 с – 2,5 мин; точка сборки
стала оранжевой на 17 %, и сборка «сама пошла», как только владелец открыл
окно аккаунта. Окно не было свёрнуто — его закрывали другие окна. В
телеметрии про видимость было только одно чтение при подключении.

Chromium на Windows сам считает перекрытие окна (native window occlusion):
закрытое окно — `document.visibilityState == "hidden"`, кадры (rAF) стоят,
таймеры придержаны, а ожидания Playwright (видимость, «стабильность»
элемента) ждут именно кадров. `_disable_background_throttling` подделывает
фокус и держит страницу «active», но видимость не трогает.

Здесь:

* `visibility_diag(page)` — что знает страница (`visibilityState`,
  `hasFocus()` — ограниченным по времени вопросом), что знает Windows о
  окне профиля (свёрнуто, видимо, на переднем плане, какая доля окна на
  экране и не закрыта окнами выше по z-порядку) и удался ли
  `_disable_background_throttling` на этой вкладке. Только чтение: ни
  одного окна не создаём, ни одно не двигаем и не поднимаем.
* `wake_if_hidden(page, where=…)` — ожидание затянулось, а окно скрыто:
  будим без переднего плана — свёрнутое окно в обычное состояние за краем
  экрана (как на границе раздела), троттлинг снимается заново, скринкаст
  держит кадры. Никаких `bring_to_front` / `SetForegroundWindow`: решение
  владельца — окна профилей не всплывают поверх его работы. Строка
  телеметрии `window_hidden_woken`, строка в панели задания (English).

Какой механизм на самом деле размораживает закрытое (не свёрнутое) окно,
ещё не проверено опытом с настоящими окнами (план — в PR ACL-1402): на
ноутбуке владельца идут живые кампании, и окна там не открываем.
"""
from __future__ import annotations

import logging
import sys
import threading
import time
from typing import Any
from urllib.parse import urlparse

log = logging.getLogger(__name__)

#: Шаг телеметрии: окно было скрыто, робот его будил (этап «hold» модели
#: процента — правило `^window_`).
WAKE_STEP = "window_hidden_woken"

#: Сколько ждать ответа страницы на вопрос о видимости.
PROBE_S = 3.0

#: Будить одну вкладку не чаще, чем раз в столько секунд: ожидания крутятся
#: по кругу, а будить на каждом круге незачем.
WAKE_EVERY_S = 30.0

#: Шаг ждёт дольше этого — стоит спросить, не скрыто ли окно (ожидание
#: объявления после «Create ad», `simple_create._wait_new_ad`).
WAKE_AFTER_S = 20.0

#: Окно считается закрытым, если на экране видно меньше этой доли.
OCCLUDED_BELOW = 0.05

TEXT_WOKEN = "The profile window was hidden behind other windows — the robot woke it"
TEXT_FRONT = "The profile window was hidden behind other windows — bring it to the front"

_lock = threading.Lock()
#: id(context) → адрес CDP профиля (по нему — процесс браузера).
_CDP_URL: dict[int, str] = {}
#: адрес CDP → pid процесса браузера (None — не нашли).
_PID: dict[str, int | None] = {}
#: id(page) → когда будили в последний раз.
_LAST_WAKE: dict[int, float] = {}
#: id(page) вкладок, которые за прогон будили (окно было скрыто).
_WOKEN: set[int] = set()
#: (run, id(page), текст) — строка в панели уже показана.
_TOLD: set[tuple] = set()


def remember_cdp_url(context, cdp_url: str) -> None:
    """Запомнить адрес CDP контекста: по порту найдётся процесс браузера."""
    if context is None or not cdp_url:
        return
    with _lock:
        _CDP_URL[id(context)] = str(cdp_url)


# ── Слои, которые тесты подменяют ─────────────────────────────────────────

def _is_windows() -> bool:
    return sys.platform == "win32"


def _pid_listening(port: int) -> int | None:
    """Процесс, слушающий 127.0.0.1:`port` (порт отладки браузера профиля)."""
    import psutil  # type: ignore

    for conn in psutil.net_connections(kind="tcp"):
        try:
            if conn.status == psutil.CONN_LISTEN and conn.laddr and conn.laddr.port == port and conn.pid:
                return int(conn.pid)
        except Exception:
            continue
    return None


def browser_pid(page) -> int | None:
    """pid процесса браузера вкладки; None — не знаем (не Windows, нет адреса)."""
    try:
        url = _CDP_URL.get(id(page.context))
    except Exception:
        url = None
    if not url:
        return None
    if url in _PID:
        return _PID[url]
    pid = None
    try:
        port = urlparse(url if "://" in url else "http://" + url).port
        if port:
            pid = _pid_listening(int(port))
    except Exception as exc:
        log.debug("browser pid lookup failed: %s", exc)
    with _lock:
        _PID[url] = pid
    return pid


def _win32_windows_state(pid: int) -> dict:
    """Окно браузера `pid` глазами Windows. Только чтение. Сбой — исключение.

    Окно — самое большое видимое верхнее окно процесса с заголовком (у
    профиля Multilogin окно одно). Доля на экране — сетка точек по окну:
    точка в пределах виртуального экрана и не под видимым, не свёрнутым,
    не «скрытым DWM» окном выше по z-порядку.
    """
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    dwmapi = None
    try:
        dwmapi = ctypes.WinDLL("dwmapi")
    except Exception:
        dwmapi = None
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetWindow.restype = wintypes.HWND
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]

    def rect_of(hwnd) -> tuple[int, int, int, int] | None:
        r = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
            return None
        return (r.left, r.top, r.right, r.bottom)

    def cloaked(hwnd) -> bool:
        if dwmapi is None:
            return False
        val = ctypes.c_int(0)
        try:
            # DWMWA_CLOAKED = 14: окно другого рабочего стола / скрытое UWP.
            if dwmapi.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(val), ctypes.sizeof(val)) == 0:
                return bool(val.value)
        except Exception:
            pass
        return False

    mine: list = []
    proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def collect(hwnd, _lparam):
        owner = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd) and user32.GetWindowTextLengthW(hwnd) > 0:
            rect = rect_of(hwnd)
            if rect:
                mine.append((hwnd, rect))
        return True

    user32.EnumWindows(proc(collect), 0)
    if not mine:
        return {"found": False}
    hwnd, rect = max(mine, key=lambda it: (it[1][2] - it[1][0]) * (it[1][3] - it[1][1]))
    minimized = bool(user32.IsIconic(hwnd))
    out: dict[str, Any] = {"found": True, "windows": len(mine), "minimized": minimized,
                           "visible": bool(user32.IsWindowVisible(hwnd)),
                           "foreground": user32.GetForegroundWindow() == hwnd}
    if minimized:
        out.update(onscreen=0.0, uncovered=0.0, occluded=True, above=0)
        return out
    # Окна выше по z-порядку, которые могут закрывать наше.
    above: list[tuple[int, int, int, int]] = []
    other = user32.GetWindow(hwnd, 3)  # GW_HWNDPREV
    guard = 0
    while other and guard < 2000:
        guard += 1
        if (user32.IsWindowVisible(other) and not user32.IsIconic(other) and not cloaked(other)):
            r = rect_of(other)
            if r and r[2] > rect[0] and r[0] < rect[2] and r[3] > rect[1] and r[1] < rect[3]:
                above.append(r)
        other = user32.GetWindow(other, 3)
    vx, vy = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)
    vw, vh = user32.GetSystemMetrics(78), user32.GetSystemMetrics(79)
    out.update(screen_share(rect, above, (vx, vy, vx + vw, vy + vh)))
    out["above"] = len(above)
    return out


def screen_share(rect, above, screen, cols: int = 16, rows: int = 12) -> dict:
    """Доля окна `rect` на экране `screen` и не под окнами `above` (сетка)."""
    left, top, right, bottom = rect
    w, h = max(right - left, 1), max(bottom - top, 1)
    total = onscreen = uncovered = 0
    for i in range(cols):
        x = left + (i + 0.5) * w / cols
        for j in range(rows):
            y = top + (j + 0.5) * h / rows
            total += 1
            if not (screen[0] <= x < screen[2] and screen[1] <= y < screen[3]):
                continue
            onscreen += 1
            if not any(a[0] <= x < a[2] and a[1] <= y < a[3] for a in above):
                uncovered += 1
    share_on = round(onscreen / total, 2)
    share_free = round(uncovered / total, 2)
    return {"onscreen": share_on, "uncovered": share_free, "occluded": share_free < OCCLUDED_BELOW}


def win32_state(page) -> dict:
    """Окно профиля глазами Windows; {} — не знаем (не Windows, нет процесса)."""
    if not _is_windows():
        return {}
    pid = browser_pid(page)
    if not pid:
        return {}
    try:
        return _win32_windows_state(pid)
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {str(exc)[:60]}"}


def page_visibility(page, timeout_s: float = PROBE_S) -> dict:
    """`visibilityState` и `hasFocus()` ограниченным вопросом.

    `wait_for_function` — вызов с таймаутом, который соблюдает драйвер
    (как `meta_dom._eval_bounded`): на зависшей вкладке — «не ответила»
    через `timeout_s`, а не вечное ожидание.
    """
    from src.fb_ads_rpa import page_liveness

    if page_liveness.tab_gone(page):
        return {"answered": None}
    try:
        handle = page.wait_for_function(
            "() => ({v: {visibility: document.visibilityState, focus: document.hasFocus()}})",
            timeout=int(timeout_s * 1000))
        try:
            got = handle.json_value()
        finally:
            try:
                handle.dispose()
            except Exception:
                pass
        v = got.get("v") if isinstance(got, dict) else None
        if isinstance(v, dict):
            return {"answered": True, "visibility": v.get("visibility"), "focus": v.get("focus")}
        return {"answered": None}
    except Exception as exc:
        if page_liveness.is_gone_error(exc):
            page_liveness.mark_gone(page)
            return {"answered": None}
        if "timeout" in type(exc).__name__.lower() or "Timeout" in str(exc):
            return {"answered": False}
        return {"answered": None}


def throttle_status(page) -> dict | None:
    """Удался ли `_disable_background_throttling` на этой вкладке (последний вызов)."""
    try:
        from src.fb_ads_rpa import browser as br

        got = br.THROTTLE_STATUS.get(id(page))
        return dict(got) if got else None
    except Exception:
        return None


def visibility_diag(page, *, answered: bool | None = None, probe: bool = True) -> dict:
    """Видимость вкладки и окна профиля одним словарём. Не бросает.

    `answered=False` — вкладка только что не ответила: страницу не
    спрашиваем снова, только Windows.
    """
    out: dict[str, Any] = {}
    try:
        if probe and answered is not False:
            out["page"] = page_visibility(page)
        else:
            out["page"] = {"answered": answered}
        win = win32_state(page)
        if win:
            out["win32"] = win
        out["throttle"] = throttle_status(page)
        out["hidden"] = is_hidden(out)
    except Exception as exc:
        out["error"] = str(exc)[:80]
    return out


def is_hidden(diag: dict) -> bool:
    """Скрыта ли вкладка или её окно: страница сказала `hidden`, или Windows —
    свёрнуто / закрыто окнами / за краем экрана."""
    page = diag.get("page") or {}
    win = diag.get("win32") or {}
    return bool(page.get("visibility") == "hidden" or win.get("minimized") or win.get("occluded"))


def _tell(page, text: str) -> None:
    """Строка в панели задания — одна на вкладку и текст за прогон."""
    try:
        from src.fb_ads_rpa import run_telemetry as rt

        run = rt.current_run_id()
    except Exception:
        run = ""
    key = (run, id(page), text)
    with _lock:
        if key in _TOLD:
            return
        _TOLD.add(key)
    try:
        from src.fb_ads_rpa.creative_compress import report_notice

        report_notice(text)
    except Exception:
        pass


def _apply_wake(page, before: dict) -> list[str]:
    """Будим без переднего плана. Что сделали — списком меток."""
    from src.fb_ads_rpa import browser as br

    done: list[str] = []
    context = page.context
    win = before.get("win32") or {}
    answered = (before.get("page") or {}).get("answered")
    if answered is not True:
        # Не ответила: CDP-сессия на самой вкладке может висеть (ACL-1320,
        # 197 с). Свёрнутое окно выводим сессией браузера — её команды
        # обрабатывает сам браузер, без рендерера; затем спрашиваем снова.
        if win.get("minimized"):
            try:
                if [d for d in br._unminimize_before_attach(context.browser) if "window" in d]:
                    done.append("unminimized_offscreen")
            except Exception as exc:
                done.append("unminimize_error:" + type(exc).__name__)
        from src.fb_ads_rpa import page_liveness

        if page_liveness.answers(page, 5.0) is not True:
            return done
        done.append("answered_again")
    else:
        try:
            if br._restore_offscreen(context, page):
                done.append("restored_offscreen")
        except Exception as exc:
            log.debug("wake restore: %s", exc)
    br._disable_background_throttling(context, page)
    done.append("throttling_off")
    if br.ensure_renderer_kept_active(context, page):
        done.append("screencast_started")
    return done


def wake_if_hidden(page, *, where: str, waited_s: float = 0.0,
                   answered: bool | None = None, force: bool = False) -> dict:
    """Ожидание затянулось: если окно профиля скрыто — разбудить его, не
    поднимая на передний план. Не бросает.

    Возвращает {"hidden", "woke" (вкладка после побудки ответила), "actions",
    "before", "after"}; `{"skipped": …}` — будить не стали (вкладки нет,
    недавно будили). Строка `window_hidden_woken` и строка в панели — только
    когда окно и правда было скрыто.
    """
    from src.fb_ads_rpa import page_liveness

    try:
        if page_liveness.tab_gone(page):
            return {"skipped": "gone", "hidden": False, "woke": False}
        now = time.time()
        with _lock:
            last = _LAST_WAKE.get(id(page))
            if not force and last is not None and now - last < WAKE_EVERY_S:
                return {"skipped": "recent", "hidden": False, "woke": False}
            _LAST_WAKE[id(page)] = now
        before = visibility_diag(page, answered=answered)
        if not before.get("hidden"):
            return {"hidden": False, "woke": False, "before": before}
        with _lock:
            _WOKEN.add(id(page))
        actions = _apply_wake(page, before)
        after = visibility_diag(page, probe=True)
        woke = (after.get("page") or {}).get("answered") is True and "throttling_off" in actions
        out = {"hidden": True, "woke": woke, "actions": actions, "before": before, "after": after}
        try:
            from src.fb_ads_rpa import run_telemetry as rt

            rt.record("window_hidden_woken", woke, note=where[:60],
                      diag={"where": where[:80], "waited_s": round(float(waited_s or 0), 1),
                            "actions": actions, "before": before, "after": after})
        except Exception:
            pass
        _tell(page, TEXT_WOKEN if woke else TEXT_FRONT)
        return out
    except Exception as exc:
        log.debug("wake_if_hidden failed: %s", exc)
        return {"error": str(exc)[:80], "hidden": False, "woke": False}


def wake_did_not_help(page) -> None:
    """Побудили, а ожидание всё равно кончилось ничем: просим человека."""
    _tell(page, TEXT_FRONT)


def woken_recently(page) -> bool:
    """Будили ли эту вкладку (за прогон) — для слов «не помогло»."""
    return id(page) in _WOKEN


def _reset_for_tests() -> None:
    with _lock:
        _CDP_URL.clear()
        _PID.clear()
        _LAST_WAKE.clear()
        _WOKEN.clear()
        _TOLD.clear()
