"""Команды над существующими кампаниями: статус, бюджет, ставка (ACL-1800).

Решение владельца (2026-10-11): кампании через API создаются опубликованными и
на паузе; ВКЛЮЧАЕТ, ВЫКЛЮЧАЕТ и правит бюджет/ставку ЧЕЛОВЕК из дешборда, с
подтверждением. Робот сам ничего не включает; ни автоматики, ни правил здесь нет.

Жизнь операции: Preview → Running → Verifying → Succeeded / Failed /
Outcome unknown.

* `preview` читает объекты и строит неизменяемый разбор (revision): что
  затронуто, до → после, валюта, что пропущено и почему, ограничения родителей
  («On · Campaign paused»). Родителя эта команда не трогает НИКОГДА.
* `execute` берёт revision и ключ идемпотентности, СНАЧАЛА перечитывает текущие
  значения и отказывается писать, если хоть одно изменилось с превью. Запись —
  одна на объект, без повторов; потом чтение и сравнение.
* Таймаут записи = «результат неизвестен»: сверка чтением, никаких повторов.
* История операций лежит рядом с прочими локальными файлами
  (`paths.get_local_config_dir()`); актор — метка покупателя, не авторизация.

Этот модуль не знает про браузер: сессию (профиль → вкладка → клиент Graph) ему
даёт `open_session(account, commands)` — см. `src/web/campaign_commands.py`.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import threading
import time
from contextlib import AbstractContextManager
import logging
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable

from src.fb_api import write_policy as _wp
from src.fb_api.creator import ZERO_DECIMAL, budget_minor
from src.fb_api.graph import GraphError, GraphStop, WriteUnknown, WritesBlocked
from src.fb_api.telemetry import scrub

log = logging.getLogger(__name__)

# ───────────────────────── константы ─────────────────────────

PREVIEW, RUNNING, VERIFYING = "Preview", "Running", "Verifying"
SUCCEEDED, FAILED, UNKNOWN = "Succeeded", "Failed", "Outcome unknown"
FINAL_STATES = frozenset({SUCCEEDED, FAILED, UNKNOWN})

LEVELS = ("campaign", "adset", "ad")
COMMANDS = ("set_status", "set_budget", "set_bid")
#: Сколько объектов за одну команду: вызовов на объект — чтение, запись, сверка.
MAX_TARGETS = 20
#: Сколько живёт превью, секунд.
PREVIEW_TTL_S = 900.0
#: Пауза перед сверкой записи без ответа: запрос мог ещё лететь.
RECONCILE_PAUSE_S = 4.0

#: Стратегии ставки, где у группы есть ручная ставка (`bid_amount`).
BID_STRATEGIES = {"LOWEST_COST_WITH_BID_CAP": "Bid cap", "COST_CAP": "Cost cap"}
AUTOMATIC_BID = "LOWEST_COST_WITHOUT_CAP"
AUTOMATIC_BID_TEXT = "Automatic · no manual bid"

#: Поля Graph для чтения по уровням. Родитель — вложенным полем (один вызов).
READ_FIELDS = {
    "campaign": "name,account_id,status,effective_status,daily_budget,lifetime_budget,bid_strategy",
    "adset": ("name,account_id,status,effective_status,daily_budget,lifetime_budget,bid_amount,"
              "bid_strategy,campaign_id,campaign{id,name,status,effective_status,daily_budget,bid_strategy}"),
    "ad": ("name,account_id,status,effective_status,adset_id,campaign_id,"
           "adset{id,name,status,effective_status},campaign{id,name,status,effective_status}"),
}
STATUS_WORD = {"ACTIVE": "On", "PAUSED": "Off"}
LEVEL_WORD = {"campaign": "Campaign", "adset": "Ad set", "ad": "Ad"}

_ID_RE = re.compile(r"[1-9][0-9]{4,}", re.ASCII)
_KEY_RE = re.compile(r"[A-Za-z0-9_.:\-]{8,80}", re.ASCII)
_DEC_RE = re.compile(r"[0-9]{1,12}(?:\.[0-9]{1,2})?", re.ASCII)


class CommandRefused(ValueError):
    """Команда отклонена с текстом человеку; к Meta ничего не ушло.

    `status` — HTTP-код для эндпоинта (400 неверный запрос, 409 конфликт).
    """

    def __init__(self, message: str, *, status: int = 400, code: str = ""):
        super().__init__(scrub(message))
        self.status = status
        self.code = code


class HistoryUnavailable(OSError):
    """Журнал операций не записался (append / flush / fsync) — писать в Meta нельзя."""


HISTORY_FAILED = ("The operation history could not be saved on this computer "
                  "(disk full or not writable).")


# ───────────────────────── хранилище операций ─────────────────────────

def default_history_path() -> Path:
    from config import paths

    return Path(paths.get_local_config_dir()) / "campaign_command_history.jsonl"


class OperationStore:
    """Операции в памяти + журнал на диске (по строке на каждый переход).

    В журнал попадает всё, что начало исполняться (с Running); превью живёт
    только в памяти. Операция, застрявшая в Running/Verifying при загрузке
    (процесс умер посреди), помечается «Outcome unknown» — результат не сверен.
    """

    def __init__(self, path: Path | str | None = None, *, clock: Callable[[], float] = time.time):
        self.path = Path(path) if path else default_history_path()
        self.clock = clock
        self._lock = threading.RLock()
        self._ops: dict[str, dict] = {}
        self._load()

    @staticmethod
    def now_iso(ts: float) -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            lines = self.path.read_bytes().split(b"\n")
        except OSError:
            return
        for line in lines:
            try:
                # строка, оборванная посреди символа (диск кончился), — пропускается (ACL-1855)
                op = json.loads(line.decode("utf-8"))
            except ValueError:   # UnicodeDecodeError тоже ValueError
                continue
            if isinstance(op, dict) and op.get("op_id"):
                self._ops[str(op["op_id"])] = op
        for op in list(self._ops.values()):
            if op.get("refresh_pending"):
                # The old process/session is gone. Preserve the command result;
                # never replay its write or claim its refresh is still running.
                op["refresh_pending"] = False
                self.try_save(op)
            if op.get("state") in (RUNNING, VERIFYING):
                items = op.get("affected", [])
                # «saved»/«mismatch» — сверка уже показала, что запись дошла (ACL-1855:
                # рестарт после сверки не должен превращать их в «не отправлено»).
                maybe_sent = any(i.get("result") in ("sending", "sent", "unknown", "saved", "mismatch")
                                 for i in items)
                if maybe_sent:
                    for item in items:
                        if item.get("result") in ("sending", "sent"):
                            item["result"] = "unknown"
                        elif item.get("result") in ("", "pending"):
                            item["result"] = "not_attempted"
                    _conclude(op)
                    if op["state"] == UNKNOWN:
                        # Запись могла уйти: результат не сверен.
                        op["message"] = ("The dashboard stopped before the result was verified. "
                                         "Check the objects in Ads Manager; nothing was retried.")
                else:
                    op["state"] = FAILED
                    op["message"] = "The dashboard stopped before anything was sent. Nothing was written."
                    for item in items:
                        if item.get("result") in ("", "pending"):
                            item["result"] = "not_sent"
                self.try_save(op)

    @staticmethod
    def _open_append(path: Path):
        return path.open("a+b")

    def _persist(self, op: dict) -> None:
        """Дописать строку и дождаться fsync. Сбой — `HistoryUnavailable`, не молчание (ACL-1855)."""
        op["updated_at"] = self.now_iso(self.clock())
        line = (json.dumps(op, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._open_append(self.path) as fh:
                fh.seek(0, os.SEEK_END)
                if fh.tell():
                    fh.seek(-1, os.SEEK_END)
                    if fh.read(1) != b"\n":
                        # прошлая строка оборвана сбоем: не приклеивать к ней новую
                        line = b"\n" + line
                fh.write(line)
                fh.flush()
                os.fsync(fh.fileno())   # след переживает падение процесса
        except OSError as exc:
            raise HistoryUnavailable(str(exc)) from exc

    # -- доступ
    def add(self, op: dict) -> None:
        with self._lock:
            self._ops[op["op_id"]] = op

    def get(self, op_id: str) -> dict | None:
        with self._lock:
            return self._ops.get(op_id)

    def by_revision(self, revision_id: str) -> dict | None:
        with self._lock:
            for op in self._ops.values():
                if op.get("revision_id") == revision_id:
                    return op
        return None

    def by_key(self, key: str) -> dict | None:
        with self._lock:
            for op in self._ops.values():
                if op.get("idempotency_key") == key:
                    return op
        return None

    def save(self, op: dict) -> None:
        """Обязательное сохранение: перед любой записью в Meta. Бросает `HistoryUnavailable`."""
        with self._lock:
            self._persist(op)

    def try_save(self, op: dict) -> bool:
        """Сохранение по возможности (после записи или когда ничего не ушло).

        Каждая строка — полный снимок операции, поэтому следующая удачная
        строка лечит пропущенную.
        """
        with self._lock:
            try:
                self._persist(op)
            except HistoryUnavailable:
                return False
            return True

    def history(self, limit: int = 50) -> list[dict]:
        with self._lock:
            ops = [o for o in self._ops.values() if o.get("state") != PREVIEW]
            ops.sort(key=lambda o: o.get("created_at", ""), reverse=True)
            return [snapshot(o) for o in ops[:limit]]


_STORES: dict[str, OperationStore] = {}


_STORES_LOCK = threading.Lock()


def get_store() -> OperationStore:
    """Хранилище по текущему пути журнала (в тестах путь задаёт `ACL_CONFIG_DIR`)."""
    path = default_history_path()
    key = str(path)
    with _STORES_LOCK:          # два первых запроса разом не должны создать два хранилища
        if key not in _STORES:
            _STORES[key] = OperationStore(path)
        return _STORES[key]


def snapshot(op: dict) -> dict:
    """Копия операции для ответа/журнала: без ключа идемпотентности наружу."""
    out = json.loads(json.dumps(op))
    out.pop("idempotency_key", None)
    out.pop("_expires_ts", None)
    return out


# ───────────────────────── разбор запроса ─────────────────────────

def clean_actor(actor: Any) -> str:
    text = re.sub(r"[\x00-\x1f]+", " ", str(actor or "")).strip()[:80]
    return scrub(text) or "unlabelled"


def parse_targets(raw: Any) -> list[dict]:
    if not isinstance(raw, list) or not raw:
        raise CommandRefused("Pick at least one campaign, ad set or ad.")
    if len(raw) > MAX_TARGETS:
        raise CommandRefused(f"At most {MAX_TARGETS} objects per command.")
    seen: set[str] = set()
    out: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            raise CommandRefused("Each target needs a level and an id.")
        level, oid = str(item.get("level") or ""), str(item.get("id") or "")
        if level not in LEVELS:
            raise CommandRefused("Target level must be campaign, adset or ad.")
        if not _ID_RE.fullmatch(oid):
            raise CommandRefused("Target id must be a numeric Meta object id.")
        if oid in seen:
            continue
        seen.add(oid)
        out.append({"level": level, "id": oid})
    return out


def parse_money(value: Any, currency: str) -> int:
    """Сумма человека в основных единицах → мелкие единицы кабинета (`budget_minor`)."""
    text = str(value if value is not None else "").strip()
    zero = currency.upper() in ZERO_DECIMAL
    if not _DEC_RE.fullmatch(text) or (zero and "." in text):
        raise CommandRefused("Enter a positive amount" + (" in whole units." if zero else
                                                          " with at most two decimals."))
    try:
        dec = Decimal(text)
    except InvalidOperation:
        raise CommandRefused("Enter a positive amount.") from None
    minor = budget_minor(float(dec), currency)
    if not _wp.amount_ok(minor):
        raise CommandRefused("The amount must be greater than zero.")
    return minor


def fmt_minor(minor: int | None, currency: str) -> str:
    if minor is None:
        return "not set"
    if currency.upper() in ZERO_DECIMAL:
        return f"{minor:,} {currency}"
    return f"{minor / 100:,.2f} {currency}"


# ───────────────────────── чтение объектов ─────────────────────────

def _int(value: Any) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def read_object(client: Any, act: str, level: str, oid: str) -> dict:
    """Один вызов Graph → нормализованный снимок объекта. Чужой кабинет — отказ."""
    raw = client.get(f"/{oid}", {"fields": READ_FIELDS[level]})
    if not isinstance(raw, dict) or str(raw.get("id") or oid) != oid:
        raise CommandRefused("Meta answered for a different object; refused.", status=409)
    if str(raw.get("account_id") or "") != act.removeprefix("act_"):
        raise CommandRefused("The object belongs to a different ad account; refused.", status=409)
    parents = []
    for key, plevel in (("adset", "adset"), ("campaign", "campaign")):   # ближний родитель первым
        p = raw.get(key)
        if isinstance(p, dict):
            parents.append({"level": plevel, "id": str(p.get("id") or ""),
                            "name": str(p.get("name") or ""),
                            "status": str(p.get("status") or ""),
                            "effective_status": str(p.get("effective_status") or ""),
                            "daily_budget": _int(p.get("daily_budget")),
                            "bid_strategy": str(p.get("bid_strategy") or "")})
    return {"id": oid, "level": level, "name": str(raw.get("name") or oid)[:160],
            "status": str(raw.get("status") or ""),
            "effective_status": str(raw.get("effective_status") or ""),
            "daily_budget": _int(raw.get("daily_budget")) or None,
            "lifetime_budget": _int(raw.get("lifetime_budget")) or None,
            "bid_amount": _int(raw.get("bid_amount")),
            "bid_strategy": str(raw.get("bid_strategy") or ""),
            "parents": parents}


def current_value(command: str, snap: dict) -> Any:
    if command == "set_status":
        return snap["status"]
    if command == "set_budget":
        return snap["daily_budget"]
    return snap["bid_amount"]


def _constraints(snap: dict) -> list[dict]:
    """Родители, из-за которых включённый объект всё равно не пойдёт в показ."""
    out = []
    for p in snap["parents"]:
        if p["status"] != "ACTIVE":
            word = LEVEL_WORD[p["level"]]
            state = {"PAUSED": "paused", "ARCHIVED": "archived", "DELETED": "deleted"}.get(
                p["status"], p["status"].lower() or "not active")
            out.append({"level": p["level"], "id": p["id"], "name": p["name"],
                        "text": f"On · {word} {state}"})
    return out


def plan_one(command: str, snap: dict, new: Any, currency: str) -> tuple[dict | None, str]:
    """Один объект → запись разбора или причина пропуска. Ничего не пишет."""
    level = snap["level"]
    head = {"level": level, "id": snap["id"], "name": snap["name"]}
    if snap["status"] in ("ARCHIVED", "DELETED"):
        return None, f"{LEVEL_WORD[level]} is {snap['status'].lower()}"
    if command == "set_status":
        before = snap["status"]
        if before == new:
            return None, f"Already {STATUS_WORD[new]}"
        if before not in STATUS_WORD:
            return None, f"Status {before or 'unknown'} cannot be changed here"
        entry = {**head, "field": "status", "before": before, "after": new,
                 "before_display": STATUS_WORD[before], "after_display": STATUS_WORD[new],
                 "constraints": _constraints(snap) if new == "ACTIVE" and level != "campaign" else []}
        return entry, ""
    if command == "set_budget":
        if level == "ad":
            return None, "Ads have no budget"
        if snap["lifetime_budget"]:
            return None, "Lifetime budget is not supported here"
        before = snap["daily_budget"]
        if not before:
            camp = next((p for p in snap["parents"] if p["level"] == "campaign"), None)
            if level == "adset" and camp and camp["daily_budget"]:
                return None, "Budget is set on the campaign"
            return None, "No daily budget on this object"
        if before == new:
            return None, "Already at this budget"
        entry = {**head, "field": "daily_budget", "before": before, "after": new,
                 "before_display": fmt_minor(before, currency), "after_display": fmt_minor(new, currency),
                 "constraints": []}
        return entry, ""
    # set_bid
    if level != "adset":
        return None, "A manual bid exists on ad sets only"
    camp = next((p for p in snap["parents"] if p["level"] == "campaign"), None)
    strategy = snap["bid_strategy"] or (camp["bid_strategy"] if camp else "")
    if strategy not in BID_STRATEGIES:
        if strategy in ("", AUTOMATIC_BID):
            return None, AUTOMATIC_BID_TEXT
        return None, f"Bid strategy {strategy} has no manual bid"
    before = snap["bid_amount"]
    if before == new:
        return None, "Already at this bid"
    entry = {**head, "field": "bid_amount", "before": before, "after": new,
             "before_display": fmt_minor(before, currency), "after_display": fmt_minor(new, currency),
             "constraints": [], "strategy": BID_STRATEGIES[strategy]}
    return entry, ""


def _check_account(client: Any, act: str, expect_currency: str | None = None) -> tuple[str, int]:
    row = client.account(act)
    if str(row.get("account_id") or "") != act.removeprefix("act_"):
        raise CommandRefused("Graph API answered for a different ad account; refused.", status=409)
    currency = str(row.get("currency") or "").upper()
    if not currency:
        raise CommandRefused("The ad account currency is unknown; refused.", status=409)
    if expect_currency is not None and currency != expect_currency:
        raise CommandRefused("The account currency changed since the preview.", status=409,
                             code="stale")
    return currency, _int(row.get("account_status")) or 0


def _revision_id(op: dict) -> str:
    core = {"op_id": op["op_id"], "account": op["account"], "command": op["command"],
            "value": op["value"], "currency": op["currency"],
            "affected": [{k: a[k] for k in ("level", "id", "field", "before", "after")}
                         for a in op["affected"]],
            "skipped": op["skipped"]}
    digest = hashlib.sha256(json.dumps(core, sort_keys=True).encode("utf-8")).hexdigest()
    return "rev_" + digest[:20]


# ───────────────────────── превью ─────────────────────────

OpenSession = Callable[..., AbstractContextManager]


MAX_CALLS_ENV = "ACL_FB_API_MAX_CALLS"


def call_ceiling() -> int:
    """Потолок вызовов сессии — тот же `ACL_FB_API_MAX_CALLS` (по умолчанию 24, 4…60), что у чтений."""
    try:
        value = float(os.environ.get(MAX_CALLS_ENV, "") or 24)
    except ValueError:
        value = 24
    return int(max(4, min(60, value)))


def call_budget(n_targets: int) -> int:
    """Вызовы на команду: кабинет + чтение, запись и сверка на каждый объект + запас.

    Не выше потолка оператора: команда, которой нужно больше, отказывается в превью.
    """
    return min(1 + 3 * n_targets + 6, call_ceiling())


def max_targets_for_ceiling() -> int:
    return max(1, (call_ceiling() - 1) // 3)


def preview(store: OperationStore, open_session: OpenSession, *, account: str, actor: str,
            command: str, value: Any, targets: Any, baseline: dict | None = None) -> dict:
    if command not in COMMANDS:
        raise CommandRefused("Unknown command. Use set_status, set_budget or set_bid.")
    account = str(account or "").strip()
    if not account:
        raise CommandRefused("Pick an account.")
    items = parse_targets(targets)
    if len(items) > max_targets_for_ceiling():
        raise CommandRefused(
            f"At most {max_targets_for_ceiling()} objects per command with the current call limit "
            f"({MAX_CALLS_ENV}={call_ceiling()}). Pick fewer objects or split the command.")
    if command == "set_status":
        value = str(value or "").strip()
        if value not in _wp.OBJECT_STATUSES:
            raise CommandRefused("Status must be ACTIVE or PAUSED.")
    else:
        value = str(value if value is not None else "").strip()
        if not _DEC_RE.fullmatch(value):
            raise CommandRefused("Enter a positive amount with at most two decimals.")
    with open_session(account, False) as sess:
        client = sess.client
        client.reset_budget(call_budget(len(items)))
        currency, status = _check_account(client, sess.act)
        if command == "set_status" and value == "ACTIVE" and status != 1:
            raise CommandRefused("The ad account is not active, so nothing can be switched on.",
                                 status=409)
        new: Any = value if command == "set_status" else parse_money(value, currency)
        affected: list[dict] = []
        skipped: list[dict] = []
        for t in items:
            try:
                snap = read_object(client, sess.act, t["level"], t["id"])
            except GraphError as exc:
                skipped.append({"level": t["level"], "id": t["id"], "name": t["id"],
                                "reason": f"Could not read this {LEVEL_WORD[t['level']].lower()} "
                                          f"(Graph error {exc.code})"})
                continue
            saved = (baseline or {}).get(t["level"] + ":" + t["id"], {})
            for field in ("status", "daily_budget", "bid_amount", "bid_strategy"):
                known = saved.get(field)
                if known not in (None, "") and snap.get(field) != known:
                    raise CommandRefused(
                        f"{LEVEL_WORD[t['level']]} changed since the saved list. "
                        "Nothing was written; the account list is refreshing. Preview it again.",
                        status=409, code="stale")
            entry, why = plan_one(command, snap, new, currency)
            if entry is None:
                skipped.append({"level": t["level"], "id": t["id"], "name": snap["name"], "reason": why})
            else:
                affected.append({**entry, "result": "", "saved": None, "note": ""})
    now = store.clock()
    op = {"op_id": "op_" + secrets.token_hex(8), "state": PREVIEW, "account": account,
          "act": sess.act, "currency": currency, "unit": 1 if currency in ZERO_DECIMAL else 100,
          "command": command, "value": value,
          "value_minor": new if command != "set_status" else None,
          "actor": clean_actor(actor), "created_at": store.now_iso(now),
          "expires_at": store.now_iso(now + PREVIEW_TTL_S), "_expires_ts": now + PREVIEW_TTL_S,
          "affected": affected, "skipped": skipped, "message": "", "stale": False,
          "idempotency_key": ""}
    op["revision_id"] = _revision_id(op)
    op["summary"] = _summary(op)
    store.add(op)
    with store._lock:
        return snapshot(op)


def _summary(op: dict) -> str:
    n, k = len(op["affected"]), len(op["skipped"])
    word = {"set_status": "status", "set_budget": "daily budget", "set_bid": "bid"}[op["command"]]
    return f"{n} to change ({word}), {k} skipped"


# ───────────────────────── исполнение ─────────────────────────

def _spawn_thread(fn: Callable[[], None]) -> None:
    threading.Thread(target=fn, name="acl-campaign-command", daemon=True).start()


def execute(store: OperationStore, open_session: OpenSession, *, revision_id: str,
            idempotency_key: str, spawn: Callable[[Callable[[], None]], None] | None = None,
            sleep: Callable[[float], None] = time.sleep,
            after_write: Callable[[dict, Any], None] | None = None,
            on_stale: Callable[[dict], None] | None = None) -> dict:
    """Принять подтверждение человека и запустить операцию. Возвращает её состояние.

    Тот же ключ — тот же ответ, записей больше нет. Превью исполняется один раз.
    """
    key = str(idempotency_key or "").strip()
    if not _KEY_RE.fullmatch(key):
        raise CommandRefused("An idempotency key of 8-80 letters, digits or - _ . : is required.")
    with store._lock:
        seen = store.by_key(key)
        op = store.by_revision(str(revision_id or ""))
        if seen is not None:
            if op is None or seen["op_id"] != op["op_id"]:
                raise CommandRefused("This idempotency key belongs to a different command.",
                                     status=409)
            return snapshot(seen)       # повтор: ни одной записи
        if op is None:
            raise CommandRefused("Unknown preview. Preview the command again.", status=404)
        if op["state"] != PREVIEW:
            raise CommandRefused("This preview was already used. Preview the command again.",
                                 status=409)
        if store.clock() > op.get("_expires_ts", 0):
            op["state"], op["message"] = FAILED, "The preview expired. Nothing was written."
            raise CommandRefused("The preview expired. Preview the command again.", status=409,
                                 code="expired")
        if not op["affected"]:
            raise CommandRefused("There is nothing to change in this preview.", status=409)
        op["idempotency_key"] = key
        op["state"] = RUNNING
        op["message"] = "Re-reading current values"
        if after_write is not None or on_stale is not None:
            op["refresh_pending"] = True
        for item in op["affected"]:
            item["result"] = "pending"
        try:
            store.save(op)
        except HistoryUnavailable:
            # Ни одной записи в Meta: без следа на диске операцию не начинаем.
            for item in op["affected"]:
                item["result"] = "not_sent"
            op["state"] = FAILED
            op["refresh_pending"] = False
            op["message"] = HISTORY_FAILED + " Nothing was sent. Free up disk space and try again."
            store.try_save(op)
            raise CommandRefused(op["message"], status=503, code="storage") from None
        op_id = op["op_id"]

    def work() -> None:
        try:
            callbacks = {}
            if after_write is not None:
                callbacks["after_write"] = after_write
            if on_stale is not None:
                callbacks["on_stale"] = on_stale
            run_op(store, op_id, open_session, sleep=sleep, **callbacks)
        except BaseException as exc:  # noqa: BLE001 — операция не должна остаться в Running
            _finish_after_crash(store, op_id, exc)

    with store._lock:
        started = snapshot(op)  # ответ — состояние на момент принятия (Running), не итог
    (spawn or _spawn_thread)(work)
    return started


def _finish_after_crash(store: OperationStore, op_id: str, exc: BaseException) -> None:
    op = store.get(op_id)
    if op is None or op["state"] in FINAL_STATES:
        return
    sent = any(a["result"] in ("sending", "sent", "unknown", "saved") for a in op["affected"])
    for a in op["affected"]:
        if a["result"] == "pending":
            a["result"] = "not_attempted"
    op["state"] = UNKNOWN if sent else FAILED
    op["message"] = (f"Stopped: {scrub(str(exc))[:200] or type(exc).__name__}. "
                     + ("Some writes may have been applied; check Ads Manager. Nothing was retried."
                        if sent else "Nothing was written."))
    store.try_save(op)


def _fail_all(op: dict, message: str, *, stale: bool = False) -> None:
    for a in op["affected"]:
        a["result"] = "not_attempted"
    op["state"], op["message"], op["stale"] = FAILED, message, stale


def run_op(store: OperationStore, op_id: str, open_session: OpenSession, *,
           sleep: Callable[[float], None] = time.sleep,
           after_write: Callable[[dict, Any], None] | None = None,
           on_stale: Callable[[dict], None] | None = None) -> None:
    op = store.get(op_id)
    assert op is not None
    try:
        with open_session(op["account"], True) as sess:
            _run(store, op, sess, sleep)
            if op["state"] == SUCCEEDED and after_write is not None:
                try:
                    after_write(op, sess)
                except Exception:  # noqa: BLE001 — запись уже подтверждена и сохранена
                    log.warning("The campaign list could not refresh after a confirmed command; its result is unchanged.")
    except CommandRefused as exc:
        # Отказ до первой записи (чужой кабинет, смена валюты, устаревшее превью).
        _fail_all(op, f"{exc} Nothing was written.", stale=exc.code == "stale")
        store.try_save(op)
    except GraphStop as exc:
        if op["state"] in FINAL_STATES:
            return
        _stopped(op, f"Stopped before writing: {exc}")
        store.try_save(op)
    except Exception as exc:  # noqa: BLE001 — профиль не открылся и т. п.
        if op["state"] in FINAL_STATES:
            return
        _stopped(op, scrub(str(exc)) or type(exc).__name__)
        store.try_save(op)
    finally:
        if op.get("stale") and on_stale is not None:
            try:
                on_stale(op)
            except Exception:  # noqa: BLE001 — отказ записи остаётся отказом
                log.warning("The stale campaign list could not be queued for refresh. Nothing was retried.")
        if op.get("refresh_pending"):
            op["refresh_pending"] = False
            store.try_save(op)


def _stopped(op: dict, message: str) -> None:
    sent = any(a["result"] in ("sending", "sent", "unknown", "saved") for a in op["affected"])
    for a in op["affected"]:
        if a["result"] == "pending":
            a["result"] = "not_attempted"
    op["state"] = UNKNOWN if sent else FAILED
    op["message"] = message + (" Some writes may have been applied; check Ads Manager." if sent
                               else " Nothing was written.")


def _run(store: OperationStore, op: dict, sess: Any, sleep: Callable[[float], None]) -> None:
    client, act = sess.client, sess.act
    if act != op["act"]:
        raise CommandRefused("The session is for a different ad account than the preview; refused.",
                             status=409, code="stale")
    if not getattr(client, "allow_writes", False):
        raise CommandRefused("This session cannot write.")
    client.reset_budget(call_budget(len(op["affected"])))
    command, currency = op["command"], op["currency"]
    _currency, acc_status = _check_account(client, act, expect_currency=currency)
    if command == "set_status" and op["value"] == "ACTIVE" and acc_status != 1:
        raise CommandRefused("The ad account is no longer active, so nothing can be switched on.",
                             status=409, code="stale")

    # 1) Перечитать ВСЁ до первой записи: изменилось хоть что-то — не пишем ничего.
    for item in op["affected"]:
        snap = read_object(client, act, item["level"], item["id"])
        new = item["after"]
        entry, why = plan_one(command, snap, new, currency)
        if (entry is None or entry["before"] != item["before"] or entry["after"] != item["after"]
                or entry["constraints"] != item["constraints"]
                or entry.get("strategy") != item.get("strategy")):
            now_value = current_value(command, snap)
            item["note"] = (f"was {item['before_display']}, now "
                            f"{why or _display(command, now_value, currency)}")
            _fail_all(op, f"{LEVEL_WORD[item['level']]} \"{item['name']}\" changed since the "
                          "preview. Nothing was written; review the change again.", stale=True)
            store.try_save(op)
            return

    # 2) Одна запись на объект. Первая неудача — остальные не пробуем.
    stop_rest = False
    history_blocked = False   # локально: флаг не попадает ни в журнал, ни в снимки
    for item in op["affected"]:
        if stop_rest:
            item["result"] = "not_attempted"
            continue
        path = f"/{item['id']}"
        item["requested"] = item["after"]
        item["result"] = "sending"      # до POST: упавший процесс не должен выглядеть как «не отправлено»
        try:
            store.save(op)              # без надёжного «sending» на диске POST не уходит (ACL-1855)
        except HistoryUnavailable:
            item["result"] = "not_sent"
            item["note"] = "Not sent: " + HISTORY_FAILED[0].lower() + HISTORY_FAILED[1:]
            history_blocked = True
            stop_rest = True
            continue
        try:
            client.post(path, {item["field"]: item["after"] if item["field"] == "status"
                               else str(item["after"])})
            item["result"] = "sent"
        except WriteUnknown:
            item["result"] = "unknown"
            item["note"] = "No answer from Meta; checking by readback"
            stop_rest = True
        except GraphError as exc:
            item["result"], item["note"] = "refused", f"Meta refused: {exc}"[:240]
            stop_rest = True
        except (GraphStop, WritesBlocked, ValueError) as exc:
            item["result"], item["note"] = "not_sent", f"Not sent: {exc}"[:240]
            stop_rest = True
        store.try_save(op)   # не вышло — на диске остаётся «sending», при рестарте это «неизвестно»

    # 3) Сверка чтением того, что ушло (или могло уйти).
    op["state"], op["message"] = VERIFYING, "Reading the values back from Meta"
    store.try_save(op)
    for item in op["affected"]:
        if item["result"] not in ("sent", "unknown"):
            continue
        was_unknown = item["result"] == "unknown"
        if was_unknown:
            sleep(RECONCILE_PAUSE_S)
        try:
            snap = read_object(client, act, item["level"], item["id"])
        except (GraphStop, GraphError, CommandRefused) as exc:
            item["result"] = "unknown"
            item["note"] = f"Could not read it back: {exc}"[:240]
            continue
        saved = current_value(command, snap)
        item["saved"] = saved
        if saved == item["after"]:
            item["result"] = "saved"
            if was_unknown:
                item["note"] = "Confirmed by readback after a timeout"
                client.reconciled(f"/{item['id']}")
        elif was_unknown and saved == item["before"]:
            item["result"] = "unknown"
            item["note"] = "Readback still shows the old value; the write may yet apply. Not retried."
        elif was_unknown:
            item["result"] = "unknown"
            item["note"] = f"Readback shows {_display(command, saved, currency)}, not the requested value."
        else:
            item["result"] = "mismatch"
            item["note"] = (f"Meta accepted the change but saved {_display(command, saved, currency)}")
    _finalize(store, op, history_blocked=history_blocked)


def _display(command: str, value: Any, currency: str) -> str:
    if command == "set_status":
        return STATUS_WORD.get(str(value), str(value or "unknown"))
    return fmt_minor(value, currency)


def _conclude(op: dict) -> None:
    """Итог по результатам объектов (и при рестарте после сверки)."""
    results = [a["result"] for a in op["affected"]]
    done = sum(1 for r in results if r == "saved")
    total = len(results)
    if any(r in ("unknown", "sent") for r in results):
        op["state"] = UNKNOWN
        op["message"] = (f"{done} of {total} confirmed. The rest could not be confirmed; "
                         "check Ads Manager. Nothing was retried.")
    elif done == total:
        op["state"], op["message"] = SUCCEEDED, f"All {total} confirmed by readback."
    elif done == 0 and all(r in ("not_sent", "not_attempted") for r in results):
        op["state"], op["message"] = FAILED, "Nothing was written."
    else:
        op["state"] = FAILED
        op["message"] = f"{done} of {total} saved; the rest were not changed or did not match."


def _finalize(store: OperationStore, op: dict, *, history_blocked: bool = False) -> None:
    _conclude(op)
    if history_blocked:
        op["message"] = HISTORY_FAILED + " " + op["message"]
    if store.try_save(op):
        return
    # Итог не лёг на диск: там последнее надёжное — намерение («sending»/«sent»).
    # Показываем то же, что покажет журнал после рестарта, — «не проверено».
    sent = any(a["result"] in ("saved", "mismatch", "unknown", "sent") for a in op["affected"])
    if sent:
        op["state"] = UNKNOWN
    if HISTORY_FAILED not in op["message"]:
        op["message"] = HISTORY_FAILED + " " + op["message"]
    if sent:
        op["message"] += " Check Ads Manager; nothing was retried."


def status(store: OperationStore, op_id: str) -> dict | None:
    with store._lock:
        op = store.get(str(op_id or ""))
        return snapshot(op) if op else None
