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

#: Остывание по умолчанию, если в тексте нет минут (секунды; как COOLDOWN_S в account_spend_api).
_CHECKPOINT_WAIT_S = 600.0
_RATE_LIMIT_WAIT_S = 300.0
_MINUTES = re.compile(r"about (\d+) min", re.I)


def _parse_utc(value: str) -> float | None:
    try:
        return datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return None


def is_fresh(account: str, *, now: float | None = None) -> bool:
    """Последний снимок ok/partial моложе `FRESH_S`."""
    snap = campaign_inventory.get_snapshot(account)
    if snap is None or snap.state not in ("ok", "partial"):
        return False
    done = _parse_utc(snap.finished_at_utc)
    if done is None:
        return False
    return 0 <= (time.time() if now is None else now) - done < FRESH_S


def _global_stop(snap: Snapshot) -> tuple[str, float | None] | None:
    """Общая остановка: `(причина по-английски, сколько секунд ждать | None)`."""
    code = (snap.error_code or "").strip()
    if code in _GLOBAL_CODES:
        return _GLOBAL_CODES[code]
    # Кодов у остальных остановок нет — текст запасной путь.
    text = (snap.error or "").lower()
    if "10061" in text or "launcher.mlx.yt" in text or "multilogin is not running" in text:
        return ("Multilogin is not reachable on this computer. Start the Multilogin app, then press Retry.", None)
    mins = _MINUTES.search(text)
    wait = int(mins.group(1)) * 60.0 if mins else None
    if "checkpoint" in text:
        return ("A profile is on a Facebook checkpoint. Solve it inside Multilogin, then press Retry.",
                wait or _CHECKPOINT_WAIT_S)
    if "slow down" in text or "rate limit" in text:
        return ("Meta asked to slow down. Account reads are paused for a while.", wait or _RATE_LIMIT_WAIT_S)
    if "call budget" in text:
        return _GLOBAL_CODES["call_budget"]
    return None


def classify(snap: Snapshot) -> Literal["ok", "account", "global"]:
    if snap.state != "failed":
        return "ok"
    return "global" if _global_stop(snap) else "account"


class RefreshQueue:
    def __init__(self, reader: Callable[[str, str], Snapshot] | None = None,
                 clock: Callable[[], float] = time.time):
        self._reader = reader or (lambda account, trigger: run_read(account, trigger=trigger))
        self._clock = clock
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        self._heap: list[tuple[int, int, str]] = []
        #: account → (приоритет, номер, триггер, force); устаревшие записи кучи отбрасываются по номеру
        self._entries: dict[str, tuple[int, int, str, bool]] = {}
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

    def request(self, account: str, trigger: str, *, force: bool = False) -> dict:
        if trigger not in PRIORITY:
            raise ValueError(f"unknown trigger {trigger!r}")
        skipped = False
        with self._lock:
            self._maybe_lift()
            if force and trigger in ("retry", "action"):
                self._halt = None
            if self._halt is not None:
                return {"queued": False, "reason": "halted"}
            if self._running == account:
                return {"queued": False, "reason": "running"}
            if not force and is_fresh(account, now=self._clock()):
                snap = campaign_inventory.get_snapshot(account)
                self._rows[account] = {"list_at": snap.finished_at if snap else "",
                                       "state": snap.state if snap else "", "error": ""}
                skipped = True
            else:
                self._push(account, trigger, force)
                self._wake.notify()
        if skipped:
            self._telemetry_skipped(account, trigger)
            return {"queued": False, "reason": "fresh"}
        return {"queued": True, "reason": "queued"}

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

            account_spend_api._telemetry("skipped_fresh", account, True, 0.0,  # noqa: SLF001
                                         {"trigger": trigger})
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

    def _pop(self) -> tuple[str, str] | None:
        """Следующая заявка или None (пусто / пауза). Вызывать под замком."""
        self._maybe_lift()
        if self._halt is not None:
            return None
        while self._heap:
            _prio, seq, account = heapq.heappop(self._heap)
            entry = self._entries.get(account)
            if entry is None or entry[1] != seq:
                continue  # заменена более приоритетной
            del self._entries[account]
            return account, entry[2]
        return None

    def _read(self, account: str, trigger: str) -> Snapshot:
        try:
            return self._reader(account, trigger)
        except Exception as exc:  # noqa: BLE001
            return Snapshot(account=account, state="failed", error=f"{type(exc).__name__}: {exc}")

    def _drain_once(self) -> int:
        """Прочитать всё, что стоит и не на паузе. Возвращает число чтений."""
        done = 0
        while True:
            with self._lock:
                item = self._pop()
                if item is None:
                    return done
                account, trigger = item
                self._running = account
                self._rows[account] = {**self._rows.get(account, {}), "state": "updating", "error": ""}
            snap = self._read(account, trigger)
            verdict = classify(snap)
            with self._lock:
                self._running = None
                if self._sweep and trigger == "schedule":
                    self._sweep["done"] = min(self._sweep["total"], self._sweep["done"] + 1)
                if verdict == "ok":
                    self._rows[account] = {"list_at": snap.finished_at, "state": snap.state, "error": ""}
                else:
                    self._rows[account] = {"list_at": self._rows.get(account, {}).get("list_at", ""),
                                           "state": "failed", "error": snap.error or snap.error_code or "failed"}
                    if verdict == "global":
                        reason, wait = _global_stop(snap)  # type: ignore[misc]
                        self._halt = {"reason": reason,
                                      "until": self._clock() + wait if wait else None}
            done += 1

    def _has_work(self) -> bool:
        self._maybe_lift()
        return bool(self._entries) and self._halt is None

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
                return
            self._stop = False
            self._thread = threading.Thread(target=self._loop, name="account-refresh", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        with self._lock:
            self._stop = True
            self._wake.notify_all()
            thread = self._thread
        if thread is not None:
            thread.join(timeout=10.0)
        self._thread = None


_QUEUE: RefreshQueue | None = None
_QUEUE_LOCK = threading.Lock()


def queue() -> RefreshQueue:
    global _QUEUE
    with _QUEUE_LOCK:
        if _QUEUE is None:
            _QUEUE = RefreshQueue()
        return _QUEUE
