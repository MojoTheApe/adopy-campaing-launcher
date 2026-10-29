"""Тёплые сессии (ACL-1812): профили открыты весь день, у каждого одна вкладка робота.

Зачем. Каждое действие по API (чтение, опись, команды, создание) раньше
поднимало или цепляло профиль, открывало свежую вкладку Ads Manager, ждало
загрузки 30–60 с, работало и закрывало вкладку. Менеджеру, который работает с
аккаунтами весь день, нужны секунды. Здесь — реестр «тёплых» профилей:

* профиль попадает в реестр, когда человек нажал «Open profiles for today»
  (дешборд сам его поднял — `dashboard_started`) или нажал это на уже открытом
  с портом профиле (взят в реестр, но НЕ нами поднят — закрыть его мы не можем);
* у профиля в реестре ОДНА вкладка Ads Manager, принадлежащая роботу. Её не
  закрывают после действия; следующее действие берёт ту же вкладку без
  перезагрузки;
* перед каждым действием человека — дешёвая проверка: URL вкладки от БРАУЗЕРА
  (`Target.getTargetInfo`, рендерер может молчать) и `.state()` помощника. Вкладка
  мёртвая или зависшая заменяется РОВНО один раз; вход, checkpoint и «не
  отвечает» останавливают действие теми же причинами, что и раньше (охлаждения
  `account_spend_api` действуют как прежде);
* замок профиля: два действия никогда не работают в одной вкладке;
* ворота памяти считают только ИДУЩИЕ тяжёлые действия, не открытые профили.
  По умолчанию одно, настраивается 1–3 (`ACL_WARM_MAX_HEAVY`).

Чего здесь нет и не будет: опроса по таймеру, автоповтора входа, остановки
профиля, который поднял не дешборд, открытия аккаунта вне Multilogin. Состояние
для экрана (`states`) берётся ТОЛЬКО из реестра и процессов на машине — ни одного
вызова к Meta, MLX или CDP.

Перезапуск профиля быстро после остановки — вероятная причина двух разлогинов
5414 (ACL-1811). Пока общий журнал остановок не вошёл, реестр ведёт свой
(`stops` в том же файле): запуск раньше `ACL_PROFILE_RESTART_COOLDOWN_S`
(3 минуты) после последней остановки отказывает с оставшимся временем.

Антитроттлинг фоновой вкладки (focus emulation + lifecycle active) остаётся в
`PageTab.open` — долгоживущая вкладка получает его при создании. Чтобы он не
ослабел за часы, перед действием проверка живая (см. выше): вкладка, которая
перестала отвечать, заменяется.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from contextvars import ContextVar
import contextlib
import logging
import os
import random
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from src.fb_api import profile as _fb_profile
from src.fb_api import stop_log as _stop_log
from src.fb_api.graph import Checkpoint, GraphStop, LoggedOut, PageFailed, PageUnresponsive
from src.fb_api.telemetry import scrub

# Upper limit of the connection preparation. It was 45 s: on a loaded machine (a profile takes 2-3 minutes to start)
# the preparation did not fit and the job failed with "Connection preparation time budget exhausted".
PREPARE_BUDGET_CAP_S = 150.0
_PREPARE_DEADLINE = ContextVar("acl_warm_prepare_deadline",default=None)
#: ACL-2278: a publish job running in this thread. While it waits for memory or for the account-action
#: gate it stays queued (visible, cancellable) instead of failing after a fixed time.
_JOB_WAIT = ContextVar("acl_warm_job_wait",default=None)
#: ACL-2278: a plain HTTP request (connection prepare) must not wait for memory: it returns at once and
#: the job opens the profile later, under the queue.
_DEFER_MEMORY = ContextVar("acl_warm_defer_memory",default=False)

log = logging.getLogger(__name__)


@dataclass
class JobWait:
    """How a waiting job is shown and stopped (ACL-2278).

    `cancelled()` - the user pressed Stop. `show(view)` - publish the live reason of the wait (free and
    needed memory, hold kind); `show(None)` - the wait is over.
    """
    cancelled: Callable[[], bool]
    show: Callable[[dict | None], None]


@contextmanager
def job_wait(cancelled: Callable[[], bool], show: Callable[[dict | None], None]):
    """Run the code of one job in this thread with patient (queued, not failing) memory/gate waits."""
    token = _JOB_WAIT.set(JobWait(cancelled, show))
    try:
        yield
    finally:
        _JOB_WAIT.reset(token)


@contextmanager
def defer_memory_wait():
    """Inside: a memory shortage returns at once (code `memory_low`) instead of waiting."""
    token = _DEFER_MEMORY.set(True)
    try:
        yield
    finally:
        _DEFER_MEMORY.reset(token)


def memory_deferred(code: str) -> bool:
    return code == "memory_low"


def _show(hook: "JobWait | None", view: dict | None) -> None:
    if hook is None:
        return
    try:
        hook.show(view)
    except Exception:  # noqa: BLE001 - showing the wait must never fail the job
        log.exception("Could not show the wait of the job")

RESTART_COOLDOWN_ENV = _stop_log.RESTART_COOLDOWN_ENV
DEFAULT_RESTART_COOLDOWN_S = _stop_log.DEFAULT_RESTART_COOLDOWN_S
MAX_HEAVY_ENV = "ACL_WARM_MAX_HEAVY"
GATE_WAIT_ENV = "ACL_WARM_GATE_WAIT_S"
DEFAULT_GATE_WAIT_S = 300.0
#: Пауза между запусками профилей «на сегодня» (секунды, случайно в диапазоне).
STAGGER_MIN_S = 5.0
STAGGER_MAX_S = 10.0
#: Сколько ждём места по памяти под запуск одного профиля.
START_SLOT_WAIT_S = 120.0
#: Сколько ждём памяти под запуск следующего профиля и сколько профиль «приживается».
START_MEMORY_WAIT_S = 120.0
#: ACL-2262: сколько ждём, пока профиль, который только что закрывали, отпустят (и как часто смотрим).
CLOSE_WAIT_S = 120.0
CLOSE_POLL_S = 3.0
SETTLE_ENV = "ACL_WARM_SETTLE_S"
DEFAULT_SETTLE_S = 90.0
#: ACL-2283. "Busy" machine = CPU at or above this share, or other profiles still warming up. Used for the
#: staggered start (the next start waits for the CPU to settle below it), for patient readiness (a tab that
#: has not answered yet is not called unresponsive for minutes) and for the closed-while-starting fallback.
LOAD_CPU_ENV = "ACL_LOAD_CPU_PCT"
DEFAULT_LOAD_CPU_PCT = 85.0
#: A: how long the CPU must stay below the level before the next profile of a job starts.
START_CPU_SETTLE_ENV = "ACL_START_CPU_SETTLE_S"
DEFAULT_START_CPU_SETTLE_S = 5.0
#: A: a started profile counts as "not ready yet" until its tab answered, but not longer than this.
START_READY_CAP_S = 300.0
#: B: how long a tab may stay silent on a busy machine before it is called unresponsive (idle: today's limit).
PATIENT_READY_ENV = "ACL_PATIENT_READY_S"
DEFAULT_PATIENT_READY_S = 480.0
#: C: growing pause between two starts of a profile that closed while starting (never hammers Multilogin).
LOAD_BACKOFF_FIRST_S = 15.0
LOAD_BACKOFF_CAP_S = 300.0
_PATIENT = ContextVar("acl_warm_patient", default=None)

#: Состояния для экрана.
WARM = "warm"
OPEN = "open"
CLOSED = "closed"
LOGGED_OUT = "logged_out"
BY_HAND = "by_hand"
CLOSING = "closing"
UNKNOWN = "unknown"

HAND_MESSAGE = ("Opened in Multilogin by hand — close its window there, then run Check plan or "
                "Publish and the dashboard opens it for automation")

#: Хост и путь Ads Manager в адресе вкладки.
_FB_HOST_SUFFIX = "facebook.com"


class WarmError(RuntimeError):
    """Отказ тёплой сессии с текстом человеку и устойчивым кодом для экрана."""

    def __init__(self, message: str, code: str = ""):
        super().__init__(scrub(message))
        self.code = code


def _env_number(name: str, default: float, lo: float, hi: float) -> float:
    try:
        value = float(str(os.environ.get(name, "") or default).replace(",", "."))
    except ValueError:
        value = default
    return max(lo, min(hi, value))


def restart_cooldown_s() -> float:
    return _stop_log.restart_cooldown_s()


def restart_refusal(profile_id: str) -> str:
    """Общая проверка «не запускать сразу после остановки» (пусто — можно). См. `stop_log`."""
    return _stop_log.refusal(profile_id)


def max_heavy() -> int:
    """Сколько тяжёлых действий идут разом: по умолчанию одно, настройкой 1–3."""
    return int(_env_number(MAX_HEAVY_ENV, 1, 1, 3))


def _hhmm(epoch: float) -> str:
    try:
        return datetime.fromtimestamp(float(epoch)).strftime("%H:%M")
    except (OverflowError, OSError, ValueError):
        return ""


def _is_ads_manager(url: str) -> bool:
    try:
        parts = urllib.parse.urlsplit(url or "")
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    return host.endswith(_FB_HOST_SUFFIX) and (parts.path or "").lower().startswith("/adsmanager")


def _url_stop(url: str) -> GraphStop | None:
    """Вход / checkpoint по адресу вкладки (от браузера). Адрес в текст не попадает."""
    try:
        path = urllib.parse.urlsplit(url or "").path.lower()
    except ValueError:
        return None
    if path.startswith(("/login", "/recover")):
        return LoggedOut("the profile is logged out of Facebook; log in inside Multilogin, then retry")
    if "/checkpoint" in path or "two_step" in path or "two_factor" in path:
        return Checkpoint("the profile is on a Facebook checkpoint")
    return None


def _url_act(url: str) -> str:
    try:
        got = urllib.parse.parse_qs(urllib.parse.urlsplit(url or "").query).get("act") or []
    except ValueError:
        return ""
    return str(got[0]) if got else ""


# ───────────────────────── хранилище: запись о профилях и журнал остановок ─────────────────────────

class WarmStore:
    """JSON-файл в каталоге настроек машины: профили реестра.

    Локальный, не в гите. Содержимое — id профилей, порты, время, pid владельца
    записи: токенов и паролей здесь нет. Запись — чтение-изменение-запись под
    файловым замком (два процесса дешборда не затирают друг друга).
    """

    def __init__(self, path: Callable[[], Path] | Path | str | None = None):
        self._path = path
        self._lock = threading.Lock()

    def _file(self) -> Path:
        path = self._path() if callable(self._path) else self._path
        if path:
            return Path(path)
        from config.paths import get_local_config_dir

        return Path(get_local_config_dir()) / "warm_sessions.json"

    def _read(self) -> dict:
        try:
            data = json.loads(self._file().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        if not isinstance(data.get("profiles"), dict):
            data["profiles"] = {}
        return data

    def load(self) -> dict:
        with self._lock:
            return self._read()

    def update(self, mutate: Callable[[dict], None]) -> None:
        """Слияние: читаем файл как он есть сейчас, меняем свои записи, пишем обратно."""
        with self._lock:
            try:
                target = self._file()
                target.parent.mkdir(parents=True, exist_ok=True)
                with _stop_log.file_lock(target):
                    data = self._read()
                    mutate(data)
                    _stop_log.atomic_write(target, data)
            except OSError:
                log.exception("Could not save the warm session registry")


def _pid_alive(pid: int, created: float) -> bool:
    """Жив ли процесс-владелец записи (pid + время создания: pid мог перейти другому)."""
    if pid <= 0:
        return False
    try:
        import psutil  # type: ignore

        proc = psutil.Process(pid)
        return abs(proc.create_time() - created) < 2.0 if created else proc.is_running()
    except Exception:
        return False


def _my_start() -> float:
    try:
        import psutil  # type: ignore

        return float(psutil.Process(os.getpid()).create_time())
    except Exception:
        return 0.0


# ───────────────────────── ворота: сколько тяжёлых действий идут разом ─────────────────────────

class HeavyGate:
    """Считает ИДУЩИЕ тяжёлые действия (вкладка Ads Manager в работе), не открытые профили.

    Первое действие идёт всегда (пол: одно при любой памяти). Второе и третье —
    только если лимит (`max_heavy`) позволяет И свободной памяти хватает на ещё
    один профиль: замер дерева процессов идущих профилей, если он есть
    (`ProfileUsage`), иначе оценка из `profile_slots`. Память не прочиталась —
    не больше двух разом. Повторный вход того же профиля (вложенное чтение тем
    же потоком) места не занимает.
    """

    def __init__(self, *, limit: Callable[[], int] = max_heavy,
                 memory: Callable[[], Any] | None = None,
                 settings: Callable[[], Any] | None = None,
                 usage: Any = None, clock: Callable[[], float] = time.monotonic,
                 tick_s: float = 1.0):
        from src.web import profile_slots as _ps

        self._ps = _ps
        self._limit = limit
        self._memory = memory or _ps.read_memory
        self._settings = settings or _ps.load_settings
        self._usage = usage if usage is not None else _ps.ProfileUsage()
        self._clock = clock
        self._tick_s = tick_s
        self._cond = threading.Condition()
        self._active: dict[str, int] = {}

    def active(self) -> list[str]:
        with self._cond:
            return sorted(self._active)

    def _allowed(self) -> tuple[bool, str]:
        n = len(self._active)
        limit = max(1, int(self._limit()))
        memory = self._memory()
        if n == 0:
            # S3: и первое действие не обходит запас: выше допустимой доли занятой памяти ждёт, а не лезет в неё.
            if memory is not None and memory.available_mb < self._ps.keep_free_mb(memory.total_mb, self._settings()):
                return False, "below_reserve"
            return True, "first"
        if n >= limit:
            return False, "limit"
        if memory is None:
            return (n < min(self._ps.FALLBACK_LIMIT, limit)), "memory_unknown"
        # ACL-2255: by the SHARE of memory in use (the running actions are already in it), not by "N GB per profile".
        ok = memory.available_mb >= self._ps.keep_free_mb(memory.total_mb, self._settings())
        return ok, "memory_ok" if ok else "memory_low"

    def wait_view(self, why: str) -> dict:
        """Live numbers for a waiting action (ACL-2278): free memory, what a new profile needs, who runs."""
        memory = self._memory()
        settings = self._settings()
        probe = self._usage.read(sorted(self._active)) if self._usage is not None and self._active else {}
        measured = [int(v) for v in (probe.get("per_profile") or {}).values()]
        per_mb = settings.profile_mb
        if probe.get("status") == "ok" and measured:
            per_mb = max(max(measured), settings.profile_floor_mb)
        low = why in ("memory_low", "below_reserve")
        view = {"hold": "memory_low" if low else "account_busy",
                "available_mb": None if memory is None else int(memory.available_mb),
                "profile_mb": int(per_mb), "reserve_mb": int(settings.reserve_mb),
                "need_mb": int(per_mb + settings.reserve_mb), "running": len(self._active)}
        if memory is not None:
            keep = self._ps.keep_free_mb(memory.total_mb, settings)
            view.update(need_mb=int(keep), max_used_pct=settings.max_used_pct,
                        used_pct=self._ps.used_pct(memory), total_mb=int(memory.total_mb))
        return view

    def acquire(self, profile_id: str, *, wait_s: float = DEFAULT_GATE_WAIT_S,
                on_wait: Callable[[str], None] | None = None,
                cancelled: Callable[[], bool] | None = None,
                on_tick: Callable[[str], None] | None = None) -> None:
        """Ждать места. Не дождались за `wait_s` — `WarmError(code="gate_wait")`.

        ACL-2278: `cancelled` - the caller stops waiting (`code="cancelled"`); `on_tick(why)` is called on
        every look so the wait can show live numbers. A job passes `wait_s=inf`: it waits until it can go.
        """
        with self._cond:
            if profile_id in self._active:
                self._active[profile_id] += 1
                return
            deadline = self._clock() + wait_s
            told = ""
            while True:
                if cancelled is not None and cancelled():
                    raise WarmError("Stopped while waiting for the account gate. Nothing was called.",
                                    code="cancelled")
                ok, why = self._allowed()
                if ok:
                    self._active[profile_id] = 1
                    return
                if self._clock() >= deadline:
                    if why == "below_reserve":
                        raise WarmError(
                            "Free memory is below the safety reserve, so the action was not started. "
                            "Close something and try again. Nothing was called.", code="below_reserve")
                    raise WarmError(
                        "Another account action is still running and there is no room for a second "
                        "one yet. Nothing was called; try again when it finishes.", code="gate_wait")
                if on_wait is not None and why != told:
                    told = why
                    try:
                        on_wait(why)
                    except Exception:
                        pass
                if on_tick is not None:
                    try:
                        on_tick(why)
                    except Exception:
                        pass
                self._cond.wait(timeout=self._tick_s)

    def release(self, profile_id: str) -> None:
        with self._cond:
            left = self._active.get(profile_id, 0) - 1
            if left > 0:
                self._active[profile_id] = left
            else:
                self._active.pop(profile_id, None)
            self._cond.notify_all()


# ───────────────────────── сессия профиля и аренда ─────────────────────────

@dataclass
class WarmSession:
    profile_id: str
    account: str = ""
    port: int = 0
    started_at: float = 0.0
    #: Профиль поднял дешборд (MLX `start` вызвали мы): только такой закрывает «Close profiles».
    dashboard_started: bool = False
    logged_out: bool = False
    target_id: str = ""
    #: Вкладка прошлого запуска, чей владелец мёртв: её закроем при открытии новой.
    stale_target: str = ""
    owner_pid: int = 0
    owner_started: float = 0.0
    tab: Any = None
    graphs: dict = field(default_factory=dict)
    #: Обычный замок, не реентерантный: аренду отпускает `Lease.release`, а он может
    #: прийти из другого потока (отмена задания). Вложенный заход того же потока
    #: ловит `owner` и отказывает, а не виснет.
    lock: Any = field(default_factory=threading.Lock)
    owner: int = 0
    #: Процесс профиля сверен с реестром (после рестарта дешборда запись не доверена).
    verified: bool = False
    tab_since: float = 0.0

    def record(self) -> dict:
        return {"account": self.account, "port": self.port, "started_at": self.started_at,
                "dashboard_started": self.dashboard_started, "logged_out": self.logged_out,
                "target_id": self.target_id, "owner_pid": self.owner_pid,
                "owner_started": self.owner_started}


class Lease:
    """Аренда вкладки на одно действие: `graph` готов, токен во вкладке есть."""

    def __init__(self, registry: "WarmRegistry", session: WarmSession, graph: Any, state: dict):
        self._registry = registry
        self._session = session
        self.graph = graph
        self.state = state
        self.tab = session.tab
        self._released = False

    def release(self, stop: BaseException | None = None) -> None:
        """Вкладка остаётся открытой. `stop` — чем кончилось действие (LoggedOut и т. п.)."""
        if self._released:
            return
        self._released = True
        self._registry._finish(self._session, stop)


class WarmRegistry:
    def __init__(self, *, store: WarmStore | None = None, wall: Callable[[], float] = time.time,
                 scan: Callable[[list[str]], dict] | None = None,
                 tab_factory: Callable[[str], Any] | None = None,
                 start_profile: Callable[..., Any] | None = None,
                 close_window: Callable[[Any, str], Any] | None = None,
                 slot_queue: Callable[[], Any] | None = None,
                 gate: HeavyGate | None = None,
                 stops: "_stop_log.StopLog | None" = None,
                 pid_alive: Callable[[int, float], bool] = _pid_alive,
                 pending_close: Callable[[str], Any] | None = None,
                 pending_restore: Callable[[str, Any], None] | None = None,
                 memory: Callable[[], Any] | None = None,
                 settings: Callable[[], Any] | None = None,
                 usage: Any = None,
                 cpu: Callable[[], float | None] | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 mono: Callable[[], float] = time.monotonic,
                 stagger: Callable[[], float] = lambda: random.uniform(STAGGER_MIN_S, STAGGER_MAX_S)):
        self._store = store or WarmStore()
        self._wall = wall
        self._scan = scan or scan_local
        self._tab_factory = tab_factory or _default_tab
        self._start_profile = start_profile or _default_start
        #: Закрытие окна профиля подаёт дешборд (`set_closer`): пилот в `src/fb_api` профили не останавливает.
        self._close_window = close_window
        self._slot_queue = slot_queue or _default_slot_queue
        self.gate = gate or HeavyGate()
        self._stops = stops or _stop_log.default()
        self._pid_alive = pid_alive
        #: Забрать отложенное закрытие профиля после чтения (S1): `pid -> прежняя запись | None`.
        self._pending_close = pending_close or _default_pending_close
        self._pending_restore = pending_restore or _default_pending_restore
        self._memory = memory
        self._settings = settings
        self._usage = usage
        self._cpu = cpu or _default_cpu
        self._sleep = sleep
        self._mono = mono
        self._stagger = stagger
        self._lock = threading.RLock()
        self._sessions: dict[str, WarmSession] = {}
        #: Записи, которые держит ДРУГОЙ живой процесс дешборда: не трогаем ни их, ни их вкладки.
        self._foreign: dict[str, dict] = {}
        self._removed: set[str] = set()
        #: Когда профиль, поднятый открытием, начал прогреваться (monotonic) — резерв под рост.
        self._settling: dict[str, float] = {}
        #: ACL-2283: profiles started by a job whose tab has not answered yet (the next start waits), and
        #: profiles whose tab answered.
        self._starting: dict[str, float] = {}
        self._ready: set[str] = set()
        self._me = (os.getpid(), _my_start())
        self._load()

    # -- хранилище
    def _load(self) -> None:
        data = self._store.load()
        for pid, rec in (data.get("profiles") or {}).items():
            if not isinstance(rec, dict):
                continue
            owner = int(rec.get("owner_pid") or 0)
            started = float(rec.get("owner_started") or 0.0)
            if owner and owner != self._me[0] and self._pid_alive(owner, started):
                self._foreign[pid] = rec          # живёт у соседнего процесса
                continue
            sess = WarmSession(
                profile_id=pid, account=str(rec.get("account") or ""), port=int(rec.get("port") or 0),
                started_at=float(rec.get("started_at") or 0.0),
                dashboard_started=bool(rec.get("dashboard_started")),
                logged_out=bool(rec.get("logged_out")),
                # Вкладка принадлежала умершему процессу — закроем её, когда откроем свою.
                stale_target=str(rec.get("target_id") or ""),
                owner_pid=self._me[0], owner_started=self._me[1])
            self._sessions[pid] = sess

    def _save(self) -> None:
        with self._lock:
            mine = {pid: s.record() for pid, s in self._sessions.items()}
            removed, self._removed = set(self._removed), set()

        def mutate(data: dict) -> None:
            profiles = data["profiles"]
            for pid, rec in mine.items():
                profiles[pid] = rec
            for pid in removed:
                rec = profiles.get(pid) or {}
                owner = int(rec.get("owner_pid") or 0)
                if pid not in mine and (not owner or owner == self._me[0]
                                        or not self._pid_alive(owner, float(rec.get("owner_started") or 0.0))):
                    profiles.pop(pid, None)

        self._store.update(mutate)

    def has_closer(self) -> bool:
        return self._close_window is not None

    def set_closer(self, close_window: Callable[[Any, str], Any]) -> None:
        self._close_window = close_window

    # -- реестр
    def is_warm(self, profile_id: str) -> bool:
        with self._lock:
            return profile_id in self._sessions

    def session(self, profile_id: str) -> WarmSession | None:
        with self._lock:
            return self._sessions.get(profile_id)

    def register(self, profile_id: str, *, account: str, port: int, dashboard_started: bool) -> WarmSession:
        with self._lock:
            sess = self._sessions.get(profile_id)
            if sess is None or sess.port != int(port):
                sess = WarmSession(profile_id=profile_id, started_at=self._wall())
                self._sessions[profile_id] = sess
            sess.account = account
            sess.port = int(port)
            sess.logged_out = False
            sess.verified = True
            sess.owner_pid, sess.owner_started = self._me
            self._foreign.pop(profile_id, None)
            # «Дешборд поднял» не пропадает от повторного открытия уже работающего профиля.
            sess.dashboard_started = bool(sess.dashboard_started or dashboard_started)
        self._save()
        return sess

    def _forget(self, profile_id: str, *, stopped: bool) -> None:
        with self._lock:
            sess = self._sessions.pop(profile_id, None)
            self._removed.add(profile_id)
            self._settling.pop(profile_id, None)
            self._starting.pop(profile_id, None)
            self._ready.discard(profile_id)
        if stopped:
            self._stops.record(profile_id)
        # Вкладку не закрываем: профиль уже ушёл (или это уже не наш экземпляр), и
        # закрытие по мёртвому сокету только ждало бы таймаута. Ссылку отпускаем.
        if sess is not None:
            sess.tab, sess.graphs = None, {}
        self._save()

    def record_stop(self, profile_id: str) -> None:
        self._stops.record(profile_id)

    def forget_stopped(self, profile_id: str) -> None:
        """Профиль остановлен где угодно (журнал остановок): его порт и вкладку забываем.

        Следующее действие поднимает профиль заново через MLX, а не цепляется к
        порту остановленного браузера (TEST9095y, B97: `CdpTimeout: connect`).
        """
        if self.is_warm(profile_id):
            self._forget(profile_id, stopped=False)

    def load_snapshot(self, profile_id: str = "") -> dict:
        """ACL-2283: is this machine busy right now? Local readings only (CPU, memory, own processes).

        `busy` - CPU at or above `ACL_LOAD_CPU_PCT` (default 85) or other profiles still warming up (started
        less than `ACL_WARM_SETTLE_S` ago and whose tab has not answered). `memory_high` - memory in use
        above the ACL-2255 share rule. `clear` - neither: it is safe to start. An unreadable CPU
        counts as quiet: no guess. `warming_ids` are the other profiles; `cpu_pct` is the whole machine.
        """
        cpu = None
        try:
            cpu = self._cpu()
        except Exception:  # noqa: BLE001
            cpu = None
        used = cap = avail = None
        memory_high = False
        try:
            memory, settings, _ps = self._memory_state()
            # The memory rule (share in use, ACL-2255) lives in `_memory_need`: short = less free than it asks.
            need = self._memory_need(settings, _ps)[0]
            if memory is not None:
                avail = int(memory.available_mb)
                used = round(100.0 * (memory.total_mb - memory.available_mb) / memory.total_mb, 1) if memory.total_mb else None
                memory_high = memory.available_mb < need
        except Exception:  # noqa: BLE001
            pass
        warming = self.warming_others(profile_id)
        others = [p for p in self.gate.active() if p != profile_id]
        level = _env_number(LOAD_CPU_ENV, DEFAULT_LOAD_CPU_PCT, 1.0, 100.0)
        cpu_high = cpu is not None and cpu >= level
        reasons = [r for r, on in (("cpu_high", cpu_high), ("profiles_warming_up", bool(warming)),
                                   ("memory_high", memory_high)) if on]
        return {"busy": cpu_high or bool(warming), "loaded": bool(reasons), "reasons": reasons,
                "memory_high": memory_high, "clear": not reasons,
                "cpu_pct": None if cpu is None else round(float(cpu), 1), "cpu_level": level,
                "available_mb": avail, "used_pct": used,
                "running": len(others), "warming": len(warming)}

    def warming_others(self, profile_id: str = "") -> list[str]:
        """Other profiles of ours that were started a moment ago and whose tab has not answered yet."""
        now = self._mono()
        settle = _env_number(SETTLE_ENV, DEFAULT_SETTLE_S, 0.0, 3600.0)
        with self._lock:
            started = {p for p, t in self._starting.items() if now - t < START_READY_CAP_S}
            settling = {p for p, t in self._settling.items()
                        if now - t < settle and p in self._sessions and p not in self._ready}
            return sorted((started | settling) - {profile_id})

    def mark_ready(self, profile_id: str) -> None:
        """The tab of this profile answered: the next profile may start (staggered start, ACL-2283)."""
        with self._lock:
            self._starting.pop(profile_id, None)
            self._ready.add(profile_id)

    def _wait_start_turn(self, pid: str, cancelled: Callable[[], bool], job: "JobWait") -> bool:
        """A (ACL-2283): profiles of jobs start one at a time.

        Waits (queued, cancellable, live numbers) until no other started profile is still warming up and the
        CPU has been below the level for `ACL_START_CPU_SETTLE_S`. The turn is taken atomically, so two jobs
        cannot pass together. False - cancelled.
        """
        settle = _env_number(START_CPU_SETTLE_ENV, DEFAULT_START_CPU_SETTLE_S, 0.0, 600.0)
        level = _env_number(LOAD_CPU_ENV, DEFAULT_LOAD_CPU_PCT, 1.0, 100.0)
        quiet_since: float | None = None
        try:
            while True:
                if cancelled():
                    return False
                cpu = None
                try:
                    cpu = self._cpu()
                except Exception:  # noqa: BLE001
                    cpu = None
                now = self._mono()
                if cpu is None or cpu < level:
                    quiet_since = now if quiet_since is None else quiet_since
                else:
                    quiet_since = None
                with self._lock:
                    now = self._mono()
                    self._starting = {p: t for p, t in self._starting.items() if now - t < START_READY_CAP_S}
                    others = self.warming_others(pid)
                    if not others and quiet_since is not None and now - quiet_since >= settle:
                        self._starting[pid] = now
                        return True
                _show(job, {"hold": "machine_busy", "cpu_pct": None if cpu is None else round(float(cpu), 1),
                            "cpu_level": level, "running": len(others), "warming": len(others)})
                self._sleep(2.0)
        finally:
            _show(job, None)

    def restart_wait_s(self, profile_id: str) -> float:
        """Сколько секунд ещё нельзя запускать профиль после его остановки (общий журнал)."""
        return self._stops.restart_wait_s(profile_id)

    # -- состояние для экрана (только локальные проверки)
    def states(self, accounts: list[tuple[str, str]]) -> dict[str, dict]:
        """`[(label, profile_id)]` → `{label: {state, text, title, since, closable, busy}}`.

        Реестр и процессы на машине. Ни Meta, ни MLX, ни CDP: безопасно вызывать
        при открытии страницы и выпадающего списка.
        """
        ids = [pid for _l, pid in accounts if pid]
        seen = self._scan(sorted(set(ids))) if ids else {}
        active = set(self.gate.active())
        out: dict[str, dict] = {}
        for label, pid in accounts:
            if not pid:
                out[label] = _chip(UNKNOWN, "No profile", "This account has no Multilogin profile in the map")
                continue
            kind, port = seen.get(pid, ("unknown", None))
            out[label] = self._state_one(pid, kind, port, busy=pid in active)
        return out

    def _state_one(self, pid: str, kind: str, port: int | None, *, busy: bool) -> dict:
        with self._lock:
            sess = self._sessions.get(pid)
        if kind == "port":
            if sess is not None and sess.port == int(port or 0):
                since = _hhmm(sess.started_at)
                if sess.logged_out:
                    return _chip(LOGGED_OUT, "Logged out", "Facebook asked for a login in this profile. "
                                 "Log in inside Multilogin, then run the action again.",
                                 closable=sess.dashboard_started)
                text = f"Warm · since {since}" if since else "Warm"
                return _chip(WARM, text + (" · working" if busy else ""),
                             "Open with automation; the robot keeps one Ads Manager tab for it",
                             since=since, closable=sess.dashboard_started, busy=busy)
            if sess is not None:
                # Порт другой: профиль перезапустил не дешборд. Запись больше не про него.
                self._forget(pid, stopped=False)
            return _chip(OPEN, "Open", "Open with automation, but not kept warm by the dashboard. "
                         "The dashboard attaches to it when an action starts.")
        if kind == "closing":
            return _chip(CLOSING, "Closing…", "The profile window is closing; wait a minute")
        if kind == "no_port":
            return _chip(BY_HAND, "Opened by hand", HAND_MESSAGE)
        if kind == "none":
            if sess is not None:
                # Профиль ушёл (закрыт человеком или упал): остановку запоминаем для паузы.
                self._forget(pid, stopped=True)
            wait = self.restart_wait_s(pid)
            title = "Not running"
            if wait > 0:
                title += f"; it can be started again in about {int(wait // 60) + 1} min"
            return _chip(CLOSED, "Closed", title)
        return _chip(UNKNOWN, "Unknown", "The running profiles could not be listed on this machine")

    # -- открыть профили на сегодня
    def open_one(self, label: str, entry: Any, *, job_id: str = "", cancelled: Callable[[], bool] = lambda: False) -> dict:
        """Поднять один профиль с автоматизацией. Ответ — `{ok, state, message, code, started}`."""
        pid = (getattr(entry, "profile_id", "") or "").strip()
        folder = (getattr(entry, "folder_id", "") or "").strip()
        instance = (getattr(entry, "multilogin_instance", "") or "A").strip() or "A"
        if not pid:
            return _result(False, "No Multilogin profile is linked to this account.", "no_profile")
        # ACL-1138: профиль без прокси в Multilogin не берём и не поднимаем — ни
        # чужой уже открытый (`_adopt`), ни свой запуск.
        refusal = _proxy_refusal(pid, instance, label)
        if refusal:
            return _result(False, refusal, "profile_no_proxy")
        kind, port = self._scan([pid]).get(pid, ("unknown", None))
        if kind == "port" and port:
            return self._adopt(label, pid, int(port))
        if kind == "no_port":
            from src.fb_api.connection import may_reopen_manual
            if may_reopen_manual():
                return self._reopen_manual(label, entry, job_id=job_id, cancelled=cancelled)
            return _result(False, HAND_MESSAGE, "open_by_hand")
        if kind == "closing":
            # ACL-2262: the previous job has just closed this profile; starting before Multilogin has
            # released it ends in "can't lock profile". Wait (bounded, one local look every few seconds,
            # no network) until it is gone, then start.
            kind = self._wait_until_closed(pid, cancelled)
            if kind != "none":
                return _result(False, f"The {label} profile is still closing in Multilogin; nothing was "
                               "started. Try again in a minute.", "profile_closing")
        if kind == "none" and self.session(pid) is not None:
            # B2: профиль в реестре, а процесса нет — его закрыли (человек, таймер чтения).
            # Останавливаем отсчёт от момента, когда увидели, и только потом решаем про запуск.
            self._forget(pid, stopped=True)
        why = _stop_log.refusal(pid, self._stops)
        if why:
            return _result(False, why, "restart_cooldown", wait_s=round(self.restart_wait_s(pid)))
        # S3: память под ещё один профиль с учётом роста уже запущенных и прогревающихся.
        job = _JOB_WAIT.get()
        if job is not None:
            # ACL-2278: Stop of the job also stops the waits below (the caller passes no `cancelled`).
            base_cancelled = cancelled
            cancelled = lambda: base_cancelled() or job.cancelled()   # noqa: E731
        # ACL-2283 (A): profiles of jobs start one at a time: wait for the previous start to be ready and the CPU
        # to settle. The turn is released when this start fails, or when the tab answers (`mark_ready`).
        # The turn wait can take minutes while the warming profiles grow, so memory is checked AGAIN once the
        # turn is ours and right before the start; over the line -> give the turn back and wait for memory
        # (queued, cancellable, live numbers), then take a turn again.
        took_turn = False
        while True:
            short = self._wait_for_memory(cancelled)
            if short:
                return _result(False, short, "memory_low")
            if job is None:
                break
            if not self._wait_start_turn(pid, cancelled, job):
                return _result(False, "Stopped while waiting for its turn to start the profile.", "cancelled")
            took_turn = True
            if self._memory_fits_now():
                break
            with self._lock:
                self._starting.pop(pid, None)
            took_turn = False
        turn_kept = False
        slot_id = f"warm-open:{label}:{job_id or int(self._wall())}"
        queue = self._slot_queue()
        began = time.monotonic()
        grant = None
        # ACL-2262: a publish job already holds its own queue slot for this profile; asking for a second
        # one would make the job wait for itself. Only an action without a slot (Check, Add account) asks.
        own_slot = False
        try:
            from src.fb_ads_rpa.run_telemetry import current_job_id

            holds = getattr(queue, "holds", None)
            own_slot = bool(queue is not None and callable(holds) and current_job_id()
                            and holds(current_job_id(), pid))
        except Exception:  # noqa: BLE001
            own_slot = False
        try:
            if queue is not None and not own_slot:
                # ACL-2278: a job waits for the place as long as it takes; others keep the time limit.
                grant = queue.acquire(slot_id, pid, cancelled=lambda: cancelled()
                                      or (job is None and time.monotonic() - began > START_SLOT_WAIT_S))
                if grant is None:
                    return _result(False, "Not started: not enough free memory for another profile. "
                                   "Close something and try again.", "memory_low")
            try:
                opened = self._start_profile(pid, folder, instance)
                turn_kept = bool(getattr(opened, "started_now", False))
            except _fb_profile.ProfileError as exc:
                code = getattr(exc, "code", "") or "profile_error"
                msg = HAND_MESSAGE if code == "open_by_hand" else str(exc)
                return _result(False, msg, code)
            except Exception as exc:  # noqa: BLE001 — MLX/сеть
                if "lock profile" in str(exc).lower():
                    # ACL-2262: process is gone but Multilogin still holds the profile lock. No retry loop.
                    return _result(False, f"Multilogin still holds the {label} profile after it closed; "
                                   "nothing was started. Try again in a minute.", "profile_locked")
                return _result(False, f"The profile could not be started with automation: "
                                      f"{scrub(str(exc))}", "start_failed")
        finally:
            if took_turn and not turn_kept:
                with self._lock:
                    self._starting.pop(pid, None)
            if queue is not None and grant is not None and not own_slot:
                queue.release(slot_id)   # место очереди заданий не держим; рост резервирует `_settling`
        started = bool(getattr(opened, "started_now", False))
        self.register(pid, account=label, port=int(opened.port), dashboard_started=started)
        if started:
            with self._lock:
                self._settling[pid] = self._mono()
                self._ready.discard(pid)
        log.info("warm: profile of %s %s (dashboard_started=%s)", label,
                 "started" if started else "was already open", started)
        return _result(True, "Opened; ready for the next action." if started
                       else "Already open with automation; kept warm.",
                       "started" if started else "already_open", started=started)

    def _wait_until_closed(self, pid: str, cancelled: Callable[[], bool]) -> str:
        """Local process look every `CLOSE_POLL_S` until the profile is gone (`none`) or the wait ends."""
        deadline = self._mono() + CLOSE_WAIT_S
        kind = "closing"
        while kind == "closing" and not cancelled() and self._mono() < deadline:
            self._sleep(CLOSE_POLL_S)
            kind = self._scan([pid]).get(pid, ("unknown", None))[0]
        return kind

    def _reopen_manual(self, label: str, entry: Any, *, job_id: str, cancelled) -> dict:
        """Explicitly authorized, graceful restart under the physical profile lease."""
        from src.fb_ads_rpa.operations import profile_lock
        from src.fb_ads_rpa.profile_lease import ProfileLeaseError
        pid = entry.profile_id.strip()
        try:
            with profile_lock(pid):
                if pid in self._foreign:
                    return _result(False, "Another dashboard owns this profile; nothing was changed.", "other_dashboard")
                sess = self.session(pid)
                queue = self._slot_queue()
                from src.fb_ads_rpa.run_telemetry import current_job_id
                if (sess is not None and sess.owner) or pid in self.gate.active() or (queue is not None and queue.others_on(pid,current_job_id())):
                    return _result(False, "An action is using this profile; nothing was changed.", "busy")
                kind, port = self._scan([pid]).get(pid,("unknown",None))
                if kind == "port" and port:
                    return self._adopt(label,pid,int(port))
                if kind != "no_port" or cancelled():
                    return _result(False, "The profile state changed; nothing was stopped.", "profile_changed")
                if self._close_window is None:
                    return _result(False, "Graceful profile closing is unavailable.", "no_closer")
                try:
                    closed = self._close_window(entry,label)
                except Exception as exc:
                    self._stops.record(pid)
                    return _result(False, f"Multilogin could not close the profile: {scrub(str(exc))}", "stop_failed")
                if closed == "still_running" or self._scan([pid]).get(pid,("unknown",None))[0] != "none":
                    self._stops.record(pid)
                    return _result(False, "Multilogin has not finished closing the profile; no start was sent.", "stop_failed")
                self._forget(pid,stopped=True)
                why = _stop_log.wait_or_refusal(pid,self._stops,cancelled=cancelled,sleep=self._sleep)
                if why:
                    return _result(False,why,"restart_cooldown")
                if cancelled():
                    return _result(False,"Stopped before reopening the profile.","cancelled")
                return self.open_one(label,entry,job_id=job_id,cancelled=cancelled)
        except ProfileLeaseError as exc:
            return _result(False,f"{exc} Nothing was changed.","profile_lease")

    def _adopt(self, label: str, pid: str, port: int) -> dict:
        """Профиль уже открыт с портом: берём в реестр, но не из-под чужого задания (S1, S2)."""
        with self._lock:
            foreign = pid in self._foreign
        if foreign:
            return _result(False, "This profile is kept warm by another dashboard window on this computer. "
                           "Use that window, or close it first. Nothing was changed.", "other_dashboard")
        # ACL-1853: замок профиля теперь по физическому UUID и отказывает словами (в карте не UUID,
        # профиль занят другой операцией ACM). Отказ — обычный ответ, а не исключение: иначе он
        # оборвал бы всю пачку `open_many`.
        from src.fb_ads_rpa.profile_lease import ProfileLeaseError

        try:
            return self._adopt_locked(label, pid, port)
        except ProfileLeaseError as exc:
            return _result(False, f"{exc} Nothing was changed.", "profile_lease")

    def _adopt_locked(self, label: str, pid: str, port: int) -> dict:
        from src.fb_ads_rpa.operations import profile_lock

        queue = self._slot_queue()
        # Под замком профиля: таймер льготы чтения берёт тот же замок и не успеет закрыть профиль
        # между «забрали отложенное закрытие» и «вернули его назад».
        with profile_lock(pid):
            prev = None
            try:
                prev = self._pending_close(pid)   # отложенное закрытие после чтения отменяется
            except Exception:
                log.exception("Could not cancel the pending close of %s", label)
            slot = getattr(prev, "slot_id", "") if prev is not None else ""
            from src.fb_ads_rpa.run_telemetry import current_job_id
            own_job = current_job_id()
            excluded = tuple(x for x in (slot,own_job) if x)
            excluded = excluded if len(excluded)>1 else (excluded[0] if excluded else "")
            try:
                # Свой слот чтения исключаем: он держится до закрытия, и без этого окно льготы не усыновить.
                if queue is not None and queue.others_on(pid, excluded):
                    if prev is not None:
                        self._pending_restore(pid, prev)
                    return _result(False, "A job is using this profile right now. Try again after it finishes. "
                                   "Nothing was changed.", "job_running")
            except Exception:
                log.exception("Could not ask the profile queue about %s", label)
            if slot and queue is not None:
                try:
                    queue.release(slot)
                except Exception:
                    log.exception("Could not release the slot held for %s", label)
        # Профиль, поднятый чтением дешборда, — «поднят дешбордом»: человек сам попросил его оставить.
        self.register(pid, account=label, port=port, dashboard_started=prev is not None)
        return _result(True, "Already open with automation; kept warm.", "already_open", started=False)

    def _memory_state(self) -> tuple[Any, Any, Any]:
        from src.web import profile_slots as _ps

        memory = (self._memory or _ps.read_memory)()
        settings = (self._settings or _ps.load_settings)()
        return memory, settings, _ps

    def _wait_for_memory(self, cancelled: Callable[[], bool]) -> str:
        """Пусто — памяти хватает. Иначе текст отказа.

        Нужно: профиль (+ запас) + недостающий рост уже запущенных профилей, которые ещё
        прогреваются (моложе `ACL_WARM_SETTLE_S`), с их ЗАМЕРЕННОЙ памятью, если замер есть.

        ACL-2278: задание (publish job) не получает отказ по времени: оно стоит в очереди (жёлтое, с
        цифрами «свободно / нужно»), пока память не появится или его не остановят. Остальные ждут
        `START_MEMORY_WAIT_S`; запрос подготовки соединения (`defer_memory_wait`) не ждёт вовсе.
        """
        job = _JOB_WAIT.get()
        defer = _DEFER_MEMORY.get() and job is None
        deadline = self._mono() + START_MEMORY_WAIT_S
        try:
            while True:
                memory, settings, _ps = self._memory_state()
                if memory is None:
                    return ""
                need, growth = self._memory_need(settings, _ps)
                if memory.available_mb >= need:
                    return ""
                used = _ps.used_pct(memory)
                text = (f"Not started: {memory.available_mb / 1024:.1f} GB free, "
                        f"{need / 1024:.1f} GB needed for another profile (memory is {used:.0f}% used; "
                        f"a profile starts while at most {settings.max_used_pct:.0f}% is used"
                        f"{', counting profiles still warming up' if growth else ''}). "
                        "Close something and try again.")
                if cancelled():
                    return "Stopped while waiting for memory. " + text if job is not None else text
                if job is None:
                    if defer or self._mono() >= deadline:
                        return text
                else:
                    _show(job, {"hold": "memory_low", "available_mb": int(memory.available_mb),
                                "need_mb": int(need), "reserve_mb": int(settings.reserve_mb),
                                "profile_mb": int(settings.profile_mb), "growth_mb": int(growth),
                                "max_used_pct": settings.max_used_pct, "used_pct": used,
                                "total_mb": int(memory.total_mb)})
                self._sleep(2.0)
        finally:
            _show(job, None)

    def _memory_fits_now(self) -> bool:
        """One look, no waiting: is there room for one more profile by the ACL-2255 rule?"""
        memory, settings, _ps = self._memory_state()
        if memory is None:
            return True
        need, _growth = self._memory_need(settings, _ps)
        return memory.available_mb >= need

    def _memory_need(self, settings: Any, _ps: Any) -> tuple[int, int]:
        now = self._mono()
        settle = _env_number(SETTLE_ENV, DEFAULT_SETTLE_S, 0.0, 3600.0)
        with self._lock:
            warm_ids = sorted(self._sessions)
            settling = {p for p, t in self._settling.items() if now - t < settle and p in self._sessions}
            self._settling = {p: t for p, t in self._settling.items() if p in settling}
        measured: dict[str, int] = {}
        usage = self._usage
        if usage is None and warm_ids:
            usage = self._usage = _ps.ProfileUsage()
        if usage is not None and warm_ids:
            probe = usage.read(warm_ids)
            if probe.get("status") == "ok":
                measured = {k: int(v) for k, v in (probe.get("per_profile") or {}).items()}
        # ACL-2255: the share of memory in use decides (a profile may start while at most `max_used_pct` is
        # used). A profile already measured is in that share; only one started so recently that no
        # measurement sees it yet is charged by the estimate.
        growth = sum(settings.profile_mb for p in settling if p not in measured)
        memory = (self._memory or _ps.read_memory)()
        keep = _ps.keep_free_mb(memory.total_mb, settings) if memory is not None else settings.reserve_mb
        return keep + growth, growth

    def open_many(self, items: list[tuple[str, Any]], *, job_id: str = "",
                  on_result: Callable[[str, dict], None] | None = None,
                  cancelled: Callable[[], bool] = lambda: False) -> dict[str, dict]:
        """Запуск по очереди; между настоящими запусками — пауза 5–10 с. Не больше одного профиля за раз."""
        results: dict[str, dict] = {}
        pending_pause = False
        from src.fb_api import mlx_session as _mlx

        # ACL-1826: пачка = это задание. После потери общей сессии слота B остальные B-старты
        # пропускаются с той же причиной (слот A идёт дальше).
        batch = job_id or f"open-{int(self._wall())}"
        _mlx.begin_batch(batch)
        for label, entry in items:
            if cancelled():
                break
            slot = (getattr(entry, "multilogin_instance", "") or "A").strip().upper() or "A"
            skip = _mlx.refusal(batch, slot, label)
            if skip:
                res = _result(False, skip["message"], skip["code"], skipped=True)
            else:
                if pending_pause:
                    self._sleep(self._stagger())
                with _mlx.scope(account=label, batch=batch):
                    res = self.open_one(label, entry, job_id=job_id, cancelled=cancelled)
            results[label] = res
            pending_pause = bool(res.get("started")) and not skip
            if on_result is not None:
                try:
                    on_result(label, res)
                except Exception:
                    pass
        return results

    # -- закрыть профили (только поднятые дешбордом)
    def close_one(self, label: str, entry: Any) -> dict:
        pid = (getattr(entry, "profile_id", "") or "").strip()
        if not pid:
            return _result(False, "No Multilogin profile is linked to this account.", "no_profile")
        kind, port = self._scan([pid]).get(pid, ("unknown", None))
        if kind == "no_port":
            return _result(False, "Not closed: this window was opened in Multilogin by hand. "
                           "Close it there.", "open_by_hand")
        sess = self.session(pid)
        if kind == "none":
            if sess is not None:
                self._forget(pid, stopped=True)
            return _result(True, "Already closed.", "already_closed")
        if kind != "port" or sess is None or not sess.dashboard_started or sess.port != int(port or 0):
            return _result(False, "Not closed: the dashboard did not start this profile, so it leaves it "
                           "open. Close it in Multilogin.", "not_ours")
        # Действие идёт — не закрываем из-под него.
        if not sess.lock.acquire(blocking=False):
            return _result(False, "Not closed: an action is running on this profile. Try again when it ends.",
                           "busy")
        try:
            try:
                if self._close_window is None:
                    return _result(False, "Closing profiles is not available here.", "no_closer")
                self._close_window(entry, label)   # мягкая остановка MLX, без kill
            except Exception as exc:  # noqa: BLE001
                # Профиль мог закрыться, хотя вызов упал: пауза перед запуском всё равно нужна.
                self._stops.record(pid)
                return _result(False, f"Multilogin did not close the profile: {scrub(str(exc))}. "
                                      "The window stays open.", "stop_failed")
            self._forget(pid, stopped=True)
        finally:
            sess.lock.release()
        log.info("warm: profile of %s closed on request", label)
        return _result(True, "Closed. It can be started again in about 3 minutes.", "closed")

    # -- аренда вкладки
    def checkout(self, profile_id: str, *, account: str, act: str, mode: str,
                 make_graph: Callable[[Any], Any], ready_timeout: float = 90.0,
                 on_wait: Callable[[str], None] | None = None) -> Lease:
        """Замок профиля → ворота → живая вкладка. Освобождает `Lease.release`."""
        sess = self.session(profile_id)
        if sess is None:
            raise WarmError("This profile is not kept warm.", "not_warm")
        # ACL-1138: и сессия, взятая в пул до гейта (старая сборка), — через гейт.
        # Пространство у сессии не записано: гейт спросит оба; «есть» кэшируется.
        refusal = _proxy_refusal(profile_id, "", account)
        if refusal:
            raise WarmError(refusal, "profile_no_proxy")
        if sess.owner == threading.get_ident():
            raise WarmError("This profile's tab is already in use by this action.", "nested")
        sess.lock.acquire()
        sess.owner = threading.get_ident()
        if self.session(profile_id) is not sess:
            # Пока ждали замок, сессию закрыли/забыли (Close profiles): работать не на чем.
            sess.owner = 0
            sess.lock.release()
            raise WarmError("This profile was closed or restarted while waiting; the next attempt "
                            "opens it again.", "profile_closed")
        job = _JOB_WAIT.get()
        try:
            if job is None:
                self.gate.acquire(profile_id, wait_s=_env_number(GATE_WAIT_ENV, DEFAULT_GATE_WAIT_S, 1.0, 3600.0),
                                  on_wait=on_wait)
            else:
                # ACL-2278: a job never fails for a busy gate or short memory: it stays queued until it can
                # go, or until the user stops it. Only local memory reads; no account is touched.
                try:
                    self.gate.acquire(profile_id, wait_s=float("inf"), on_wait=on_wait,
                                      cancelled=job.cancelled,
                                      on_tick=lambda why: _show(job, self.gate.wait_view(why)))
                finally:
                    _show(job, None)
            try:
                graph, state = self._prepare(sess, act, mode, make_graph, ready_timeout)
                self.mark_ready(profile_id)       # ACL-2283: the tab answered; the next profile may start
            except BaseException:
                self.gate.release(profile_id)
                with self._lock:
                    self._starting.pop(profile_id, None)    # ACL-2283: a failed prepare gives the turn back
                raise
        except BaseException:
            sess.owner = 0
            sess.lock.release()
            raise
        return Lease(self, sess, graph, state)

    def _finish(self, sess: WarmSession, stop: BaseException | None) -> None:
        try:
            if isinstance(stop, (LoggedOut, Checkpoint)):
                self._note_login_stop(sess)
            elif isinstance(stop, PageUnresponsive):
                self._drop_tab(sess)   # зависшую не копим: следующая — после охлаждения, свежая
        finally:
            self.gate.release(sess.profile_id)
            sess.owner = 0
            sess.lock.release()

    def _note_login_stop(self, sess: WarmSession) -> None:
        sess.logged_out = True
        self._drop_tab(sess)
        self._save()

    def _drop_tab(self, sess: WarmSession) -> None:
        tab, sess.tab, sess.graphs = sess.tab, None, {}
        if tab is not None:
            try:
                tab.close()
            except Exception:
                log.exception("Could not close the robot tab of %s", sess.account)

    def _verify_process(self, sess: WarmSession) -> None:
        """Профиль всё ещё наш и работает с портом; иначе — понятный отказ, запись снимается."""
        kind, port = self._scan([sess.profile_id]).get(sess.profile_id, ("unknown", None))
        if kind == "port" and int(port or 0) == sess.port:
            sess.verified = True
            return
        if kind == "port":
            # Порт другой: перезапуск не нами. Запись про него больше не годится.
            self._forget(sess.profile_id, stopped=False)
            raise WarmError(f"The {sess.account} profile was restarted since it was opened; "
                            "the next attempt connects to it again.", "profile_restarted")
        if kind == "no_port":
            self._forget(sess.profile_id, stopped=False)
            raise WarmError(HAND_MESSAGE, "open_by_hand")
        if kind == "closing":
            raise WarmError(f"The {sess.account} profile is closing; try again in a minute.", "profile_closing")
        if kind == "none":
            self._forget(sess.profile_id, stopped=True)
            # ACL-2262: no button and no waiting text. `prepare_existing` opens a closed profile before
            # the action; this is only the narrow race of a profile closing right after that check.
            raise WarmError(f"The {sess.account} profile closed while the action was starting; "
                            "the next attempt opens it again.", "profile_closed")
        raise WarmError(f"The {sess.account} profile could not be checked on this machine.", "profile_unknown")

    def _remaining(self, cap: float | None = None) -> float:
        deadline = _PREPARE_DEADLINE.get()
        left = 45.0 if deadline is None else deadline-self._mono()
        if left <= 0:
            raise PageFailed("Connection preparation timed out before the action was sent")
        if cap is not None:
            from src.fb_api.cdp import patient_active
            if patient_active():
                cap *= 4.0          # ACL-2283 (B): busy machine - navigation gets a longer cap too
        return min(left,cap) if cap is not None else left

    def _new_tab(self, sess: WarmSession) -> Any:
        if not sess.verified:
            self._verify_process(sess)
        try:
            tab = self._tab_factory(f"http://127.0.0.1:{sess.port}")
        except Exception:
            # Не подключились: профиль закрыли или порт устарел — скажет проверка процесса.
            sess.verified = False
            self._verify_process(sess)
            raise
        # Вкладка от прошлого запуска дешборда осталась в профиле — убираем, чтобы вкладка была одна.
        # Только вкладку умершего процесса (владелец записи мёртв): чужую живую не трогаем (S2).
        stale, sess.stale_target = sess.stale_target, ""
        if stale and stale != getattr(tab, "target_id", ""):
            closer = getattr(tab, "close_target", None)
            if callable(closer):
                closer(stale)
        sess.tab, sess.graphs = tab, {}
        sess.target_id = str(getattr(tab, "target_id", "") or "")
        sess.tab_since = self._wall()
        self._save()
        return tab

    def _fresh(self, sess: WarmSession, act: str, mode: str, make_graph: Callable[[Any], Any],
               timeout: float) -> tuple[Any, dict]:
        self._remaining()
        tab = self._new_tab(sess)
        self._remaining()
        graph = make_graph(tab)
        try:
            graph.install()
            graph.navigate(act, timeout=self._remaining(30.0))
            state = graph.wait_ready(timeout=self._remaining())
        except GraphStop as exc:
            if isinstance(exc, (LoggedOut, Checkpoint)):
                self._note_login_stop(sess)
            raise
        except BaseException:
            self._drop_tab(sess)
            raise
        self._raise_for_state(sess, state)
        self._remaining()
        sess.graphs = {mode: graph}
        sess.logged_out = False
        return graph, state

    def _raise_for_state(self, sess: WarmSession, state: dict) -> None:
        kind = state.get("kind")
        try:
            from src.fb_api import launch as _launch

            _launch.note_graph_session(sess.profile_id, bool(state.get("has_token")), account=sess.account)
        except Exception:  # noqa: BLE001 — телеметрия не роняет действие
            pass
        if kind == "checkpoint":
            self._note_login_stop(sess)
            raise Checkpoint("the profile is on a Facebook checkpoint")
        if kind == "login":
            self._note_login_stop(sess)
            raise LoggedOut("the profile is logged out of Facebook")
        if not state.get("has_token"):
            self._drop_tab(sess)
            raise PageFailed("Ads Manager did not finish loading (no Graph session in the page); "
                             "the profile may be logged out")

    def _reuse(self, sess: WarmSession, act: str, mode: str,
               make_graph: Callable[[Any], Any], timeout: float) -> tuple[Any, dict] | None:
        """Живая ли вкладка. `None` — мёртвая или зависшая (заменить один раз)."""
        tab = sess.tab
        url = tab.browser_url()
        if not url:
            return None                       # цель исчезла или браузер молчит
        stop = _url_stop(url)
        if stop is not None:
            self._note_login_stop(sess)
            raise stop
        if not _is_ads_manager(url):
            return None                       # about:blank / чужой сайт — не наша рабочая страница
        keep = getattr(tab, "keep_active", None)
        if callable(keep):
            try:
                keep()          # фокус-эмуляция живёт на сессии CDP: подтверждаем (только локальный CDP)
            except Exception:
                return None
        graph = sess.graphs.get(mode)
        if graph is None:
            graph = make_graph(tab)
        wanted = act[4:] if act.startswith("act_") else act
        if _url_act(url) and _url_act(url) != wanted:
            # Другой кабинет в том же профиле: одна загрузка Ads Manager нужного.
            try:
                graph.install()
                graph.navigate(act, timeout=self._remaining(30.0))
                state = graph.wait_ready(timeout=self._remaining())
            except (LoggedOut, Checkpoint) as exc:
                self._note_login_stop(sess)
                raise exc
            except Exception:
                return None
            self._raise_for_state(sess, state)
            self._remaining()
            sess.graphs = {mode: graph}
            return graph, state
        try:
            state = graph.live_state(timeout=self._remaining(5.0))
        except (LoggedOut, Checkpoint):
            self._note_login_stop(sess)
            raise
        except Exception:                      # PageUnresponsive, CdpError, TimeoutError: заменяем
            return None
        if state.get("kind") in ("login", "checkpoint"):
            self._raise_for_state(sess, state)
        if state.get("kind") != "ads_manager" or not state.get("has_token"):
            return None
        self._remaining()
        sess.graphs[mode] = graph
        sess.logged_out = False
        return graph, state

    def _prepare(self, sess: WarmSession, act: str, mode: str,
                 make_graph: Callable[[Any], Any], timeout: float) -> tuple[Any, dict]:
        """An absolute shared deadline covers CDP creation, reuse and one replacement."""
        from src.fb_api.cdp import command_budget, patient_wait
        budget = min(float(timeout),PREPARE_BUDGET_CAP_S)
        # ACL-2283 (B): busy machine (CPU at the level or other profiles warming) - a tab that has not
        # answered yet is not called unresponsive for several minutes. Idle machine: today's limit.
        patient_s = _env_number(PATIENT_READY_ENV, DEFAULT_PATIENT_READY_S, 0.0, 3600.0)
        check = None
        if patient_s > budget and self._busy_now(sess.profile_id):
            budget = patient_s
            until = self._mono()+budget
            seen = [self._mono(), True]

            def check() -> bool:
                now = self._mono()
                if now >= until:
                    return False
                if now - seen[0] >= 5.0:      # re-read the machine at most every 5 s; load gone = idle limit
                    seen[0], seen[1] = now, self._busy_now(sess.profile_id)
                return bool(seen[1])
        token = _PREPARE_DEADLINE.set(self._mono()+budget)
        try:
            with command_budget(budget), patient_wait(check) if check else contextlib.nullcontext():
                return self._prepare_owned(sess,act,mode,make_graph)
        except GraphStop:
            self._drop_tab(sess)
            raise
        finally:
            _PREPARE_DEADLINE.reset(token)

    def _busy_now(self, profile_id: str) -> bool:
        try:
            return bool(self.load_snapshot(profile_id)["busy"])
        except Exception:  # noqa: BLE001 - unknown = idle limit
            return False

    def _prepare_owned(self,sess,act,mode,make_graph):
        self._remaining()
        had_tab = sess.tab is not None and not getattr(sess.tab,"closed",False)
        if had_tab:
            got = self._reuse(sess,act,mode,make_graph,self._remaining())
            if got is not None:
                self._remaining()
                return got
            self._drop_tab(sess)
            sess.verified = False
        try:
            return self._fresh(sess,act,mode,make_graph,self._remaining())
        except PageUnresponsive:
            self._drop_tab(sess)
            if had_tab:
                raise
            self._remaining()
            sess.verified = False
            return self._fresh(sess,act,mode,make_graph,self._remaining())


# ───────────────────────── подставляемые части ─────────────────────────

def _chip(state: str, text: str, title: str, *, since: str = "", closable: bool = False,
          busy: bool = False) -> dict:
    return {"state": state, "text": text, "title": title, "since": since,
            "closable": bool(closable), "busy": bool(busy)}


def _proxy_refusal(profile_id: str, instance: str, label: str) -> str:
    """Текст отказа, если у профиля в Multilogin нет прокси (ACL-1138); иначе пусто."""
    from types import SimpleNamespace

    from src.fb_ads_rpa import profile_proxy_gate, profile_trouble

    try:
        profile_proxy_gate.ensure_profile_has_proxy(
            SimpleNamespace(profile_id=profile_id, multilogin_instance=instance), label or profile_id)
    except profile_trouble.ProfileStop as stop:
        return profile_trouble.message(stop.code) or str(stop)
    return ""


def _result(ok: bool, message: str, code: str, **extra: Any) -> dict:
    return {"ok": ok, "message": message, "code": code, "started": bool(extra.pop("started", False)),
            **extra}


def scan_local(profile_ids: list[str]) -> dict[str, tuple[str, int | None]]:
    """Один обход процессов на все профили: `{profile_id: (kind, port)}`. Только чтение."""
    try:
        import psutil  # type: ignore
    except Exception:
        return {pid: ("unknown", None) for pid in profile_ids}
    try:
        procs = list(psutil.process_iter(["name", "exe", "cmdline", "create_time"]))
    except Exception:
        return {pid: ("unknown", None) for pid in profile_ids}

    def listens(proc: Any, port: int) -> bool:
        try:
            return any(c.status == psutil.CONN_LISTEN and c.laddr.port == port
                       for c in proc.net_connections(kind="tcp"))
        except Exception:
            return False

    out: dict[str, tuple[str, int | None]] = {}
    for pid in profile_ids:
        try:
            # Браузер, переживший нашу недавнюю остановку, — "closing", а не порт.
            out[pid] = _fb_profile.local_debug_port(
                pid, process_iter=lambda: procs, profile_dir=_fb_profile.find_profile_dir(pid),
                listens=listens, stopped_at=_stop_log.default().recent_stop_at(pid))
        except Exception:
            out[pid] = ("unknown", None)
    return out


def _default_cpu() -> float | None:
    """Machine CPU share over a short local sample; None when it cannot be read."""
    try:
        import psutil  # type: ignore

        return float(psutil.cpu_percent(interval=0.5))
    except Exception:  # noqa: BLE001
        return None


def _default_tab(cdp_url: str) -> Any:
    from src.fb_api.cdp import PageTab

    return PageTab.open(cdp_url)


def _default_start(profile_id: str, folder_id: str, instance: str) -> Any:
    return _fb_profile.open_profile(profile_id, folder_id, instance)


def _default_pending_close(profile_id: str) -> Any:
    """Забрать у чтения отложенное закрытие профиля (`CLOSE_GRACE_S`) и вернуть его запись."""
    from src.analysis_agent import account_spend as _as

    with _as._PENDING_LOCK:
        return _as._PENDING_CLOSE.pop(profile_id, None)


def _default_pending_restore(profile_id: str, prev: Any) -> None:
    from src.analysis_agent import account_spend as _as

    with _as._PENDING_LOCK:
        _as._PENDING_CLOSE.setdefault(profile_id, prev)


def _default_slot_queue() -> Any:
    from src.web import publish_jobs

    return publish_jobs._slots


_REGISTRY: WarmRegistry | None = None
_REGISTRY_LOCK = threading.Lock()


def registry() -> WarmRegistry:
    global _REGISTRY
    with _REGISTRY_LOCK:
        if _REGISTRY is None:
            _REGISTRY = WarmRegistry()
            _REGISTRY._stops.on_stop(_REGISTRY.forget_stopped)
        return _REGISTRY
