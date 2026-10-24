"""ACL-2063: очередь обновления списка кампаний аккаунтов (только структура).

Один рабочий поток читает аккаунты по одному. Порядок: action > retry > attention
> schedule. Свежий аккаунт (снимок ok/partial/empty моложе часа) не читается. Сбой одного
аккаунта не останавливает очередь; сбой, общий для всех (Multilogin, чекпоинт,
потолок вызовов, остывание, выключенный API), ставит очередь на паузу с одной
причиной. Периода здесь нет: даты читать аккаунты не заставляют.
"""
from __future__ import annotations

from contextlib import contextmanager, ExitStack
import heapq
import hashlib
import logging
import os
import re
import threading
import time
import sys
from datetime import datetime, timezone
from typing import Callable, Literal

from src.analysis_agent import campaign_inventory
from src.analysis_agent.campaign_inventory import Snapshot, run_read
from src.analysis_agent.account_refresh_state import RefreshStore
from src.analysis_agent.account_refresh_machine import MachineStore, binding, CoordinationError

log = logging.getLogger(__name__)

FRESH_S = 3600.0
PRIORITY = {"action": 0, "retry": 1, "attention": 2, "schedule": 3}
WINDOWS = ("08:00", "13:00", "18:00")


def current_window(now: datetime) -> str | None:
    return next((w for w in reversed(WINDOWS) if now.strftime("%H:%M") >= w), None)


def auto_refresh_enabled(port: int) -> bool:
    value = os.environ.get("ACL_AUTO_REFRESH", "").strip().lower()
    if value:
        return value in {"1", "true", "yes", "on"}
    return bool(getattr(sys, "frozen", False)) or port == 9090

#: Коды ошибки снимка, общие для всех аккаунтов: код → (причина, сколько секунд ждать | None).
_GLOBAL_CODES: dict[str, tuple[str, float | None]] = {
    campaign_inventory.NEEDS_API_CODE: (
        "Account reads are off: the API backend is not enabled on this server. Enable it, then press Retry.", None),
    "mlx_session_lost": ("Multilogin lost its session. Reopen Multilogin, then press Retry.", None),
    "mlx_token_expired": ("The Multilogin token has expired. Update it in Multilogin, then press Retry.", None),
    "mlx_token_workspace": ("The Multilogin token belongs to another workspace. Fix it, then press Retry.", None),
    "call_budget": ("The call budget for account reads is used up. Raise the limit, then press Retry.", None),
    "coordination_unavailable": ("The machine account-read state is unavailable. Restore access, then press Retry.", None),
}

_MINUTES = re.compile(r"about (\d+) min", re.I)

#: Остановки Meta (`Snapshot.stop`), общие для всех аккаунтов: вид → (причина, ждать по таймеру?).
#: Чекпоинт и потолок вызовов сами не проходят: только явный Retry, который удался.
_GLOBAL_STOPS: dict[str, tuple[str, bool]] = {
    "checkpoint": ("A profile is on a Facebook checkpoint. Solve it inside Multilogin, then press Retry.", False),
    "rate_limited": ("Meta asked to slow down. Account reads are paused for a while.", True),
    "policy_block": ("Meta refused the requests. Account reads are paused for a while.", True),
    "call_budget": _GLOBAL_CODES["call_budget"][:1] + (False,),
}


def _parse_utc(value: str) -> float | None:
    try:
        return datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return None


def is_fresh(account: str, *, now: float | None = None) -> bool:
    """Успешный снимок, включая пустой аккаунт, моложе часа без стопа/упавшего шага."""
    snap = campaign_inventory.get_snapshot(account)
    if snap is None or snap.state not in ("ok", "partial", "empty"):
        return False
    # Неполное чтение после сбоя не считается удачей даже при сохранённых кампаниях.
    if classify(snap) != "ok":
        return False
    # Неудачная последняя попытка сохраняет хороший снимок для показа, но
    # его возраст не должен скрывать ошибку и Retry. Новая удача очищает attempt.
    attempt = campaign_inventory.get_attempt(account)
    if attempt and attempt.get("state") == "failed":
        return False
    done = _parse_utc(snap.finished_at_utc)
    if done is None:
        return False
    return 0 <= (time.time() if now is None else now) - done < FRESH_S


def recently_failed(account: str, *, now: float | None = None) -> bool:
    """Последняя попытка чтения не удалась и моложе `FRESH_S`: автоматические заявки её не повторяют.

    Только явные Retry/действие (force) читают такой аккаунт раньше срока.
    """
    attempt = campaign_inventory.get_attempt(account)
    if not attempt or attempt.get("state") != "failed":
        return False
    at = _parse_utc(attempt.get("at_utc") or "")
    if at is None:
        return False
    return 0 <= (time.time() if now is None else now) - at < FRESH_S


