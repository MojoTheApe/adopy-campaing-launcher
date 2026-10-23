"""Rules T3 (ACL-1816): сбор входа для `evaluate` из локальных источников.

Только чтение: книга BI из кэша отчёта, последний сохранённый снимок описи,
цели My Goals, журналы правил. В Meta не ходит и профили не открывает: источники
приходят снаружи (`Sources`), поэтому тесты подставляют заглушки, а рабочая
проводка лежит в `src/web/rules_runtime.py`.

Здесь же проводятся `assigned_names` / `current_names` для вердикта `renamed`
(T2 сам их не берёт): имя при назначении - из `scope.objects[*].name_at_assign`
(или `name`); имя из повторного чтения - из `Sources.current_names`. Повторного
чтения Observe/Suggest не делают, поэтому по умолчанию оно пусто; его подставит
исполнитель (T5) перед записью.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Callable

from src.analysis_agent.campaign_inventory import Snapshot, build_tree

from . import evidence as ev
from . import model

#: Исполненные состояния действия: только они включают cooldown (предложение - нет).
EXECUTED_STATES = frozenset({"queued", "submitted", "running", "verifying", "verified", "undone", "failed", "unknown"})

_DATE_IN_NAME = re.compile(r"(?<!\d)(\d{1,2})\.(\d{1,2})(?!\d)")


class GatherError(Exception):
    """Вход собрать нельзя (нет книги и т. п.); причина идёт в журнал оценки."""


def _none_names(account: str) -> dict[str, str]:  # noqa: ARG001
    return {}


@dataclass
class Sources:
    """Откуда брать данные. Все вызовы локальные."""

    #: () -> (DataFrame | None, bi_built_at, ключ_книги)
    workbook: Callable[[], tuple[Any, Any, Any]]
    #: аккаунт -> Snapshot | None
    snapshot: Callable[[str], Snapshot | None]
    #: (аккаунт, страна) -> {страна: имя аккаунта в книге}
    aliases: Callable[[str, str], dict[str, str]]
    #: (менеджер, страны) -> список целей (`goals.for_cm`)
    goals: Callable[[str, Any], list[dict]]
    #: аккаунт -> {ключ: имя из повторного чтения}; в Observe/Suggest повторного чтения нет
    current_names: Callable[[str], dict[str, str]] = field(default=_none_names)


# ───────────────────────── мелочи ─────────────────────────

def age_days_from_name(name: str, today: date) -> int | None:
    """Возраст по дате `dd.mm` в имени: сколько дней назад создано (включая сегодня = 1).

    Года в имени нет: берём ближайшую такую дату не позже сегодня. Нет даты - `None`
    (возраст неизвестен, а не ноль).
    """
    m = _DATE_IN_NAME.search(name or "")
    if not m:
        return None
    day, month = int(m.group(1)), int(m.group(2))
    for year in (today.year, today.year - 1):
        try:
            made = date(year, month, day)
        except ValueError:
            continue
        if made <= today:
            return (today - made).days + 1
    return None


def object_key(level: str, oid: str) -> str:
    return f"{level}:{oid}"


def executed_actions(actions: list[dict]) -> dict[str, str]:
    """`{id объекта: время последнего ИСПОЛНЕННОГО действия}` из журнала действий."""
    last: dict[str, str] = {}
    for rec in actions:
        if rec.get("id") and rec.get("state") in EXECUTED_STATES and rec.get("at"):
            last[str(rec["id"])] = rec["at"]
    return last


def _budget_amount(snap: Snapshot, obj: dict) -> float | None:
    from src.analysis_agent.campaign_inventory import _budget_amount as amount

    minor = obj.get("daily_budget")
    return amount(minor, snap.currency) if minor else None


def _object_info(snap: Snapshot, level: str, oid: str, assigned: str, today: date) -> dict:
    """Описание объекта из снимка: статусы, бюджет, предки и потомки (из дерева по id)."""
    tree = build_tree(snap)
    pool = {"campaign": tree["campaigns"], "adset": tree["adsets"], "ad": {a["id"]: a for a in snap.ads}}[level]
    obj = pool.get(oid)
    base = {"account": snap.account, "level": level, "id": oid, "name": assigned,
            "ancestors": [], "descendants": []}
    if obj is None:
        return {**base, "missing": True}
    camp = tree["campaigns"].get(obj.get("campaign_id")) if level != "campaign" else None
    adset = tree["adsets"].get(obj.get("adset_id")) if level == "ad" else None
    parent = adset or camp
    anc = []
    if adset is not None:
        anc.append(object_key("adset", adset["id"]))
    if camp is not None:
        anc.append(object_key("campaign", camp["id"]))
    desc: list[str] = []
    if level == "campaign":
        for a in tree["adsets_of"].get(oid, []):
            desc.append(object_key("adset", a["id"]))
            desc += [object_key("ad", x["id"]) for x in tree["ads_of"].get(a["id"], [])]
    elif level == "adset":
        desc = [object_key("ad", x["id"]) for x in tree["ads_of"].get(oid, [])]
    owns = bool(obj.get("daily_budget") or obj.get("lifetime_budget"))
    return {
        **base, "name": obj.get("name") or assigned, "status": obj.get("status"),
        "effective_status": obj.get("effective_status"),
        "parent_status": (parent or {}).get("status"),
        "daily_budget_amount": _budget_amount(snap, obj) if level != "ad" else None,
        "lifetime_budget": bool(obj.get("lifetime_budget")),
        "budget_owner": owns and level != "ad", "ancestors": anc, "descendants": desc,
        "age_days": age_days_from_name(obj.get("name") or "", today),
    }


# ───────────────────────── сбор ─────────────────────────

def _scope_objects(version: dict) -> list[dict]:
    return [o for o in (version.get("scope") or {}).get("objects") or [] if isinstance(o, dict)]


def gather(rule: dict, version: dict, sources: Sources, *, now: Any, actions: list[dict] | None = None,
           holds: dict[str, dict] | None = None, today: date | None = None) -> dict:
    """Вход `evaluate` для одной версии правила. Бросает `GatherError`, если книги нет."""
    wb = sources.workbook()
    df, built_at = (wb[0], wb[1]) if wb else (None, None)
    if df is None:
        raise GatherError("the stats workbook is not available")
    through = ev.workbook_through(df)
    if through is None:
        raise GatherError("the stats workbook has no dates")
    current = ev._utc(now)  # noqa: SLF001
    today = today or (current.astimezone().date() if current else date.today())

    strategy = version.get("strategy")
    evi = version.get("evidence") or {}
    scope = version.get("scope") or {}
    country = str(scope.get("country") or "")
    d = ev.maturity_days(None, evi.get("maturity_days"))
    window_days = max(1, int(evi.get("window_days") or 21))
    kind = ev.STOP_LOSS if strategy == 1 else ev.SCALE

    countries = [country] if country else []
    target = model.resolve_target(version, sources.goals(rule.get("owner_cm", ""), countries or None),
                                  country, str(scope.get("asset") or ""))

    # окна: S3 - блоки подряд до зрелой границы, S4 - два зрелых окна, остальное - одно окно
    act = version.get("action") or {}
    mature_end = through - timedelta(days=d)
    spans: dict[str, tuple[date, date]] = {}
    start = through - timedelta(days=window_days - 1)
    if strategy == 3:
        n, size = int(act.get("blocks") or 2), int(act.get("block_days") or 3)
        for i in range(n):  # 0 - самый старый
            end = mature_end - timedelta(days=size * (n - 1 - i))
            spans[f"block{i}"] = (end - timedelta(days=size - 1), end)
        start = spans["block0"][0]
    elif strategy == 4:
        cur = (mature_end - timedelta(days=window_days - 1), mature_end)
        base = (cur[0] - timedelta(days=window_days), cur[0] - timedelta(days=1))
        spans = {"current": cur, "base": base}
        start = cur[0]

    by_account: dict[str, list[dict]] = {}
    for o in _scope_objects(version):
        by_account.setdefault(o["account"], []).append(o)

    accounts: dict[str, dict] = {}
    objects: dict[str, dict] = {}
    blocks: dict[str, list[dict]] = {}
    compare: dict[str, dict] = {}
    snapshot_at: dict[str, str] = {}

    for acct in scope.get("accounts") or list(by_account):
        scoped = by_account.get(acct, [])
        keys = [object_key(o["level"], o["id"]) for o in scoped]
        assigned = {object_key(o["level"], o["id"]): str(o.get("name_at_assign") or o.get("name") or "")
                    for o in scoped}
        assigned_names = {k: v for k, v in assigned.items() if v}
        snap = sources.snapshot(acct)
        if snap is None:
            accounts[acct] = {"error": "the account inventory was never read", "identity": {}, "numbers": {}}
            for o in scoped:
                objects[object_key(o["level"], o["id"])] = {
                    "account": acct, "level": o["level"], "id": o["id"], "name": assigned.get(
                        object_key(o["level"], o["id"]), ""), "missing": True}
            continue
        snapshot_at[acct] = snap.finished_at_utc or snap.finished_at
        aliases = sources.aliases(acct, country) if country else {}
        extra = dict(account_label=acct, through=through, days=d, target_cpa=target["value"], now=now,
                     today=today, assigned_names=assigned_names or None,
                     current_names=sources.current_names(acct) or None, scope_keys=keys)

        def build(strategy_kind: str, begin: date, last: date) -> dict:
            return ev.build_evidence(snap, df, aliases, strategy=strategy_kind, start=begin,
                                     bi_built_at=built_at, **{**extra, "through": last})

        main = build(kind, start, through)
        accounts[acct] = main
        if strategy == 3:
            parts = [build(ev.SCALE, spans[f"block{i}"][0], spans[f"block{i}"][1] + timedelta(days=d))
                     for i in range(len(spans))]
            for k in keys:
                blocks[k] = [{"spend": (p["numbers"].get(k) or {}).get("spend"),
                              "ftds": (p["numbers"].get(k) or {}).get("ftds")} for p in parts]
        elif strategy == 4:
            cur = build(ev.SCALE, spans["current"][0], spans["current"][1] + timedelta(days=d))
            base = build(ev.SCALE, spans["base"][0], spans["base"][1] + timedelta(days=d))
            accounts[acct] = cur
            for k in keys:
                compare[k] = {"current": _pick(cur["numbers"].get(k)), "base": _pick(base["numbers"].get(k))}
        for o in scoped:
            k = object_key(o["level"], o["id"])
            objects[k] = _object_info(snap, o["level"], str(o["id"]), assigned.get(k, ""), today)

    holds = holds or {}
    last = executed_actions(actions or [])
    return {
        "target": target, "accounts": accounts, "objects": objects, "blocks": blocks, "compare": compare,
        "holds": {k: holds[i["id"]] for k, i in objects.items() if i.get("id") in holds},
        "last_action_at": {k: last[i["id"]] for k, i in objects.items() if i.get("id") in last},
        "bi": {"through": through.isoformat(), "built_at": str(built_at or ""), "key": str(wb[2]) if len(wb) > 2 else ""},
        "snapshot_at": snapshot_at,
    }


def _pick(num: dict | None) -> dict:
    num = num or {}
    return {"spend": num.get("spend"), "leads": num.get("leads"), "ftds": num.get("ftds")}
