"""Можно ли закрыть окно профиля, открытое руками (ACL-1388).

С ACL-1320 окно профиля, открытое руками, робот не трогает: остановка
однажды потеряла только что созданную сессию Facebook, которую Multilogin
ещё не сохранил (живой прогон 26.09). Цена — задание аккаунта кончается
`profile_open_by_hand`, и человек сам ищет окно и жмёт Publish заново
(30.09 06:49 UTC так встал Facebook_6540 в начале «Publish all»).

Решение владельца 30.09: закрывать такое окно самому, но только когда это
безопасно — ВСЕ условия сразу:

1. браузер профиля работает не меньше `MIN_AGE_S` (15 минут — Multilogin
   успел сохранить свежий вход);
2. окно профиля не на переднем плане, и на этой машине не было ввода с
   клавиатуры/мыши не меньше `MIN_IDLE_S` (60 секунд — в нём сейчас никто
   не работает).

Любой сбой чтения (нет процесса, не Windows, Win32 API не ответил) — «не
безопасно»: остановка как раньше. Сама остановка — в `browser.py`, только
мягкая через MLX API этого профиля.

Пороги — константы; на отдельной машине их можно сдвинуть переменными
окружения `FB_RPA_HAND_WINDOW_MIN_AGE_S` / `FB_RPA_HAND_WINDOW_MIN_IDLE_S`,
а `FB_RPA_REOPEN_HAND_WINDOW=0` возвращает прежнее поведение (всегда стоп).

ACL-1427: публикацию начинает щелчок Publish, и он же сбрасывает счётчик
тишины ввода — проверка через пару секунд почти всегда видела «был ввод».
Теперь, когда не прошло ТОЛЬКО это условие, робот ждёт (`wait_until_safe`)
до `IDLE_WAIT_S` (2 минуты, `FB_RPA_HAND_WINDOW_IDLE_WAIT_S`), перепроверяя
ВСЕ условия каждые `IDLE_POLL_S` секунд. Другие условия не ждутся: их провал
— остановка сразу. Stop задания прерывает ожидание (`watch_cancel`).
"""
from __future__ import annotations

import logging
import os
import sys
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Callable, Iterable

log = logging.getLogger(__name__)

#: Сколько секунд браузер профиля должен уже работать: за это время
#: Multilogin сохраняет свежий вход в Facebook.
MIN_AGE_S = 15 * 60

#: Сколько секунд на машине не должно быть ввода с клавиатуры/мыши.
MIN_IDLE_S = 60

ENV_ENABLED = "FB_RPA_REOPEN_HAND_WINDOW"
ENV_MIN_AGE = "FB_RPA_HAND_WINDOW_MIN_AGE_S"
ENV_MIN_IDLE = "FB_RPA_HAND_WINDOW_MIN_IDLE_S"

#: ACL-1427: сколько секунд ждать тишины ввода, когда не прошло только это
#: условие (щелчок Publish сам сбрасывает счётчик). Дольше — прежний стоп.
IDLE_WAIT_S = 120

#: Как часто перепроверять условия во время ожидания.
IDLE_POLL_S = 3.0

#: Как часто во время ожидания смотреть на Stop задания.
CANCEL_SLICE_S = 0.5

ENV_IDLE_WAIT = "FB_RPA_HAND_WINDOW_IDLE_WAIT_S"

# Причины — устойчивые метки для телеметрии и Sentry.
SAFE = "safe"
DISABLED = "disabled"
NO_PROCESS = "no_process"
NOT_WINDOWS = "not_windows"
AGE_UNKNOWN = "age_unknown"
TOO_YOUNG = "too_young"
FOREGROUND_UNKNOWN = "foreground_unknown"
IN_FOREGROUND = "in_foreground"
IDLE_UNKNOWN = "idle_unknown"
RECENT_INPUT = "recent_input"


@dataclass(frozen=True)
class Verdict:
    """Итог проверки: можно ли закрыть окно, почему, и что намерили."""

    safe: bool
    reason: str
    age_s: float | None = None
    idle_s: float | None = None

    def diag(self) -> dict:
        return {"reason": self.reason,
                "age_s": None if self.age_s is None else round(self.age_s, 1),
                "idle_s": None if self.idle_s is None else round(self.idle_s, 1)}


