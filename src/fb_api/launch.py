"""Параллельный запуск профилей: темп стартов MLX и «холодный» бюджет CDP.

Живой TEST9095y, 22.10 12:08–12:11: Check plan по трём аккаунтам поднял три
профиля за 40 с, и все три упали. Место по памяти (`publish_jobs._slots`,
потолок 2) работало — профилей разом было не больше двух. Упало другое:

* два запроса MLX `start` шли одновременно (12:08:18 и 12:08:29), первый MLX
  оборвал через 22 с — `ConnectionAbortedError(10053)`. Профиль при этом
  ПОДНЯЛСЯ (его остановили только в 12:11:46): окно без места в очереди;
* холодный Chrome не успел ответить на `Page.enable` за 10 с.

Отсюда три правила, общие для всех путей API, которые поднимают профиль
(чтения, Check plan, Publish, «Open profiles for today»):

* `StartPacer` — сколько запросов MLX `start` идут разом
  (`ACL_MLX_PARALLEL_STARTS`, по умолчанию 1) и пауза перед стартом, которому
  пришлось ждать предыдущий (`ACL_MLX_START_GAP_S`, 3 с). Одиночный старт не
  ждёт ничего. Профили после старта по-прежнему работают параллельно —
  сколько разом, решает место по памяти (`ACL_MAX_PROFILES`).
* `is_launcher_connection_error` — обрыв соединения с MLX launcher
  (10053/10054) на старте: один отложенный повтор у `profile.open_profile`.
  Таймаут, 4xx/5xx, вход в прокси, потеря сессии MLX — не повторяются.
* холодный бюджет: порт профиля, который мы только что подняли, отмечается
  (`mark_started`), и первые вызовы CDP к нему (`connect`, `Page.enable`, …,
  `PageTab.open`) получают `ACL_CDP_COLD_START_S` (40 с) вместо 10 с. После
  первой удачной вкладки отметка снимается: дальше обычные сроки.

Телеметрия (журнал пилота `fb_api_pilot.jsonl`): `fb_api_mlx_start` — каждый
старт (сколько шёл, сколько ждал очереди, сколько стартов было разом, повтор,
вид отказа) и `fb_api_cold_open` — сроки первых вызовов CDP. По ним владелец
подбирает потолок и темп на своей машине; живых замеров код сам не делает.
"""
from __future__ import annotations

import os
import threading
import time
import urllib.parse
from contextlib import contextmanager
from typing import Any, Callable, Iterator

PARALLEL_STARTS_ENV = "ACL_MLX_PARALLEL_STARTS"
DEFAULT_PARALLEL_STARTS = 1
START_GAP_ENV = "ACL_MLX_START_GAP_S"
DEFAULT_START_GAP_S = 3.0
#: Пауза перед единственным повтором старта после обрыва соединения с MLX.
START_RETRY_DELAY_S = 5.0

COLD_START_ENV = "ACL_CDP_COLD_START_S"
DEFAULT_COLD_START_S = 40.0
#: Сколько секунд после старта порт считается «холодным», если вкладку так и не открыли.
COLD_WINDOW_S = 180.0


def _env_number(name: str, default: float, lo: float, hi: float) -> float:
    try:
        value = float(str(os.environ.get(name, "") or default).replace(",", "."))
    except ValueError:
        value = default
    return max(lo, min(hi, value))


def parallel_starts() -> int:
    return int(_env_number(PARALLEL_STARTS_ENV, DEFAULT_PARALLEL_STARTS, 1, 4))


def start_gap_s() -> float:
    return _env_number(START_GAP_ENV, DEFAULT_START_GAP_S, 0.0, 30.0)


def cold_start_s() -> float:
    return _env_number(COLD_START_ENV, DEFAULT_COLD_START_S, 10.0, 120.0)


def _record(event: dict) -> None:
    try:
        from src.fb_api import telemetry as _tm

        _tm.record(event)
    except Exception:
        pass


# ───────────────────────── темп стартов MLX ─────────────────────────

