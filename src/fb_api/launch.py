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

#: Нейтральная страница для пробы прокси перед первым заходом в Facebook (п. 7 ACL-2095).
PROXY_PROBE_ENV = "ACL_PROXY_PROBE_URL"
DEFAULT_PROXY_PROBE_URL = "https://api.ipify.org/"
#: Через сколько секунд повторить пробу/первую навигацию после ERR_INVALID_AUTH_CREDENTIALS.
PROXY_RETRY_S = 12.0
#: Сколько ждём, пока процесс остановленного профиля уйдёт (п. 8c).
STOP_GONE_WAIT_S = 30.0


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


def proxy_probe_url() -> str:
    """Пустая строка (`ACL_PROXY_PROBE_URL=off`) — пробы нет, остаётся повтор первой навигации."""
    raw = str(os.environ.get(PROXY_PROBE_ENV, "") or DEFAULT_PROXY_PROBE_URL).strip()
    return "" if raw.lower() in ("off", "0", "none") else raw


def is_proxy_auth_error(text: str) -> bool:
    return "err_invalid_auth_credentials" in str(text or "").lower()


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
#: Порты, чей браузер уже ответил на `/json/version` после старта (старт устоялся).
_READY: set[int] = set()


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
            _READY.discard(p)
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


def mark_ready(cdp_url: Any) -> None:
    """Браузер этого порта ответил после старта: stop больше не ждёт (п. 8b)."""
    p = _port_of(cdp_url)
    if p:
        with _COLD_LOCK:
            _READY.add(p)


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
            _READY.discard(p)


# ───────────────────────── готовность после старта (п. 8) ─────────────────────────

def debug_answers(port: Any, *, timeout: float = 1.5) -> bool:
    """Отвечает ли браузер на `/json/version` (порт слушается — ещё не значит готов)."""
    p = _port_of(port)
    if not p:
        return False
    try:
        import json
        import urllib.request

        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{p}/json/version", timeout=timeout) as resp:
            return isinstance(json.loads(resp.read().decode("utf-8", errors="replace")), dict)
    except Exception:
        return False


def wait_ready(port: Any, *, budget_s: float | None = None,
               answers: Callable[[Any], bool] = debug_answers,
               sleep: Callable[[float], None] = time.sleep,
               clock: Callable[[], float] = time.monotonic, poll_s: float = 1.0) -> tuple[bool, float]:
    """Ждать ответа `/json/version` после старта (живой B97 13:27: порт отдан за 11,6 с,
    а браузер поднялся на 10–30 с позже). Ответ — (готов, сколько ждали). Не бросает:
    не дождались — дальше решает холодный бюджет CDP, профиль остаётся нашим."""
    budget = cold_start_s() if budget_s is None else float(budget_s)
    began = clock()
    while True:
        if answers(port):
            with _COLD_LOCK:
                _READY.add(_port_of(port))
            return True, clock() - began
        if clock() - began >= budget:
            return False, clock() - began
        sleep(poll_s)


def wait_start_settled(profile_id: str, *, answers: Callable[[Any], bool] = debug_answers,
                       sleep: Callable[[float], None] = time.sleep,
                       clock: Callable[[], float] = time.monotonic) -> str:
    """Перед stop: старт этого профиля устоялся? Ждём готовности или конца бюджета старта.

    Stop, посланный, пока браузер ещё поднимается, MLX принимает и НЕ выполняет —
    браузер потом встаёт и висит брошенным (п. 8b). Ответ для телеметрии:
    "not_ours" / "ready" / "settled" (дождались) / "budget_over".
    """
    pid = str(profile_id or "")
    with _COLD_LOCK:
        p = _COLD_PROFILE.get(pid, 0)
        at = _COLD.get(p) if p else None
        ready = p in _READY
    if not p or at is None:
        return "not_ours"
    if ready:
        return "ready"
    left = at + cold_start_s() - clock()
    if left <= 0:
        return "budget_over"
    ok, _took = wait_ready(p, budget_s=left, answers=answers, sleep=sleep, clock=clock)
    return "settled" if ok else "budget_over"


# ───────────────────────── телеметрия остановка → переоткрытие (ACL-2117) ─────────────────────────
#
# Гипотеза живых прогонов 22.10: переоткрытие профиля, пока MLX ещё сохраняет его в
# облако, отдаёт копию без сессии Facebook. Чтобы подтвердить или снять её, на каждый
# повторный запуск пишется цепочка: время остановки, время ухода процесса
# (`fb_api_stop_gone` / `fb_api_profile_stop`), чем кончилось ожидание сохранения
# (`fb_api_profile_sync_wait`: пауза, сколько ждали), время переоткрытия
# (`fb_api_profile_reopen`) и была ли на странице сессия Graph
# (`fb_api_reopen_graph_session`). Токенов и адресов здесь нет.

_WAITED: dict[str, dict] = {}
_REOPENED: dict[str, dict] = {}
_TRACE_LOCK = threading.Lock()


def record_profile_stop(*, account: str, profile_id: str, stop_at: float, exit_at: float | None,
                        settled: str, gone: bool) -> None:
    _record({"event": "fb_api_profile_stop", "account": account, "stop_at": round(stop_at, 1),
             "exit_at": None if exit_at is None else round(exit_at, 1), "process_gone": bool(gone),
             "stop_to_exit_s": None if exit_at is None else round(exit_at - stop_at, 1),
             "start_state": settled})


def note_waited(profile_id: str, *, stop_at: float | None, waited_s: float) -> None:
    with _TRACE_LOCK:
        _WAITED[str(profile_id)] = {"stop_at": stop_at, "waited_s": float(waited_s)}


def mark_reopened(profile_id: str, *, wall: Callable[[], float] = time.time) -> None:
    """Профиль поднят заново после остановки (старт через MLX): пишем переоткрытие."""
    pid = str(profile_id or "")
    from src.fb_api import stop_log as _stop_log

    now = wall()
    try:
        stop_at = _stop_log.default().last_stop(pid)
    except Exception:  # noqa: BLE001
        stop_at = None
    with _TRACE_LOCK:
        waited = _WAITED.pop(pid, None) or {}
        if stop_at is None:
            stop_at = waited.get("stop_at")
        if stop_at is None:
            return                      # профиль не останавливали при нас — нечего сравнивать
        info = {"reopen_at": now, "stop_at": stop_at, "waited_s": waited.get("waited_s", 0.0),
                "cooldown_s": _stop_log.restart_cooldown_s()}
        _REOPENED[pid] = info
    _record({"event": "fb_api_profile_reopen", "stop_at": round(stop_at, 1), "reopen_at": round(now, 1),
             "stop_age_s": round(now - stop_at, 1), "waited_s": round(info["waited_s"], 1),
             "cooldown_s": round(info["cooldown_s"], 1),
             "sync_status": "cooldown_waited" if info["waited_s"] > 0 else "cooldown_passed"})


def note_graph_session(profile_id: str, has_token: bool, *, account: str = "",
                       wall: Callable[[], float] = time.time) -> None:
    """Первая страница Ads Manager после переоткрытия: была ли сессия Graph."""
    with _TRACE_LOCK:
        info = _REOPENED.pop(str(profile_id or ""), None)
    if info is None:
        return
    _record({"event": "fb_api_reopen_graph_session", "account": account, "has_graph_session": bool(has_token),
             "stop_age_s": round(info["reopen_at"] - info["stop_at"], 1),
             "waited_s": round(info["waited_s"], 1),
             "since_reopen_s": round(wall() - info["reopen_at"], 1)})


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