def _env_seconds(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return float(default)
    try:
        value = float(raw)
    except ValueError:
        return float(default)
    return value if value >= 0 else float(default)


def min_age_s() -> float:
    return _env_seconds(ENV_MIN_AGE, MIN_AGE_S)


def min_idle_s() -> float:
    return _env_seconds(ENV_MIN_IDLE, MIN_IDLE_S)


def idle_wait_s() -> float:
    return _env_seconds(ENV_IDLE_WAIT, IDLE_WAIT_S)


def enabled() -> bool:
    return (os.environ.get(ENV_ENABLED) or "").strip().lower() not in ("0", "false", "no", "off")


# ── Слои, которые тесты подменяют ─────────────────────────────────────────

def _monotonic() -> float:
    return time.monotonic()


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _is_windows() -> bool:
    return sys.platform == "win32"


def _process_create_times(pids: Iterable[int]) -> list[float]:
    """Время запуска (epoch) главных процессов профиля. Сбой — исключение."""
    import psutil  # type: ignore

    return [float(psutil.Process(int(pid)).create_time()) for pid in pids]


def _ancestors(pid: int) -> list[int]:
    """Родители процесса вверх по цепочке. Сбой — исключение."""
    import psutil  # type: ignore

    return [p.pid for p in psutil.Process(int(pid)).parents()]


def _foreground_pid() -> int:
    """Процесс окна переднего плана; 0 — окна переднего плана нет (например,
    экран заблокирован). Сбой Win32 — исключение."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return 0
    pid = wintypes.DWORD(0)
    thread = user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not thread or not pid.value:
        raise OSError(f"GetWindowThreadProcessId failed ({ctypes.get_last_error()})")
    return int(pid.value)


def _idle_seconds() -> float:
    """Сколько секунд на машине не было ввода (`GetLastInputInfo`). Сбой — исключение."""
    import ctypes
    from ctypes import wintypes

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetTickCount.restype = wintypes.DWORD
    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(LASTINPUTINFO)
    if not user32.GetLastInputInfo(ctypes.byref(info)):
        raise OSError(f"GetLastInputInfo failed ({ctypes.get_last_error()})")
    # Оба счётчика 32-битные и переполняются раз в ~49 дней — разность по модулю.
    elapsed_ms = (int(kernel32.GetTickCount()) - int(info.dwTime)) & 0xFFFFFFFF
    return elapsed_ms / 1000.0


# ── Проверка ──────────────────────────────────────────────────────────────

def _belongs(pid: int, pids: frozenset[int]) -> bool:
    """Процесс окна — это профиль или его потомок (сбой — исключение)."""
    if pid in pids:
        return True
    return any(p in pids for p in _ancestors(pid))


def assess(pids: Iterable[int], *, now: float | None = None) -> Verdict:
    """Можно ли закрыть окно профиля, которым владеют процессы `pids`.

    Порядок — от дешёвого к дорогому; первый же провал — «не безопасно».
    """
    if not enabled():
        return Verdict(False, DISABLED)
    pids = frozenset(int(p) for p in (pids or ()))
    if not pids:
        return Verdict(False, NO_PROCESS)
    if not _is_windows():
        return Verdict(False, NOT_WINDOWS)
    try:
        created = _process_create_times(pids)
    except Exception as exc:
        log.info("hand-opened window: process age unreadable (%s)", exc)
        return Verdict(False, AGE_UNKNOWN)
    if not created:
        return Verdict(False, AGE_UNKNOWN)
    # Самый молодой главный процесс: окно, открытое позже всех, решает.
    age = (time.time() if now is None else now) - max(created)
    if age < min_age_s():
        return Verdict(False, TOO_YOUNG, age_s=age)
    try:
        fg = _foreground_pid()
        in_front = bool(fg) and _belongs(fg, pids)
    except Exception as exc:
        log.info("hand-opened window: foreground window unreadable (%s)", exc)
        return Verdict(False, FOREGROUND_UNKNOWN, age_s=age)
    if in_front:
        return Verdict(False, IN_FOREGROUND, age_s=age)
    try:
        idle = float(_idle_seconds())
    except Exception as exc:
        log.info("hand-opened window: last input unreadable (%s)", exc)
        return Verdict(False, IDLE_UNKNOWN, age_s=age)
    if idle < min_idle_s():
        return Verdict(False, RECENT_INPUT, age_s=age, idle_s=idle)
    return Verdict(True, SAFE, age_s=age, idle_s=idle)


# ── Ожидание тишины ввода (ACL-1427) ──────────────────────────────────────

# Итоги ожидания — устойчивые метки телеметрии.
WAIT_NONE = "no_wait"          # ждать не пришлось (сразу безопасно или сразу стоп)
WAIT_IDLE = "idle"             # дождались тишины, остальные условия держатся
WAIT_TIMEOUT = "timeout"       # ввод шёл всё время ожидания
WAIT_GATE = "gate_failed"      # во время ожидания провалилось другое условие
WAIT_CANCELLED = "cancelled"   # задание остановили (Stop)

#: Проверка «задание остановлено» — её ставит публикатор вокруг открытия
#: браузера (`watch_cancel`); вне задания — None, ожидание не прерывается.
_cancel_check: ContextVar[Callable[[], bool] | None] = ContextVar("hand_window_cancel", default=None)


@contextmanager
def watch_cancel(check: Callable[[], bool]):
    """Пока открыт этот блок, ожидание тишины прерывается, когда `check()` — да."""
    token = _cancel_check.set(check)
    try:
        yield
    finally:
        _cancel_check.reset(token)


def _cancelled() -> bool:
    check = _cancel_check.get()
    if check is None:
        return False
    try:
        return bool(check())
    except Exception:
        return False


@dataclass(frozen=True)
class Wait:
    """Итог `wait_until_safe`: последний вердикт, сколько ждали и чем кончилось."""

    verdict: Verdict
    waited_s: float
    outcome: str

    def diag(self) -> dict:
        return {"waited_s": round(self.waited_s, 1), "wait_outcome": self.outcome}


def wait_until_safe(pids: Iterable[int], *,
                    on_wait: Callable[[Verdict], None] | None = None) -> Wait:
    """`assess`, а если не прошла только тишина ввода — ждать её (ACL-1427).

    Ждём не дольше `idle_wait_s()`, каждые `IDLE_POLL_S` секунд заново
    проверяя ВСЕ условия: стало безопасно — `WAIT_IDLE`; провалилось другое
    условие — сразу стоп (`WAIT_GATE`); время вышло — стоп с `RECENT_INPUT`.
    `on_wait` зовётся один раз, когда ожидание начинается (строка в панели).
    Stop задания (`watch_cancel`) замечается за `CANCEL_SLICE_S`.
    """
    pids = frozenset(int(p) for p in (pids or ()))
    verdict = assess(pids)
    if verdict.safe or verdict.reason != RECENT_INPUT:
        return Wait(verdict, 0.0, WAIT_NONE)
    limit = idle_wait_s()
    if limit <= 0:
        return Wait(verdict, 0.0, WAIT_NONE)
    started = _monotonic()
    slept = 0.0

    def waited() -> float:
        # Сумма заказанных пауз — страховка, если часы стоят (подменённый сон).
        return max(_monotonic() - started, slept)

    log.info("hand-opened window: input %.0f s ago - waiting up to %.0f s for the machine to go idle",
             verdict.idle_s or 0.0, limit)
    if on_wait is not None:
        try:
            on_wait(verdict)
        except Exception:
            pass
    while True:
        if _cancelled():
            return Wait(verdict, waited(), WAIT_CANCELLED)
        left = limit - waited()
        if left <= 0:
            return Wait(verdict, waited(), WAIT_TIMEOUT)
        # До следующей проверки: не дольше шага и не дольше, чем нужно до
        # нужной тишины, если ввода больше не будет.
        need = max(min_idle_s() - (verdict.idle_s or 0.0), 0.0)
        pause = min(IDLE_POLL_S, left, need) if need > 0 else min(IDLE_POLL_S, left)
        end = waited() + pause
        while True:
            if _cancelled():
                return Wait(verdict, waited(), WAIT_CANCELLED)
            rest = end - waited()
            if rest <= 0:
                break
            step = min(CANCEL_SLICE_S, rest)
            _sleep(step)
            slept += step
        verdict = assess(pids)
        if verdict.safe:
            return Wait(verdict, waited(), WAIT_IDLE)
        if verdict.reason != RECENT_INPUT:
            return Wait(verdict, waited(), WAIT_GATE)
