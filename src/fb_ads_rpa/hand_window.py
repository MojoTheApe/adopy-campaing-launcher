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
"""
from __future__ import annotations

import logging
import os
import sys
import time
from dataclasses import dataclass
from typing import Iterable

log = logging.getLogger(__name__)

#: Сколько секунд браузер профиля должен уже работать: за это время
#: Multilogin сохраняет свежий вход в Facebook.
MIN_AGE_S = 15 * 60

#: Сколько секунд на машине не должно быть ввода с клавиатуры/мыши.
MIN_IDLE_S = 60

ENV_ENABLED = "FB_RPA_REOPEN_HAND_WINDOW"
ENV_MIN_AGE = "FB_RPA_HAND_WINDOW_MIN_AGE_S"
ENV_MIN_IDLE = "FB_RPA_HAND_WINDOW_MIN_IDLE_S"

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


def enabled() -> bool:
    return (os.environ.get(ENV_ENABLED) or "").strip().lower() not in ("0", "false", "no", "off")


# ── Слои, которые тесты подменяют ─────────────────────────────────────────

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