def _cooldown_s(kind: str, text: str) -> float:
    """Сколько ждать по таймеру: минуты из текста, иначе COOLDOWN_S по виду остановки."""
    from src.analysis_agent.account_spend_api import COOLDOWN_S

    table = float(COOLDOWN_S.get(kind, 300.0))
    mins = _MINUTES.search(text or "")
    # Минуты из текста не короче таблицы: подсказка «about 1 min» не отменяет остывание вида.
    return max(int(mins.group(1)) * 60.0, table) if mins else table


def _global_stop(snap: Snapshot) -> tuple[str, float | None] | None:
    """Общая остановка: `(причина по-английски, сколько секунд ждать | None)`.

    Сначала коды и вид остановки, строки — только запасной путь.
    """
    code = (snap.error_code or "").strip()
    if code in _GLOBAL_CODES:
        return _GLOBAL_CODES[code]
    kind = (snap.stop or "").strip()
    if kind in _GLOBAL_STOPS:
        reason, timed = _GLOBAL_STOPS[kind]
        return reason, (_cooldown_s(kind, snap.error) if timed else None)
    text = (snap.error or "").lower()
    if "10061" in text or "launcher.mlx.yt" in text or "multilogin is not running" in text:
        return ("Multilogin is not reachable on this computer. Start the Multilogin app, then press Retry.", None)
    if "checkpoint" in text:
        return _GLOBAL_STOPS["checkpoint"][0], None
    if "slow down" in text or "rate limit" in text:
        return _GLOBAL_STOPS["rate_limited"][0], _cooldown_s("rate_limited", text)
    if "call budget" in text:
        return _GLOBAL_CODES["call_budget"]
    return None


def classify(snap: Snapshot) -> Literal["ok", "account", "global"]:
    if _global_stop(snap):
        return "global"
    # Любой иной обрыв (включая PageFailed и обычную ошибку транспорта) оставляет
    # прочитанное доступным, но строка показывает причину и Retry. Обычное
    # ограничение числа страниц без упавших шагов остаётся пригодным partial.
    return "ok" if campaign_inventory.read_succeeded(snap) else "account"


def _halt_kind(snap: Snapshot) -> str:
    """Вид общей паузы: `checkpoint` снимается только чтением того же аккаунта, остальные - любым удачным."""
    kind = (snap.stop or "").strip()
    text = (snap.error or "").lower()
    return "checkpoint" if kind == "checkpoint" or (not kind and "checkpoint" in text) else (kind or "global")


