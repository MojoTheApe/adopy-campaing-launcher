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
from src.fb_api.graph import Checkpoint, GraphStop, LoggedOut, PageFailed, PageUnresponsive
from src.fb_api.telemetry import scrub

log = logging.getLogger(__name__)

RESTART_COOLDOWN_ENV = "ACL_PROFILE_RESTART_COOLDOWN_S"
DEFAULT_RESTART_COOLDOWN_S = 180.0
MAX_HEAVY_ENV = "ACL_WARM_MAX_HEAVY"
GATE_WAIT_ENV = "ACL_WARM_GATE_WAIT_S"
DEFAULT_GATE_WAIT_S = 300.0
#: Пауза между запусками профилей «на сегодня» (секунды, случайно в диапазоне).
STAGGER_MIN_S = 5.0
STAGGER_MAX_S = 10.0
#: Сколько ждём места по памяти под запуск одного профиля.
START_SLOT_WAIT_S = 120.0

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
    return _env_number(RESTART_COOLDOWN_ENV, DEFAULT_RESTART_COOLDOWN_S, 0.0, 3600.0)


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
    """JSON-файл в каталоге настроек машины: профили реестра и последние остановки.

    Локальный, не в гите (`config.paths.get_local_config_dir`). Содержимое — id
    профилей, порты и время: токенов и паролей здесь нет.
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

    def load(self) -> dict:
        with self._lock:
            try:
                data = json.loads(self._file().read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
        if not isinstance(data, dict):
            data = {}
        data.setdefault("profiles", {})
        data.setdefault("stops", {})
        return data

    def save(self, data: dict) -> None:
        with self._lock:
            try:
                target = self._file()
                target.parent.mkdir(parents=True, exist_ok=True)
                tmp = target.with_suffix(".tmp")
                tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
                os.replace(tmp, target)
            except OSError:
                log.exception("Could not save the warm session registry")


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
        if n == 0:
            return True, "first"
        if n >= limit:
            return False, "limit"
        memory = self._memory()
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
                "target_id": self.target_id}


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
                 sleep: Callable[[float], None] = time.sleep,
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
        self._sleep = sleep
        self._stagger = stagger
        self._lock = threading.RLock()
        self._sessions: dict[str, WarmSession] = {}
        self._stops: dict[str, float] = {}
        self._load()

    # -- хранилище
    def _load(self) -> None:
        data = self._store.load()
        for pid, rec in (data.get("profiles") or {}).items():
            if not isinstance(rec, dict):
                continue
            self._sessions[pid] = WarmSession(
                profile_id=pid, account=str(rec.get("account") or ""), port=int(rec.get("port") or 0),
                started_at=float(rec.get("started_at") or 0.0),
                dashboard_started=bool(rec.get("dashboard_started")),
                logged_out=bool(rec.get("logged_out")), target_id=str(rec.get("target_id") or ""))
        self._stops = {k: float(v) for k, v in (data.get("stops") or {}).items()
                       if isinstance(v, (int, float))}

    def _save(self) -> None:
        with self._lock:
            data = {"profiles": {pid: s.record() for pid, s in self._sessions.items()},
                    "stops": dict(self._stops)}
        self._store.save(data)

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
            # «Дешборд поднял» не пропадает от повторного открытия уже работающего профиля.
            sess.dashboard_started = bool(sess.dashboard_started or dashboard_started)
        self._save()
        return sess

    def _forget(self, profile_id: str, *, stopped: bool) -> None:
        with self._lock:
            sess = self._sessions.pop(profile_id, None)
            if stopped:
                self._stops[profile_id] = self._wall()
        # Вкладку не закрываем: профиль уже ушёл (или это уже не наш экземпляр), и
        # закрытие по мёртвому сокету только ждало бы таймаута. Ссылку отпускаем.
        if sess is not None:
            sess.tab, sess.graphs = None, {}
        self._save()

    def record_stop(self, profile_id: str) -> None:
        with self._lock:
            self._stops[profile_id] = self._wall()
        self._save()

    def restart_wait_s(self, profile_id: str) -> float:
        """Сколько секунд ещё нельзя запускать профиль после его остановки."""
        with self._lock:
            last = self._stops.get(profile_id)
        if last is None:
            return 0.0
        return max(0.0, last + restart_cooldown_s() - self._wall())

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
            self.register(pid, account=label, port=int(port), dashboard_started=False)
            return _result(True, "Already open with automation; kept warm.", "already_open", started=False)
        if kind == "no_port":
            return _result(False, HAND_MESSAGE, "open_by_hand")
        if kind == "closing":
            return _result(False, "The profile is closing; try again in a minute. Nothing was started.",
                           "profile_closing")
        wait = self.restart_wait_s(pid)
        if wait > 0:
            return _result(False, (f"This profile was closed {int((restart_cooldown_s() - wait))} s ago. "
                                   f"Wait about {int(wait // 60) + 1} min before starting it again "
                                   "(a quick restart can log the account out). Nothing was started."),
                           "restart_cooldown", wait_s=round(wait))
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
                queue.release(slot_id)   # профиль простаивает: место по памяти не держим
        started = bool(getattr(opened, "started_now", False))
        self.register(pid, account=label, port=int(opened.port), dashboard_started=started)
        log.info("warm: profile of %s %s (dashboard_started=%s)", label,
                 "started" if started else "was already open", started)
        return _result(True, "Opened; ready for the next action." if started
                       else "Already open with automation; kept warm.",
                       "started" if started else "already_open", started=started)

    def open_many(self, items: list[tuple[str, Any]], *, job_id: str = "",
                  on_result: Callable[[str, dict], None] | None = None,
                  cancelled: Callable[[], bool] = lambda: False) -> dict[str, dict]:
        """Запуск по очереди; между настоящими запусками — пауза 5–10 с. Не больше одного профиля за раз."""
        results: dict[str, dict] = {}
        pending_pause = False
        for label, entry in items:
            if cancelled():
                break
            if pending_pause:
                self._sleep(self._stagger())
            res = self.open_one(label, entry, job_id=job_id, cancelled=cancelled)
            results[label] = res
            pending_pause = bool(res.get("started"))
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
        stale = sess.target_id
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
        procs = list(psutil.process_iter(["name", "exe", "cmdline"]))
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
            out[pid] = _fb_profile.local_debug_port(
                pid, process_iter=lambda: procs, profile_dir=_fb_profile.find_profile_dir(pid),
                listens=listens)
        except Exception:
            out[pid] = ("unknown", None)
    return out


def _default_tab(cdp_url: str) -> Any:
    from src.fb_api.cdp import PageTab

    return PageTab.open(cdp_url)


def _default_start(profile_id: str, folder_id: str, instance: str) -> Any:
    return _fb_profile.open_profile(profile_id, folder_id, instance)


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
        return _REGISTRY
