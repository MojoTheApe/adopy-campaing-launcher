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

log = logging.getLogger(__name__)

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
SETTLE_ENV = "ACL_WARM_SETTLE_S"
DEFAULT_SETTLE_S = 90.0

#: Состояния для экрана.
WARM = "warm"
OPEN = "open"
CLOSED = "closed"
LOGGED_OUT = "logged_out"
BY_HAND = "by_hand"
CLOSING = "closing"
UNKNOWN = "unknown"

HAND_MESSAGE = ("Opened in Multilogin by hand — close it there, wait about 3 minutes, "
                "then use Open profiles for today")

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
            # S3: и первое действие не обходит запас: ниже него ждёт, а не лезет в память.
            if memory is not None and memory.available_mb < self._settings().reserve_mb:
                return False, "below_reserve"
            return True, "first"
        if n >= limit:
            return False, "limit"
        if memory is None:
            return (n < min(self._ps.FALLBACK_LIMIT, limit)), "memory_unknown"
        settings = self._settings()
        per_mb = settings.profile_mb
        probe = self._usage.read(sorted(self._active)) if self._usage is not None else {}
        measured = [int(v) for v in (probe.get("per_profile") or {}).values()]
        if probe.get("status") == "ok" and measured:
            per_mb = max(max(measured), settings.profile_floor_mb)
        ok = memory.available_mb >= per_mb + settings.reserve_mb
        return ok, "memory_ok" if ok else "memory_low"

    def acquire(self, profile_id: str, *, wait_s: float = DEFAULT_GATE_WAIT_S,
                on_wait: Callable[[str], None] | None = None) -> None:
        """Ждать места. Не дождались за `wait_s` — `WarmError(code="gate_wait")`."""
        with self._cond:
            if profile_id in self._active:
                self._active[profile_id] += 1
                return
            deadline = self._clock() + wait_s
            told = ""
            while True:
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
                         "Use Open profiles for today to keep it ready.")
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
        kind, port = self._scan([pid]).get(pid, ("unknown", None))
        if kind == "port" and port:
            return self._adopt(label, pid, int(port))
        if kind == "no_port":
            return _result(False, HAND_MESSAGE, "open_by_hand")
        if kind == "closing":
            return _result(False, "The profile is closing; try again in a minute. Nothing was started.",
                           "profile_closing")
        if kind == "none" and self.session(pid) is not None:
            # B2: профиль в реестре, а процесса нет — его закрыли (человек, таймер чтения).
            # Останавливаем отсчёт от момента, когда увидели, и только потом решаем про запуск.
            self._forget(pid, stopped=True)
        why = _stop_log.refusal(pid, self._stops)
        if why:
            return _result(False, why, "restart_cooldown", wait_s=round(self.restart_wait_s(pid)))
        # S3: память под ещё один профиль с учётом роста уже запущенных и прогревающихся.
        short = self._wait_for_memory(cancelled)
        if short:
            return _result(False, short, "memory_low")
        slot_id = f"warm-open:{label}:{job_id or int(self._wall())}"
        queue = self._slot_queue()
        began = time.monotonic()
        grant = None
        try:
            if queue is not None:
                grant = queue.acquire(slot_id, pid, cancelled=lambda: cancelled()
                                      or time.monotonic() - began > START_SLOT_WAIT_S)
                if grant is None:
                    return _result(False, "Not started: not enough free memory for another profile. "
                                   "Close something and try again.", "memory_low")
            try:
                opened = self._start_profile(pid, folder, instance)
            except _fb_profile.ProfileError as exc:
                code = getattr(exc, "code", "") or "profile_error"
                msg = HAND_MESSAGE if code == "open_by_hand" else str(exc)
                return _result(False, msg, code)
            except Exception as exc:  # noqa: BLE001 — MLX/сеть
                return _result(False, f"The profile could not be started with automation: "
                                      f"{scrub(str(exc))}", "start_failed")
        finally:
            if queue is not None and grant is not None:
                queue.release(slot_id)   # место очереди заданий не держим; рост резервирует `_settling`
        started = bool(getattr(opened, "started_now", False))
        self.register(pid, account=label, port=int(opened.port), dashboard_started=started)
        if started:
            with self._lock:
                self._settling[pid] = self._mono()
        log.info("warm: profile of %s %s (dashboard_started=%s)", label,
                 "started" if started else "was already open", started)
        return _result(True, "Opened; ready for the next action." if started
                       else "Already open with automation; kept warm.",
                       "started" if started else "already_open", started=started)

    def _adopt(self, label: str, pid: str, port: int) -> dict:
        """Профиль уже открыт с портом: берём в реестр, но не из-под чужого задания (S1, S2)."""
        with self._lock:
            foreign = pid in self._foreign
        if foreign:
            return _result(False, "This profile is kept warm by another dashboard window on this computer. "
                           "Use that window, or close it first. Nothing was changed.", "other_dashboard")
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
            try:
                # Свой слот чтения исключаем: он держится до закрытия, и без этого окно льготы не усыновить.
                if queue is not None and queue.others_on(pid, slot):
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
        """Пусто — памяти хватает. Иначе текст отказа после ожидания `START_MEMORY_WAIT_S`.

        Нужно: профиль (+ запас) + недостающий рост уже запущенных профилей, которые ещё
        прогреваются (моложе `ACL_WARM_SETTLE_S`), с их ЗАМЕРЕННОЙ памятью, если замер есть.
        """
        deadline = self._mono() + START_MEMORY_WAIT_S
        while True:
            memory, settings, _ps = self._memory_state()
            if memory is None:
                return ""
            need, growth = self._memory_need(settings, _ps)
            if memory.available_mb >= need:
                return ""
            if cancelled() or self._mono() >= deadline:
                return (f"Not started: {memory.available_mb / 1024:.1f} GB free, "
                        f"{need / 1024:.1f} GB needed for another profile (new profile, safety reserve"
                        f"{' and the growth of profiles still warming up' if growth else ''}). "
                        "Close something and try again.")
            self._sleep(2.0)

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
        profile_mb = settings.profile_mb
        if measured:
            profile_mb = max(max(measured.values()), settings.profile_floor_mb)
        growth = 0
        for p in settling:
            growth += max(0, profile_mb - measured.get(p, 0))
        return profile_mb + settings.reserve_mb + growth, growth

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
        if sess.owner == threading.get_ident():
            raise WarmError("This profile's tab is already in use by this action.", "nested")
        sess.lock.acquire()
        sess.owner = threading.get_ident()
        if self.session(profile_id) is not sess:
            # Пока ждали замок, сессию закрыли/забыли (Close profiles): работать не на чем.
            sess.owner = 0
            sess.lock.release()
            raise WarmError("This profile was closed or restarted while waiting. Use Open profiles "
                            "for today first.", "profile_closed")
        try:
            self.gate.acquire(profile_id, wait_s=_env_number(GATE_WAIT_ENV, DEFAULT_GATE_WAIT_S, 1.0, 3600.0),
                              on_wait=on_wait)
            try:
                graph, state = self._prepare(sess, act, mode, make_graph, ready_timeout)
            except BaseException:
                self.gate.release(profile_id)
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
            raise WarmError(f"The {sess.account} profile was restarted since it was opened. "
                            "Use Open profiles for today to keep it warm again.", "profile_restarted")
        if kind == "no_port":
            self._forget(sess.profile_id, stopped=False)
            raise WarmError(HAND_MESSAGE, "open_by_hand")
        if kind == "closing":
            raise WarmError(f"The {sess.account} profile is closing; try again in a minute.", "profile_closing")
        if kind == "none":
            self._forget(sess.profile_id, stopped=True)
            raise WarmError(f"The {sess.account} profile was closed. Use Open profiles for today to start it "
                            "again (about 3 minutes after it closed).", "profile_closed")
        raise WarmError(f"The {sess.account} profile could not be checked on this machine.", "profile_unknown")

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
        tab = self._new_tab(sess)
        graph = make_graph(tab)
        try:
            graph.install()
            graph.navigate(act, timeout=60.0)
            state = graph.wait_ready(timeout=timeout)
        except GraphStop as exc:
            if isinstance(exc, (LoggedOut, Checkpoint)):
                self._note_login_stop(sess)
            raise
        except BaseException:
            self._drop_tab(sess)
            raise
        self._raise_for_state(sess, state)
        sess.graphs = {mode: graph}
        sess.logged_out = False
        return graph, state

    def _raise_for_state(self, sess: WarmSession, state: dict) -> None:
        kind = state.get("kind")
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
                graph.navigate(act, timeout=60.0)
                state = graph.wait_ready(timeout=timeout)
            except (LoggedOut, Checkpoint) as exc:
                self._note_login_stop(sess)
                raise exc
            except Exception:
                return None
            self._raise_for_state(sess, state)
            sess.graphs = {mode: graph}
            return graph, state
        try:
            state = graph.live_state()
        except (LoggedOut, Checkpoint):
            self._note_login_stop(sess)
            raise
        except Exception:                      # PageUnresponsive, CdpError, TimeoutError: заменяем
            return None
        if state.get("kind") in ("login", "checkpoint"):
            self._raise_for_state(sess, state)
        if state.get("kind") != "ads_manager" or not state.get("has_token"):
            return None
        sess.graphs[mode] = graph
        sess.logged_out = False
        return graph, state

    def _prepare(self, sess: WarmSession, act: str, mode: str,
                 make_graph: Callable[[Any], Any], timeout: float) -> tuple[Any, dict]:
        if sess.tab is not None and not getattr(sess.tab, "closed", False):
            got = self._reuse(sess, act, mode, make_graph, timeout)
            if got is not None:
                return got
            # Мёртвая или зависшая: заменяем РОВНО один раз.
            log.info("warm: robot tab of %s is not usable; replacing it once", sess.account)
            self._drop_tab(sess)
            sess.verified = False
        return self._fresh(sess, act, mode, make_graph, timeout)


# ───────────────────────── подставляемые части ─────────────────────────

def _chip(state: str, text: str, title: str, *, since: str = "", closable: bool = False,
          busy: bool = False) -> dict:
    return {"state": state, "text": text, "title": title, "since": since,
            "closable": bool(closable), "busy": bool(busy)}


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
