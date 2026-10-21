"""ACL-2063: очередь обновления списка кампаний аккаунтов (только структура).

Один рабочий поток читает аккаунты по одному. Порядок: action > retry > attention
> schedule. Свежий аккаунт (снимок ok/partial моложе часа) не читается. Сбой одного
аккаунта не останавливает очередь; сбой, общий для всех (Multilogin, чекпоинт,
потолок вызовов, остывание, выключенный API), ставит очередь на паузу с одной
причиной. Периода здесь нет: даты читать аккаунты не заставляют.
"""
from __future__ import annotations

import heapq
import logging
import re
import threading
import time
from datetime import datetime
from typing import Callable, Literal

from src.analysis_agent import campaign_inventory
from src.analysis_agent.campaign_inventory import Snapshot, run_read

log = logging.getLogger(__name__)

FRESH_S = 3600.0
PRIORITY = {"action": 0, "retry": 1, "attention": 2, "schedule": 3}

#: Коды ошибки снимка, общие для всех аккаунтов: код → (причина, сколько секунд ждать | None).
_GLOBAL_CODES: dict[str, tuple[str, float | None]] = {
    campaign_inventory.NEEDS_API_CODE: (
        "Account reads are off: the API backend is not enabled on this server. Enable it, then press Retry.", None),
    "mlx_session_lost": ("Multilogin lost its session. Reopen Multilogin, then press Retry.", None),
    "mlx_token_expired": ("The Multilogin token has expired. Update it in Multilogin, then press Retry.", None),
    "mlx_token_workspace": ("The Multilogin token belongs to another workspace. Fix it, then press Retry.", None),
    "call_budget": ("The call budget for account reads is used up. Raise the limit, then press Retry.", None),
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
    """Последний снимок ok/partial моложе `FRESH_S`, без стопа и без упавшего шага."""
    snap = campaign_inventory.get_snapshot(account)
    if snap is None or snap.state not in ("ok", "partial"):
        return False
    # Обрыв на стопе Meta или упавший шаг — снимок неполный, свежим не считается.
    if snap.stop or any(v == "failed" for v in snap.steps.values()):
        return False
    done = _parse_utc(snap.finished_at_utc)
    if done is None:
        return False
    return 0 <= (time.time() if now is None else now) - done < FRESH_S


def _cooldown_s(kind: str, text: str) -> float:
    """Сколько ждать по таймеру: минуты из текста, иначе COOLDOWN_S по виду остановки."""
    from src.analysis_agent.account_spend_api import COOLDOWN_S

    mins = _MINUTES.search(text or "")
    if mins:
        return int(mins.group(1)) * 60.0
    return float(COOLDOWN_S.get(kind, 300.0))


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
    # Стоп бывает и у «partial»-снимка без ошибки: клиент оборвал чтение на adsets/ads.
    if snap.state != "failed" and not snap.stop:
        return "ok"
    return "global" if _global_stop(snap) else ("account" if snap.state == "failed" else "ok")


class RefreshQueue:
    def __init__(self, reader: Callable[[str, str], Snapshot] | None = None,
                 clock: Callable[[], float] = time.time):
        self._reader = reader or (lambda account, trigger: run_read(account, trigger=trigger))
        self._clock = clock
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
        self._thread: threading.Thread | None = None
        self._stop = False

    # ───────── заявки ─────────

    def _maybe_lift(self) -> None:
        halt = self._halt
        if halt and halt["until"] is not None and self._clock() >= halt["until"]:
            self._halt = None
            self._bypass.clear()

    def request(self, account: str, trigger: str, *, force: bool = False) -> dict:
        """Поставить аккаунт в очередь.

        Явный `retry`/`action` (force) пересчитывает свежесть, но паузу всей очереди не снимает:
        без таймера он читает только свой аккаунт мимо паузы, и пауза уходит, лишь если чтение
        удалось; с таймером ждёт его конца. Остальные заявки на паузе встают в очередь.
        """
        if trigger not in PRIORITY:
            raise ValueError(f"unknown trigger {trigger!r}")
        skipped = False
        halted = False
        with self._lock:
            self._maybe_lift()
            if self._running == account:
                return {"queued": False, "reason": "running"}
            if not force and is_fresh(account, now=self._clock()):
                snap = campaign_inventory.get_snapshot(account)
                self._rows[account] = {"list_at": snap.finished_at if snap else "",
                                       "state": snap.state if snap else "", "error": ""}
                skipped = True
            else:
                self._push(account, trigger, force)
                halted = self._halt is not None
                if halted and force and trigger in ("retry", "action") and self._halt["until"] is None:
                    self._bypass.add(account)
                    halted = False
                self._wake.notify()
        if skipped:
            self._telemetry_skipped(account, trigger)
            return {"queued": False, "reason": "fresh"}
        return {"queued": True, "reason": "halted" if halted else "queued"}

    def _push(self, account: str, trigger: str, force: bool) -> None:
        prio = PRIORITY[trigger]
        current = self._entries.get(account)
        if current is not None and current[0] <= prio:
            # Уже стоит с тем же или более высоким приоритетом; force не теряем.
            if force and not current[3]:
                self._entries[account] = (*current[:3], True)
            return
        self._seq += 1
        self._entries[account] = (prio, self._seq, trigger, force or bool(current and current[3]))
        heapq.heappush(self._heap, (prio, self._seq, account))

    def _telemetry_skipped(self, account: str, trigger: str) -> None:
        try:
            from src.analysis_agent import account_spend_api

            account_spend_api._telemetry_skip(account, trigger)  # noqa: SLF001
        except Exception:  # noqa: BLE001 — телеметрия не должна ломать очередь
            log.debug("skipped_fresh telemetry was not written", exc_info=True)

    def begin_sweep(self, window: str, total: int) -> None:
        """Обход окна: показывает `Refreshing accounts done/total` (ставит Task 3)."""
        with self._lock:
            self._sweep = {"window": window, "done": 0, "total": total}

    def state(self) -> dict:
        with self._lock:
            self._maybe_lift()
            queued = [{"account": a, "trigger": e[2]}
                      for a, e in sorted(self._entries.items(), key=lambda kv: kv[1][:2])]
            return {"running": self._running, "queued": queued,
                    "halt": dict(self._halt) if self._halt else None,
                    "sweep": dict(self._sweep) if self._sweep else None,
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
            return account, entry[2], False
        return None

    def _read(self, account: str, trigger: str) -> Snapshot:
        try:
            return self._reader(account, trigger)
        except Exception as exc:  # noqa: BLE001
            return Snapshot(account=account, state="failed", error=f"{type(exc).__name__}: {exc}")

    def _drain_once(self) -> int:
        """Прочитать всё, что стоит и не на паузе. Возвращает число чтений."""
        if not self._drain_lock.acquire(blocking=False):
            return 0  # читает другой вызов: два чтения сразу не бывает
        try:
            done = 0
            while True:
                with self._lock:
                    item = self._pop()
                    if item is None:
                        return done
                    account, trigger, bypassed = item
                    self._running = account
                    self._rows[account] = {**self._rows.get(account, {}), "state": "updating", "error": ""}
                snap = None
                try:
                    snap = self._read(account, trigger)
                finally:
                    with self._lock:
                        self._running = None
                        if snap is not None:
                            self._settle(account, trigger, snap, bypassed)
                done += 1
        finally:
            self._drain_lock.release()

    def _settle(self, account: str, trigger: str, snap: Snapshot, bypassed: bool) -> None:
        """Записать итог чтения. Под замком."""
        verdict = classify(snap)
        if self._sweep and trigger == "schedule":
            self._sweep["done"] = min(self._sweep["total"], self._sweep["done"] + 1)
        prev_at = self._rows.get(account, {}).get("list_at", "")
        if verdict == "ok":
            self._rows[account] = {"list_at": snap.finished_at, "state": snap.state, "error": ""}
            if bypassed and self._halt is not None:
                self._halt = None  # Retry удался — пауза без таймера снята
                self._bypass.clear()
            return
        why = snap.error or (snap.problems[-1] if snap.problems else "") or snap.error_code or snap.stop or "failed"
        self._rows[account] = {"list_at": prev_at, "state": "failed", "error": why}
        if verdict == "global":
            reason, wait = _global_stop(snap)  # type: ignore[misc]
            self._halt = {"reason": reason, "until": self._clock() + wait if wait else None}

    def _has_work(self) -> bool:
        self._maybe_lift()
        if self._halt is None:
            return bool(self._entries)
        return any(a in self._bypass for a in self._entries)

    def _loop(self) -> None:
        while True:
            with self._lock:
                while not self._stop and not self._has_work():
                    self._wake.wait(timeout=5.0)
                if self._stop:
                    return
            try:
                self._drain_once()
            except Exception:  # noqa: BLE001
                log.exception("Account refresh worker failed")
                time.sleep(1.0)

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return  # старый поток ещё жив (в том числе дочитывает после stop): второго не заводим
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
            _QUEUE = RefreshQueue()
        return _QUEUE
