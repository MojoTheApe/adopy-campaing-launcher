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
  «Скрыто» — только свёрнуто или закрыто другими окнами по Windows; окно
  за краем экрана — обычная парковка робота, не повод (ревью PR #420, F1).
  «Разбудили» — только если после побудки пришёл кадр rAF (F4).

Какой механизм на самом деле размораживает закрытое (не свёрнутое) окно,
ещё не проверено опытом с настоящими окнами (план — в PR ACL-1402): на
ноутбуке владельца идут живые кампании, и окна там не открываем.
"""
from __future__ import annotations

import logging
import sys
import threading
import time
import weakref
from typing import Any
from urllib.parse import urlparse

log = logging.getLogger(__name__)

#: Шаг телеметрии: окно было скрыто, робот его будил (этап «hold» модели
#: процента — правило `^window_`).
WAKE_STEP = "window_hidden_woken"

#: Сколько ждать ответа страницы на вопрос о видимости.
PROBE_S = 3.0

#: Сколько ждать кадра (`requestAnimationFrame`) после побудки: пришёл —
#: окно и правда ожило (ревью PR #420, F4), не пришёл — кадры стоят.
FRAMES_PROBE_S = 2.0

#: Будить одну вкладку не чаще, чем раз в столько секунд: ожидания крутятся
#: по кругу, а будить на каждом круге незачем.
WAKE_EVERY_S = 30.0

#: Шаг ждёт дольше этого — стоит спросить, не скрыто ли окно (ожидание
#: объявления после «Create ad», `simple_create._wait_new_ad`).
WAKE_AFTER_S = 20.0

#: Окно закрыто другими окнами, если из его части на экране не закрыто
#: меньше этой доли (от всего окна).
OCCLUDED_BELOW = 0.05

#: Окно «запарковано» за краем экрана, если на экране меньше этой доли.
#: Это обычный дом свёрнутого профиля у робота (`_restore_offscreen`,
#: решение 19.09) — не «закрыто окнами» (ревью PR #420, F1).
OFFSCREEN_BELOW = 0.05

#: Почему окно считаем скрытым → строка в панели, когда побудка помогла
#: (кадры пошли). Слова — только о том, что видели (ревью PR #420, F1/F4).
TEXTS_WOKEN = {
    "covered": "The profile window was covered by other windows — the robot woke it",
    "minimized": "The profile window was minimised — the robot restored it off-screen",
    "page_hidden": "The profile tab was hidden — the robot woke it",
}
TEXT_WOKEN = TEXTS_WOKEN["covered"]
TEXT_FRONT = "The profile window stayed hidden and did not wake — bring it to the front"

_lock = threading.Lock()


class PerPage:
    """Значение на вкладку (или контекст) без ложных совпадений по `id()`.

    `id(page)` после сборки мусора достаётся новой вкладке, а serve.py живёт
    сутками (ревью PR #420, F5): ключ — `id`, но рядом слабая ссылка, и
    запись чужой (умершей) вкладки не читается. Объект без слабых ссылок
    (заглушка `object()`) — по одному `id`, как прежде.
    """

    def __init__(self) -> None:
        self._d: dict[int, tuple[Any, Any]] = {}

    @staticmethod
    def _ref(obj):
        try:
            return weakref.ref(obj)
        except TypeError:
            return None

    def get(self, obj, default=None):
        entry = self._d.get(id(obj))
        if entry is None:
            return default
        ref, val = entry
        if ref is not None and ref() is not obj:
            self._d.pop(id(obj), None)
            return default
        return val

    def set(self, obj, val) -> None:
        self._d[id(obj)] = (self._ref(obj), val)
        if len(self._d) > 256:
            self.prune()

    def pop(self, obj, default=None):
        val = self.get(obj, default)
        self._d.pop(id(obj), None)
        return val

    def add(self, obj) -> None:
        self.set(obj, True)

    def __contains__(self, obj) -> bool:
        return self.get(obj, _MISSING) is not _MISSING

    def __len__(self) -> int:
        return len(self._d)

    def prune(self) -> None:
        for key, (ref, _val) in list(self._d.items()):
            if ref is not None and ref() is None:
                self._d.pop(key, None)

    def clear(self) -> None:
        self._d.clear()


_MISSING = object()

#: контекст → адрес CDP профиля (по нему — процесс браузера).
_CDP_URL = PerPage()
#: адрес CDP → (pid процесса браузера или None, когда искали). None не
#: держим дольше `PID_RETRY_S` (ревью PR #420, F6): порт мог ещё не слушать.
_PID: dict[str, tuple[int | None, float]] = {}
PID_RETRY_S = 15.0
#: вкладка → когда будили в последний раз.
_LAST_WAKE = PerPage()
#: вкладка → прогон, в котором её будили (окно было скрыто).
_WOKEN = PerPage()
#: вкладка → {(прогон, текст)} — строка в панели уже показана.
_TOLD = PerPage()


def _run_id() -> str:
    try:
        from src.fb_ads_rpa import run_telemetry as rt

        return rt.current_run_id() or ""
    except Exception:
        return ""


def remember_cdp_url(context, cdp_url: str) -> None:
    """Запомнить адрес CDP контекста: по порту найдётся процесс браузера."""
    if context is None or not cdp_url:
        return
    with _lock:
        _CDP_URL.set(context, str(cdp_url))


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
        url = _CDP_URL.get(page.context)
    except Exception:
        url = None
    if not url:
        return None
    cached = _PID.get(url)
    if cached is not None and (cached[0] is not None or time.time() - cached[1] < PID_RETRY_S):
        return cached[0]
    pid = None
    try:
        port = urlparse(url if "://" in url else "http://" + url).port
        if port:
            pid = _pid_listening(int(port))
    except Exception as exc:
        log.debug("browser pid lookup failed: %s", exc)
    with _lock:
        _PID[url] = (pid, time.time())
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
        out.update(onscreen=0.0, uncovered=0.0, covered=False, offscreen=False, above=0)
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
    offscreen = share_on < OFFSCREEN_BELOW
    # Закрыто окнами — только то, что на экране и под окнами; окно за краем
    # экрана — «запарковано» роботом, а не закрыто (ревью PR #420, F1).
    return {"onscreen": share_on, "uncovered": share_free, "offscreen": offscreen,
            "covered": (not offscreen) and share_free < OCCLUDED_BELOW}


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

        got = br.THROTTLE_STATUS.get(page)
        return dict(got) if got else None
    except Exception:
        return None


def frames_running(page, timeout_s: float = FRAMES_PROBE_S) -> bool | None:
    """Идут ли у вкладки кадры: пришёл ли `requestAnimationFrame` за `timeout_s`.

    Доказательство, что побудка помогла (ревью PR #420, F4): «страница
    ответила» и «троттлинг снят» кадров не гарантируют — скрытое окно
    отвечает на вопросы, а rAF у него стоит. `wait_for_function` ждёт
    обещание, которое вернул предикат, под таймаутом драйвера.
    True — кадр пришёл; False — не пришёл; None — спросить не удалось.
    """
    from src.fb_ads_rpa import page_liveness

    if page_liveness.tab_gone(page):
        return None
    try:
        handle = page.wait_for_function(
            "() => new Promise(r => requestAnimationFrame(() => r({v: true})))",
            timeout=int(timeout_s * 1000))
        try:
            got = handle.json_value()
        finally:
            try:
                handle.dispose()
            except Exception:
                pass
        return bool(isinstance(got, dict) and got.get("v") is True)
    except Exception as exc:
        if page_liveness.is_gone_error(exc):
            page_liveness.mark_gone(page)
            return None
        if "timeout" in type(exc).__name__.lower() or "Timeout" in str(exc):
            return False
        return None


def visibility_diag(page, *, answered: bool | None = None, probe: bool = True) -> dict:
    """Видимость вкладки и окна профиля одним словарём. Не бросает.

    `answered=False` — вкладка только что не ответила: страницу не
    спрашиваем снова, только Windows. `probe=False` — страницу не
    спрашиваем вовсе (опрос в цикле ожидания, ревью PR #420, F3).
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
        out["reason"] = hidden_reason(out)
        out["hidden"] = out["reason"] is not None
        out["parked"] = _win_known(out) and bool((out.get("win32") or {}).get("offscreen"))
    except Exception as exc:
        out["error"] = str(exc)[:80]
    return out


def _win_known(diag: dict) -> bool:
    win = diag.get("win32") or {}
    return bool(win.get("found")) and not win.get("error")


def hidden_reason(diag: dict) -> str | None:
    """Почему вкладка скрыта: "minimized" / "covered" / "page_hidden"; None — не скрыта.

    Windows знает окно — верим ему: свёрнуто или закрыто другими окнами.
    Окно за краем экрана — обычная парковка робота, не «скрыто» (F1).
    Страница «hidden» при окне на экране — это фоновая вкладка (робот
    возвращает вперёд вкладку человека, F2), а не закрытое окно. Только
    когда Windows ничего не знает, решает сама страница.
    """
    win = diag.get("win32") or {}
    if _win_known(diag):
        if win.get("minimized"):
            return "minimized"
        if win.get("covered"):
            return "covered"
        return None
    page = diag.get("page") or {}
    if page.get("visibility") == "hidden":
        return "page_hidden"
    return None


def is_hidden(diag: dict) -> bool:
    """Скрыта ли вкладка или её окно (`hidden_reason`)."""
    return hidden_reason(diag) is not None


def _tell(page, text: str) -> None:
    """Строка в панели задания — одна на вкладку и текст за прогон."""
    key = (_run_id(), text)
    with _lock:
        told = _TOLD.get(page)
        if told is None:
            told = set()
            _TOLD.set(page, told)
        if key in told:
            return
        told.add(key)
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


def _keep_parked_alive(page) -> list[str]:
    """Окно запарковано за краем экрана, вкладка отвечает: скринкаст, если
    его ещё нет (свежая вкладка). Молча — ничего не скрывали (F1)."""
    from src.fb_ads_rpa import browser as br

    try:
        if br.ensure_renderer_kept_active(page.context, page):
            return ["screencast_started"]
    except Exception as exc:
        log.debug("parked keep-alive: %s", exc)
    return []


def wake_if_hidden(page, *, where: str, waited_s: float = 0.0,
                   answered: bool | None = None, force: bool = False,
                   probe: bool = True) -> dict:
    """Ожидание затянулось: если окно профиля скрыто — разбудить его, не
    поднимая на передний план. Не бросает.

    Возвращает {"hidden", "reason", "woke" (после побудки пошли кадры),
    "frames", "actions", "before", "after"}; `{"skipped": …}` — будить не
    стали (вкладки нет, недавно будили). Строка `window_hidden_woken` и
    строка в панели — только когда окно и правда было скрыто
    (`hidden_reason`). `probe=False` — страницу о видимости не спрашивать,
    решает Windows (опрос в цикле ожидания).
    """
    from src.fb_ads_rpa import page_liveness

    try:
        if page_liveness.tab_gone(page):
            return {"skipped": "gone", "hidden": False, "woke": False}
        now = time.time()
        with _lock:
            last = _LAST_WAKE.get(page)
            if not force and last is not None and now - last < WAKE_EVERY_S:
                return {"skipped": "recent", "hidden": False, "woke": False}
            _LAST_WAKE.set(page, now)
        before = visibility_diag(page, answered=answered, probe=probe)
        reason = before.get("reason")
        if not before.get("hidden"):
            out = {"hidden": False, "woke": False, "before": before}
            if before.get("parked") and (before.get("page") or {}).get("answered") is True:
                out["actions"] = _keep_parked_alive(page)
            return out
        with _lock:
            _WOKEN.set(page, _run_id())
        actions = _apply_wake(page, before)
        frames = None
        if "throttling_off" in actions:
            frames = frames_running(page)
        after = visibility_diag(page, probe=frames is not None)
        # «Разбудили» — только когда пришёл кадр (F4), а не когда команды ушли.
        woke = frames is True
        out = {"hidden": True, "reason": reason, "woke": woke, "frames": frames,
               "actions": actions, "before": before, "after": after}
        try:
            from src.fb_ads_rpa import run_telemetry as rt

            rt.record(WAKE_STEP, woke, note=f"{where[:50]}:{reason}",
                      diag={"where": where[:80], "reason": reason,
                            "waited_s": round(float(waited_s or 0), 1),
                            "actions": actions, "frames": frames,
                            "before": before, "after": after})
        except Exception:
            pass
        _tell(page, TEXTS_WOKEN.get(reason, TEXT_WOKEN) if woke else TEXT_FRONT)
        return out
    except Exception as exc:
        log.debug("wake_if_hidden failed: %s", exc)
        return {"error": str(exc)[:80], "hidden": False, "woke": False}


def wake_did_not_help(page) -> None:
    """Побудили, а ожидание всё равно кончилось ничем: просим человека."""
    _tell(page, TEXT_FRONT)


def woken_recently(page) -> bool:
    """Будили ли эту вкладку в текущем прогоне — для слов «не помогло»."""
    run = _WOKEN.get(page)
    return run is not None and run == _run_id()


def _reset_for_tests() -> None:
    with _lock:
        _CDP_URL.clear()
        _PID.clear()
        _LAST_WAKE.clear()
        _WOKEN.clear()
        _TOLD.clear()