class StartPacer:
    """Не больше `limit()` запросов MLX `start` разом; ждавший — ещё `gap()` секунд.

    Пауза — от конца последнего старта и только для того, кто стоял в очереди:
    одиночный старт (и последовательные тесты) не ждут ничего.
    """

    def __init__(self, *, limit: Callable[[], int] = parallel_starts,
                 gap: Callable[[], float] = start_gap_s,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self._limit = limit
        self._gap = gap
        self._clock = clock
        self._sleep = sleep
        self._cond = threading.Condition()
        self._in_flight = 0
        self._last_end = 0.0

    def in_flight(self) -> int:
        with self._cond:
            return self._in_flight

    @contextmanager
    def slot(self) -> Iterator[dict]:
        """Контекст одного запроса `start`; отдаёт `{waited_s, in_flight}` для телеметрии."""
        began = self._clock()
        waited = False
        with self._cond:
            while self._in_flight >= max(1, int(self._limit())):
                waited = True
                self._cond.wait(timeout=1.0)
            self._in_flight += 1
            pause = max(0.0, self._last_end + self._gap() - self._clock()) if waited else 0.0
        info = {"in_flight": 0, "waited_s": 0.0}
        try:
            if pause > 0:
                self._sleep(pause)
            with self._cond:
                info["in_flight"] = self._in_flight
            info["waited_s"] = round(self._clock() - began, 1)
            yield info
        finally:
            with self._cond:
                self._in_flight -= 1
                self._last_end = self._clock()
                self._cond.notify_all()


_PACER = StartPacer()


def pacer() -> StartPacer:
    return _PACER


def is_launcher_connection_error(exc: BaseException | None) -> bool:
    """Обрыв соединения с MLX launcher (WinError 10053/10054, «Connection aborted/reset»).

    Смотрит цепочку исключения и аргументы (`requests` заворачивает
    `ConnectionAbortedError` в `ProtocolError`, а тот — в `ConnectionError`).
    Таймаут чтения — НЕ обрыв: старт мог ещё идти, повтор дал бы второй.
    """
    seen: set[int] = set()
    stack: list[Any] = [exc]
    while stack:
        item = stack.pop()
        if item is None or id(item) in seen:
            continue
        seen.add(id(item))
        if isinstance(item, (ConnectionAbortedError, ConnectionResetError)):
            return True
        if isinstance(item, BaseException):
            if "timeout" in type(item).__name__.lower() or isinstance(item, TimeoutError):
                return False
            if getattr(item, "winerror", None) in (10053, 10054) or getattr(item, "errno", None) in (10053, 10054):
                return True
            stack.extend([item.__cause__, item.__context__, *[a for a in item.args
                                                              if isinstance(a, BaseException)]])
        text = str(item)
        if "10053" in text or "10054" in text or "Connection aborted" in text or "Connection reset" in text:
            return True
    return False


def record_start(*, ok: bool, took_s: float, attempt: int, pace: dict | None = None,
                 error: BaseException | None = None, adopted: bool = False, code: str = "") -> None:
    """Строка `fb_api_mlx_start`: сколько шёл старт и сколько стартов было разом."""
    event: dict = {"event": "fb_api_mlx_start", "ok": bool(ok), "took_s": round(float(took_s), 1),
                   "attempt": int(attempt), "in_flight": int((pace or {}).get("in_flight") or 0),
                   "waited_s": float((pace or {}).get("waited_s") or 0.0),
                   "parallel_starts": parallel_starts()}
    if adopted:
        event["adopted_after_abort"] = True
    if error is not None:
        event["error"] = ("launcher_connection" if is_launcher_connection_error(error)
                          else type(error).__name__)
    if code:
        event["code"] = code
    _record(event)


# ───────────────────────── холодный бюджет CDP ─────────────────────────

_COLD: dict[int, float] = {}
#: Профиль → его холодный порт: остановка профиля снимает отметку (`forget_profile`).
_COLD_PROFILE: dict[str, int] = {}
_COLD_LOCK = threading.Lock()


def _port_of(cdp_url_or_port: Any) -> int:
    if isinstance(cdp_url_or_port, int):
        return cdp_url_or_port
    raw = str(cdp_url_or_port or "").strip()
    if raw.isdigit():
        return int(raw)
    try:
        return int(urllib.parse.urlsplit(raw if "://" in raw else f"http://{raw}").port or 0)
    except ValueError:
        return 0


def mark_started(port: Any, profile_id: str = "", *, clock: Callable[[], float] = time.monotonic) -> None:
    """Порт профиля, который только что поднял MLX: первые вызовы CDP — с холодным сроком."""
    p = _port_of(port)
    if p:
        with _COLD_LOCK:
            _COLD[p] = clock()
            if profile_id:
                _COLD_PROFILE[str(profile_id)] = p


def cold_since(cdp_url: Any, *, clock: Callable[[], float] = time.monotonic) -> float | None:
    """Сколько секунд назад подняли профиль этого порта; None — порт не холодный."""
    p = _port_of(cdp_url)
    with _COLD_LOCK:
        at = _COLD.get(p)
        if at is None:
            return None
        age = clock() - at
        if age > COLD_WINDOW_S:
            _COLD.pop(p, None)
            return None
        return age


def forget(cdp_url: Any) -> None:
    """Первая вкладка открыта (или профиль остановлен) — дальше обычные сроки."""
    p = _port_of(cdp_url)
    with _COLD_LOCK:
        _COLD.pop(p, None)


def forget_profile(profile_id: str) -> None:
    """Профиль остановлен: его порт больше не наш и не холодный (слушатель `stop_log`)."""
    with _COLD_LOCK:
        p = _COLD_PROFILE.pop(str(profile_id or ""), 0)
        if p:
            _COLD.pop(p, None)


def _listen_for_stops() -> None:
    try:
        from src.fb_api import stop_log as _stop_log

        _stop_log.default().on_stop(forget_profile)
    except Exception:  # noqa: BLE001 — без слушателя отметка всё равно истечёт за COLD_WINDOW_S
        pass


_listen_for_stops()


def record_cold_open(*, ok: bool, since_start_s: float, budget_s: float, timings: dict,
                     step: str = "") -> None:
    event = {"event": "fb_api_cold_open", "ok": bool(ok), "since_start_s": round(float(since_start_s), 1),
             "budget_s": round(float(budget_s), 1),
             **{k: round(float(v), 2) for k, v in timings.items()}}
    if step:
        event["step"] = step
    _record(event)