class RefreshQueue:
    def __init__(self, reader: Callable[[str, str], Snapshot] | None = None,
                 clock: Callable[[], float] = time.time, *, store: RefreshStore | None = None,
                 accounts: Callable[[], list[str]] | None = None,
                 machine: MachineStore | None = None):
        self._reader = reader or (lambda account, trigger: run_read(account, trigger=trigger,
                                                                   machine=self._machine, now=self._clock))
        self._injected_reader = reader is not None
        self._clock = clock
        self._store = store
        self._accounts = accounts
        # An injected reader is a standalone/test executor; production queue()
        # always uses run_read and the real machine coordinator.
        self._machine = machine if machine is not None else (MachineStore() if reader is None and store else None)
        self._owner = hashlib.sha256(str(store.root.resolve()).encode()).hexdigest() if store else ""
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        #: Второй замок: два чтения одновременно не бывает ни при каком вызове _drain_once.
        self._drain_lock = threading.Lock()
        self._heap: list[tuple[int, int, str]] = []
        #: account → (приоритет, номер, триггер, force); устаревшие записи кучи отбрасываются по номеру
        self._entries: dict[str, tuple[int, int, str, bool]] = {}
        #: Аккаунты с явным Retry/действием: на паузе без таймера читаются в обход неё (только они).
        self._bypass: set[str] = set()
        self._seq = 0
        self._running: str | None = None
        self._halt: dict | None = None
        self._sweep: dict | None = None
        self._rows: dict[str, dict] = {}
        self._keys: dict[str, str] = {}
        self._thread: threading.Thread | None = None
        self._stop = False
        #: Заявки, отброшенные при выборе как уже свежие: телеметрию пишем вне замка.
        self._pending_skips: list[tuple[str, str]] = []

    def _bindings(self, accounts) -> dict[str, str]:
        keys = {}
        for account in accounts:
            try:
                keys[account] = binding(account)
            except (ValueError, CoordinationError):
                # A bad/removed map row must not prevent the other rows' reads.
                continue
        return keys

    def _invalid_row(self, account: str) -> None:
        self._entries.pop(account, None)
        self._rows[account] = {**self._rows.get(account, {}), "state": "failed",
                              "error": "This account needs a valid ad-account ID and physical profile in the account map."}
        self._sweep_complete(account)

    def _migrate_legacy(self) -> None:
        old = self._store.read()
        if old.get("machine_migrated"):
            return
        local = datetime.fromtimestamp(self._clock()).astimezone()
        window = current_window(local)
        keys = self._bindings(self._accounts() if self._accounts else [])
        if window:
            self._machine.migrate(self._owner, f"{local:%Y-%m-%d}/{window}", list(keys.values()),
                                  seen=window in old["windows_done"].get(f"{local:%Y-%m-%d}", []),
                                  halt=old.get("halt") or self._persisted_halt(), bindings=keys, now=self._clock())
        with self._store.transaction() as state:
            state["machine_migrated"] = True

    def _pull_results(self, accounts=None) -> None:
        if self._machine is None:
            return
        hold = self._machine.lease("account-structure-read.lock").holding(timeout=0)
        try:
            hold.__enter__()
        except TimeoutError:
            return  # status/admission remains responsive during a real profile read
        try:
            self._migrate_legacy()
            self._machine.recover_running(now=self._clock())
            members = list(accounts if accounts is not None else (self._accounts() if self._accounts else self._entries))
            keys = self._bindings(members)
            results = self._machine.results(list(keys.values()))
            for account, key in keys.items():
                self._import_result(account, shared=results.get(key), known=True)
        finally:
            hold.__exit__(None, None, None)

    def _import_result(self, account: str, *, shared=None, known: bool = False) -> None:
        if self._machine is None or not self._known_account(account):
            return
        key = binding(account)
        if not known:
            shared = self._machine.result(key)
        if not shared:
            return
        payload = shared["payload"]
        if shared.get("snapshot") and shared.get("snapshot_revision") != shared["revision"]:
            accepted = Snapshot(account=account, **{k: v for k, v in shared["snapshot"].items() if k != "ok"})
            accepted._machine_binding = key
            campaign_inventory.adopt_machine_result(accepted, shared["snapshot_revision"])
        snap = Snapshot(account=account, **{k: v for k, v in payload.items() if k != "ok"})
        snap._machine_binding = key
        if binding(account) != key:
            return
        if campaign_inventory.adopt_machine_result(snap, shared["revision"]):
            with self._state_guard():
                previous = self._rows.get(account, {}).get("list_at", "")
                self._rows[account] = {"list_at": snap.finished_at if payload["ok"] else previous,
                                       "state": snap.state if payload["ok"] else "failed", "error": snap.error}

    def _publish_result(self, snap: Snapshot, *, force: bool = False, recovery: int | None = None,
                        token: int | None = None, own_recovery: int | None = None) -> None:
        if self._machine is not None and not getattr(snap, "_machine_committed", False):
            with self._machine.lease("account-structure-read.lock").holding():
                key = binding(snap.account)
                revision = self._machine.publish(key, snap, now=self._clock(), force=force, recovery=recovery, own_recovery=own_recovery, token=token)
                snap._machine_revision = revision
                snap._machine_binding = key
                snap._machine_committed = True
                try:
                    campaign_inventory.put_result(snap)
                except Exception:
                    log.exception("The committed account result awaits its local cache projection")

    @contextmanager
    def _state_guard(self):
        """Сначала поток, затем короткий OS-замок; не держать его во время чтения."""
        with self._lock:
            if self._store is None:
                yield
                return
            with self._store.transaction() as state:
                self._seq = int(state.get("seq", 0))
                self._entries = {a: tuple(v) for a, v in state["entries"].items()}
                self._heap = [(v[0], v[1], a) for a, v in self._entries.items()]
                heapq.heapify(self._heap)
                self._running = state.get("running")
                self._rows = state["rows"]
                self._keys = state.get("machine_bindings", {})
                self._halt = state.get("halt")
                self._sweep = state.get("sweep")
                self._bypass = set(state.get("bypass", []))
                yield state
                state.update(seq=self._seq, entries=self._entries, running=self._running,
                             rows=self._rows, halt=self._halt, sweep=self._sweep,
                             bypass=sorted(self._bypass), machine_bindings=self._keys)

    def _known_account(self, account: str) -> bool:
        return self._accounts is None or account.casefold() in {a.casefold() for a in self._accounts()}

    def _sweep_complete(self, account: str, trigger: str = "") -> None:
        if self._machine and account in self._keys:
            self._machine.withdraw(self._keys[account], self._owner)
        if not self._sweep:
            return
        pending = self._sweep.get("_pending")
        if isinstance(pending, list):
            if account in pending:
                pending.remove(account)
            self._sweep["done"] = self._sweep["total"] - len(pending)
        elif trigger.split(":")[0] == "schedule":
            self._sweep["done"] = min(self._sweep["total"], self._sweep["done"] + 1)

    def page_open(self, *, now: datetime | None = None, port: int, view_only: bool = False) -> dict:
        """Первое обычное открытие страницы в окне; смена дат профили не открывает."""
        if view_only:
            return {"sweep": "view_only", "window": None, "queued": 0}
        if not auto_refresh_enabled(port):
            return {"sweep": "disabled", "window": None, "queued": 0}
        now = now or datetime.now().astimezone()
        window = current_window(now)
        if window is None:
            return {"sweep": "no_window", "window": None, "queued": 0}
        if self._store is None or self._accounts is None:
            raise RuntimeError("Scheduled account reads require persisted dashboard settings.")
        day = now.strftime("%Y-%m-%d")
        accounts = list({a.casefold(): a for a in self._accounts()}.values())
        machine_started = True
        if self._machine is not None:
            keys = self._bindings(accounts)
            self._migrate_legacy()
            machine_started = self._machine.admit(f"{day}/{window}", list(keys.values()), self._owner)
            self._pull_results(accounts)
        skipped = []
        queued = 0
        with self._state_guard() as state:
            seen = state["windows_done"].setdefault(day, [])
            tag = f"{day}/{window}"
            bindings = self._bindings(accounts) if self._machine else {}
            previous = state.setdefault("machine_members", {}).get(tag, {}) if self._machine else {}
            added = {a for a, key in bindings.items() if previous.get(a) != key}
            if window in seen and (not self._machine or not added):
                return {"sweep": "already_done", "window": window, "queued": 0}
            if window not in seen:
                seen.append(window)
            if self._machine:
                state["machine_members"][tag] = bindings
            self._maybe_lift()
            # Старые, ещё не начатые окна не догоняем. Действие/Retry/attention
            # остаются; идущее чтение не прерывается.
            self._entries = {a: v for a, v in self._entries.items() if v[2].split(":")[0] != "schedule"}
            self._sweep = {"window": f"{day}/{window}", "done": 0, "total": len(accounts),
                           "_pending": list(accounts)}
            label = f"schedule:{day}/{window}"
            for account in accounts:
                if self._machine and account not in bindings:
                    self._invalid_row(account)
                    continue
                if self._running == account:
                    continue
                if self._machine and self._machine.satisfied(bindings[account], tag):
                    self._sweep_complete(account)
                    continue
                if is_fresh(account, now=self._clock()):
                    snap = campaign_inventory.get_snapshot(account)
                    self._rows[account] = {"list_at": snap.finished_at, "state": snap.state, "error": ""}
                    self._sweep_complete(account)
                    skipped.append((account, label))
                elif recently_failed(account, now=self._clock()):
                    self._sweep_complete(account)
                else:
                    self._push(account, "schedule", label, False)
                    queued += 1
            self._wake.notify_all()
        for account, label in skipped:
            self._telemetry_skipped(account, label)
        return {"sweep": "started" if machine_started else "joined", "window": window, "queued": queued}

    # ───────── заявки ─────────

    def _persisted_halt(self) -> dict | None:
        """Пауза, восстановленная из хранилища: чекпоинт и потолок вызовов переживают рестарт
        и видны второму процессу дешборда (остановка записана в последней попытке аккаунта).
        """
        try:
            stops = campaign_inventory.latest_stops()
        except Exception:  # noqa: BLE001 — нечитаемое хранилище не должно валить очередь
            log.debug("persisted stops were not read", exc_info=True)
            return None
        now = self._clock()
        found: dict | None = None
        for account, (kind, at_utc) in sorted(stops.items()):
            if kind in ("checkpoint", "call_budget"):
                until = None
            elif kind in ("rate_limited", "policy_block"):
                at = _parse_utc(at_utc)
                if at is None:
                    continue
                until = at + _cooldown_s(kind, "")
                if now >= until:
                    continue
            else:
                continue
            halt = {"reason": _GLOBAL_STOPS[kind][0], "until": until, "kind": kind,
                    "account": account, "persisted": True}
            if kind == "checkpoint":
                return halt
            found = found or halt
        return found

    def _maybe_lift(self) -> None:
        if self._machine is not None:
            shared = self._machine.halt(now=self._clock())
            if shared:
                account = next((a for a, key in self._bindings(self._accounts() if self._accounts else []).items()
                                if key == shared["key"]), "")
                self._halt = {**shared, "account": account, "machine_generation": shared["generation"]}
                return
            if self._halt and "machine_generation" in self._halt:
                self._halt = None
        halt = self._halt
        if halt and halt.get("persisted"):
            self._halt = halt = None  # восстановленная пауза выводится заново: файл мог измениться
        if halt and halt["until"] is not None and self._clock() >= halt["until"]:
            self._halt = None
            self._bypass.clear()
        if self._halt is None:
            self._halt = self._persisted_halt()

    def request(self, account: str, trigger: str, *, force: bool = False, window: str = "") -> dict:
        """Поставить аккаунт в очередь.

        Явный `retry`/`action` (force) пересчитывает свежесть, но паузу всей очереди не снимает:
        без таймера он читает только свой аккаунт мимо паузы, и пауза уходит, лишь если чтение
        удалось; с таймером ждёт его конца. Остальные заявки на паузе встают в очередь.
        Автоматические заявки (`attention`, `schedule`) не читают аккаунт, чьё последнее чтение
        упало меньше часа назад. `window` помечает обход по расписанию в телеметрии.
        """
        if trigger not in PRIORITY:
            raise ValueError(f"unknown trigger {trigger!r}")
        label = f"schedule:{window}" if trigger == "schedule" and window else trigger
        skipped = False
        failed_recently = False
        halted = False
        with self._state_guard():
            self._maybe_lift()
            if not self._known_account(account):
                return {"queued": False, "reason": "removed"}
            if self._machine and account not in self._bindings([account]):
                self._invalid_row(account)
                return {"queued": False, "reason": "invalid_account_map"}
            if self._running == account:
                return {"queued": False, "reason": "running"}
            if not force and is_fresh(account, now=self._clock()):
                snap = campaign_inventory.get_snapshot(account)
                self._rows[account] = {"list_at": snap.finished_at if snap else "",
                                       "state": snap.state if snap else "", "error": ""}
                self._sweep_complete(account, label)
                skipped = True
            elif not force and recently_failed(account, now=self._clock()):
                failed_recently = True
            else:
                # A request joins a currently running read; inline action refreshes
                # use their already-open session and settle separately.
                self._push(account, trigger, label, force)
                halted = self._halt is not None
                if halted and force and trigger in ("retry", "action") and self._halt["until"] is None:
                    self._bypass.add(account)
                    halted = False
                self._wake.notify()
        if skipped:
            self._telemetry_skipped(account, label)
            return {"queued": False, "reason": "fresh"}
        if failed_recently:
            return {"queued": False, "reason": "recent_failure"}
        return {"queued": True, "reason": "halted" if halted else "queued"}

    def _push(self, account: str, trigger: str, label: str, force: bool) -> None:
        prio = PRIORITY[trigger]
        if self._machine is not None:
            key = binding(account)
            self._keys[account] = key
            self._machine.request(key, self._owner, prio, label, force)
        current = self._entries.get(account)
        if current is not None and current[0] <= prio:
            # Уже стоит с тем же или более высоким приоритетом; force не теряем.
            if force and not current[3]:
                self._entries[account] = (*current[:3], True)
            return
        self._seq += 1
        self._entries[account] = (prio, self._seq, label, force or bool(current and current[3]))
        heapq.heappush(self._heap, (prio, self._seq, account))

    def _telemetry_skipped(self, account: str, trigger: str) -> None:
        try:
            from src.analysis_agent import account_spend_api

            account_spend_api._telemetry_skip(account, trigger)  # noqa: SLF001
        except Exception:  # noqa: BLE001 — телеметрия не должна ломать очередь
            log.debug("skipped_fresh telemetry was not written", exc_info=True)

    def begin_sweep(self, window: str, total: int, *, accounts: list[str] | None = None) -> None:
        """Обход окна: показывает `Refreshing accounts done/total` (ставит Task 3)."""
        with self._state_guard():
            self._sweep = {"window": window, "done": 0, "total": total}
            if accounts is not None:
                self._sweep["_pending"] = list(accounts)

    def record_result(self, snap: Snapshot, *, trigger: str) -> None:
        """Настоящее чтение в уже открытой сессии команды; повторной заявки нет."""
        if not self._known_account(snap.account):
            return
        self._publish_result(snap, force=trigger in {"action", "retry"},
                             recovery=(self._halt or {}).get("machine_generation"))
        with self._state_guard():
            if not self._known_account(snap.account):
                return
            self._maybe_lift()
            bypassed = bool(trigger in {"action", "retry"} and self._halt
                            and self._halt["until"] is None)
            self._entries.pop(snap.account, None)  # this same-session result satisfies queued attention/schedule
            self._settle(snap.account, trigger, snap, bypassed)

    def state(self) -> dict:
        self._pull_results()
        with self._state_guard():
            self._maybe_lift()
            queued = [{"account": a, "trigger": e[2]}
                      for a, e in sorted(self._entries.items(), key=lambda kv: kv[1][:2])]
            return {"running": self._running, "queued": queued,
                    "halt": dict(self._halt) if self._halt else None,
                    "sweep": {k: v for k, v in self._sweep.items() if not k.startswith("_")} if self._sweep else None,
                    "rows": {a: dict(r) for a, r in self._rows.items()}}

    # ───────── работа ─────────

    def _pop(self) -> tuple[str, str, bool] | None:
        """Следующая заявка `(аккаунт, триггер, мимо паузы)` или None. Вызывать под замком."""
        if self._stop:
            return None
        self._maybe_lift()
        if self._halt is not None:
            # Пауза: только аккаунты с явным Retry/действием, самый приоритетный первым.
            ready = [(e[:2], a) for a, e in self._entries.items() if a in self._bypass]
            if not ready:
                return None
            _, account = min(ready)
            entry = self._entries.pop(account)
            self._bypass.discard(account)
            return account, entry[2], True
        self._bypass.clear()
        while self._heap:
            _prio, seq, account = heapq.heappop(self._heap)
            entry = self._entries.get(account)
            if entry is None or entry[1] != seq:
                continue  # заменена более приоритетной
            del self._entries[account]
            # A process may restart before the next page opening. Never replay
            # an unfinished, earlier schedule window on startup.
            if entry[2].startswith("schedule:") and "/" in entry[2]:
                local = datetime.fromtimestamp(self._clock()).astimezone()
                window = current_window(local)
                expected = f"schedule:{local:%Y-%m-%d}/{window}"
                if window is None or entry[2] != expected:
                    self._sweep_complete(account, entry[2])
                    continue
            if not self._known_account(account):
                self._sweep_complete(account, entry[2])
                continue
            # Свежесть проверяется и при выборе: пока заявка стояла, аккаунт могли прочитать
            # другой триггер или второй процесс дешборда. Явные заявки (force) читаются всегда.
            if not entry[3]:
                if is_fresh(account, now=self._clock()):
                    self._pending_skips.append((account, entry[2]))
                    self._sweep_complete(account, entry[2])
                    continue
                if recently_failed(account, now=self._clock()):
                    self._sweep_complete(account, entry[2])
                    continue
            return account, entry[2], False
        return None

    def _read(self, account: str, trigger: str) -> Snapshot:
        try:
            if not self._known_account(account):
                return Snapshot(account=account, state="failed", error="This account was removed from the dashboard.")
            if self._machine is not None and self._injected_reader:
                key = binding(account)
                self._machine.recover_running(now=self._clock())
                own_recovery = self._machine.stop_generation(key)
                token = self._machine.begin(key, self._owner, trigger)
                with self._machine.executing(token):
                    try:
                        snap = self._reader(account, trigger)
                    except Exception as exc:
                        stamp = datetime.fromtimestamp(self._clock(), timezone.utc)
                        snap = Snapshot(account=account, state="failed", error=f"{type(exc).__name__}: {exc}",
                                        finished_at=stamp.astimezone().strftime("%Y-%m-%d %H:%M:%S"),
                                        finished_at_utc=stamp.isoformat())
                    self._publish_result(snap, force=trigger.split(":")[0] in {"action", "retry"},
                                         recovery=(self._halt or {}).get("machine_generation"), own_recovery=own_recovery, token=token)
                    return snap
            snap = self._reader(account, trigger)
            self._publish_result(snap, force=trigger.split(":")[0] in {"action", "retry"},
                                 recovery=(self._halt or {}).get("machine_generation"))
            return snap
        except Exception as exc:  # noqa: BLE001
            return Snapshot(account=account, state="failed", error=f"{type(exc).__name__}: {exc}")

    def _drain_once(self) -> int:
        if self._machine is not None:
            hold = self._machine.lease(f"worker-{self._owner}.lock").holding(timeout=0)
            try:
                hold.__enter__()
            except TimeoutError:
                return 0
            try:
                return self._drain_machine()
            finally:
                hold.__exit__(None, None, None)
        return self._drain_owned()

    @contextmanager
    def _execution_available(self, account: str):
        """Never occupy the machine dispatcher while waiting for a live target."""
        from src.analysis_agent import account_spend
        from src.analysis_agent.account_refresh_state import target_lease, read_session
        from src.fb_ads_rpa.operations import browser_operation
        stack = ExitStack()
        try:
            stack.enter_context(browser_operation.reading())
            physical = account_spend._lock_for(account)
            if not physical.acquire(blocking=False):
                yield False
                return
            stack.callback(physical.release)
            try:
                stack.enter_context(target_lease(account_spend._profile_key(account)).holding(timeout=0))
                stack.enter_context(read_session(priority=False, timeout=0))
            except TimeoutError:
                stack.close()
                yield False
                return
            yield True
        finally:
            stack.close()

    def _requeue(self, account: str, trigger: str, force: bool, bypassed: bool) -> None:
        with self._state_guard():
            self._push(account, trigger.split(":")[0], trigger, force)
            if bypassed:
                self._bypass.add(account)

    def _execute_selected(self, account: str, trigger: str, bypassed: bool) -> Snapshot | None:
        with self._state_guard():
            self._running = account
            self._rows[account] = {**self._rows.get(account, {}), "state": "updating", "error": ""}
        snap = None
        try:
            snap = self._read(account, trigger)
        finally:
            with self._state_guard():
                self._running = None
                if snap is not None:
                    self._settle(account, trigger, snap, bypassed)
                else:
                    previous = self._rows.get(account, {}).get("list_at", "")
                    self._rows[account] = {"list_at": previous, "state": "failed",
                                           "error": "The read was interrupted. Press Retry."}
                    self._sweep_complete(account, trigger)
        return snap

    def _drain_machine(self) -> int:
        if not self._drain_lock.acquire(blocking=False):
            return 0
        local = self._store.dispatch_lock.holding(timeout=0)
        entered = False
        try:
            try:
                local.__enter__()
                entered = True
            except TimeoutError:
                return 0
            done, blocked = 0, set()
            with self._state_guard():
                if self._running is not None:
                    previous = self._rows.get(self._running, {}).get("list_at", "")
                    self._rows[self._running] = {"list_at": previous, "state": "failed",
                                                "error": "The read was interrupted. Press Retry."}
                    self._sweep_complete(self._running)
                    self._running = None
            while True:
                self._pull_results()
                with self._state_guard():
                    deferred = {a: self._entries.pop(a) for a in blocked if a in self._entries}
                    self._heap = [(e[0], e[1], a) for a, e in self._entries.items()]
                    heapq.heapify(self._heap)
                    forces = {a: e[3] for a, e in self._entries.items()}
                    item = self._pop()
                    self._entries.update(deferred)
                    skips, self._pending_skips = self._pending_skips, []
                for acc, label in skips:
                    self._telemetry_skipped(acc, label)
                if item is None:
                    return done
                account, trigger, bypassed = item
                force = forces[account]
                try:
                    key = binding(account)
                except (ValueError, CoordinationError):
                    with self._state_guard():
                        self._invalid_row(account)
                    continue
                with self._execution_available(account) as available:
                    self._machine.availability(key, self._owner, available)
                    if not available:
                        self._requeue(account, trigger, force, bypassed)
                        self._machine.availability(key, self._owner, False)
                        blocked.add(account)
                        continue
                    dispatch = self._machine.lease("dispatch.lock").holding(timeout=0)
                    try:
                        dispatch.__enter__()
                    except TimeoutError:
                        self._requeue(account, trigger, force, bypassed)
                        return done
                    try:
                        if not self._machine.may_dispatch(key):
                            self._requeue(account, trigger, force, bypassed)
                            return done
                        self._import_result(account)
                        if force and not self._machine.pending(key, self._owner):
                            with self._state_guard():
                                self._sweep_complete(account, trigger)
                            continue
                        if not force and (is_fresh(account, now=self._clock()) or recently_failed(account, now=self._clock())):
                            with self._state_guard():
                                self._sweep_complete(account, trigger)
                            continue
                        snap = self._execute_selected(account, trigger, bypassed)
                        if snap is not None and not getattr(snap, "_machine_paused", None):
                            self._machine.finish(key, getattr(snap, "_machine_revision", None))
                            done += 1
                    finally:
                        dispatch.__exit__(None, None, None)
        finally:
            if entered:
                local.__exit__(None, None, None)
            self._drain_lock.release()

    def _drain_owned(self) -> int:
        """Прочитать всё, что стоит и не на паузе. Возвращает число чтений."""
        if not self._drain_lock.acquire(blocking=False):
            return 0  # читает другой вызов: два чтения сразу не бывает
        dispatch = None
        try:
            if self._store is not None:
                lock = self._store.dispatch_lock
                dispatch = lock.holding(timeout=0)
                try:
                    dispatch.__enter__()
                except TimeoutError:
                    dispatch = None
                    return 0
                with self._state_guard():
                    # OS-аренда свободна: сохранённое running осталось от прерванного процесса.
                    if self._running is not None:
                        row = self._rows.get(self._running, {})
                        self._rows[self._running] = {"list_at": row.get("list_at", ""), "state": "failed",
                                                     "error": "The read was interrupted. Press Retry."}
                        self._sweep_complete(self._running)
                        self._running = None
            done = 0
            while True:
                with self._state_guard():
                    item = self._pop()
                    skips, self._pending_skips = self._pending_skips, []
                    if item is not None:
                        account, trigger, bypassed = item
                        self._running = account
                        self._rows[account] = {**self._rows.get(account, {}), "state": "updating", "error": ""}
                for acc, label in skips:
                    self._telemetry_skipped(acc, label)
                if item is None:
                    return done
                snap = None
                try:
                    snap = self._read(account, trigger)
                finally:
                    with self._state_guard():
                        self._running = None
                        if snap is not None:
                            self._settle(account, trigger, snap, bypassed)
                        else:
                            # Чтение оборвано не-Exception (выход, KeyboardInterrupt): строка не висит в «updating».
                            prev = self._rows.get(account, {}).get("list_at", "")
                            self._rows[account] = {"list_at": prev, "state": "failed",
                                                   "error": "The read was interrupted. Press Retry."}
                            self._sweep_complete(account, trigger)
                done += 1
        finally:
            if dispatch is not None:
                dispatch.__exit__(None, None, None)
            self._drain_lock.release()

    def _settle(self, account: str, trigger: str, snap: Snapshot, bypassed: bool) -> None:
        """Записать итог чтения. Под замком."""
        paused = getattr(snap, "_machine_paused", None)
        if paused:
            self._halt = {**paused, "account": account, "machine_generation": paused["generation"]}
            self._push(account, trigger.split(":")[0], trigger, bypassed)
            return
        verdict = classify(snap)
        self._sweep_complete(account, trigger)
        prev_at = self._rows.get(account, {}).get("list_at", "")
        if verdict == "ok":
            self._rows[account] = {"list_at": snap.finished_at, "state": snap.state, "error": ""}
            halt = self._halt
            if bypassed and halt is not None:
                # Пауза чекпоинта снимается только удачным чтением аккаунта, который её поднял:
                # удача соседнего профиля ничего не говорит о чекпоинте этого.
                if halt.get("kind") != "checkpoint" or halt.get("account") == account:
                    self._halt = None
                    self._bypass.clear()
            return
        why = snap.error or (snap.problems[-1] if snap.problems else "") or snap.error_code or snap.stop or "failed"
        self._rows[account] = {"list_at": prev_at, "state": "failed", "error": why}
        if verdict == "global":
            reason, wait = _global_stop(snap)  # type: ignore[misc]
            self._halt = {"reason": reason, "until": self._clock() + wait if wait else None,
                          "kind": _halt_kind(snap), "account": account}

    def _has_work(self) -> bool:
        self._maybe_lift()
        if self._halt is None:
            return bool(self._entries)
        return any(a in self._bypass for a in self._entries)

    def _loop(self) -> None:
        if self._machine is not None:
            while not self._stop:
                try:
                    with self._machine.lease(f"worker-{self._owner}.lock").holding(timeout=0):
                        self._loop_owned()
                    return
                except TimeoutError:
                    with self._wake:
                        if not self._stop:
                            self._wake.wait(timeout=1)
        else:
            self._loop_owned()

    def _loop_owned(self) -> None:
        while True:
            try:
                self._pull_results()
                with self._state_guard():
                    if self._stop:
                        if self._thread is threading.current_thread():
                            self._thread = None
                        return
                    ready = self._has_work()
                if ready:
                    self._drain_once()
            except Exception:  # noqa: BLE001
                log.exception("Account refresh worker failed")
            # Только локальная очередь/таймер уже разрешённой паузы. Новое окно
            # и чтение Meta этот опрос не создаёт. OS-замка здесь нет.
            with self._wake:
                if not self._stop:
                    self._wake.wait(timeout=1.0)

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                # Старый поток ещё жив (дочитывает после stop с истёкшим ожиданием): второго не заводим,
                # а отменяем остановку, иначе после чтения очередь осталась бы без рабочего.
                self._stop = False
                return
            self._stop = False
            self._thread = threading.Thread(target=self._loop, name="account-refresh", daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        """Остановить рабочий поток: новых чтений не начнётся; идущее дочитывается."""
        with self._lock:
            self._stop = True
            self._wake.notify_all()
            thread = self._thread
        if thread is None:
            return
        thread.join(timeout=timeout)
        with self._lock:
            if not thread.is_alive() and self._thread is thread:
                self._thread = None


_QUEUE: RefreshQueue | None = None
_QUEUE_LOCK = threading.Lock()


def queue() -> RefreshQueue:
    global _QUEUE
    with _QUEUE_LOCK:
        if _QUEUE is None:
            _QUEUE = RefreshQueue(store=RefreshStore(), accounts=dashboard_accounts)
        return _QUEUE


def dashboard_accounts() -> list[str]:
    """Актуальные локальные привязки, не копия списка из HTML/прошлого окна."""
    from src.meta_campaign_agent.ad_account_map import load_ad_account_map
    from config import paths

    return list(load_ad_account_map(paths.get_ad_account_map_path()))


def resolve_account(account: str) -> str | None:
    wanted = account.strip().casefold()
    return next((a for a in dashboard_accounts() if a.casefold() == wanted), None)


def stop_queue() -> None:
    if _QUEUE is not None:
        _QUEUE.stop()
