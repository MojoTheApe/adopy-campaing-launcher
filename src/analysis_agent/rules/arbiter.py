"""Rules T3 (ACL-1816): арбитр приоритетов и конфликтов (дизайн §1.7).

Чистая функция: берёт результаты `evaluate` по нескольким правилам и решает, чьё
предложение остаётся. Порядок детерминирован и не зависит от порядка входа.

Правила:

* приоритет: stop-loss (S1) -> quality (S2) -> deterioration (S4) -> pacing (S5) -> scale (S3);
  внутри класса - меньший `priority` правила, затем `rule_id`, версия, ключ объекта;
* пауза, `block_scale` и тревога S1 (spend >= 2T без исходов) гасят scale на том же объекте, на его детях и на предках
  (родство берётся из `lineage` объекта, оно симметрично: пауза ad set гасит scale
  кампании-владельца бюджета, пауза кампании гасит scale её ad set);
* проигравший объект гасит scale, даже если пауза ему сейчас запрещена (Manual hold, Cooldown,
  Parent paused, свежесть в Automatic): такой «убийца» никогда не становится предложением и не
  занимает мутацию объекта;
* на объект - не больше одной записывающей мутации (`pause`, `budget_up`);
* одинаковое предложение (объект + вид действия) от двух правил - дубль, остаётся первое.
"""
from __future__ import annotations

from typing import Any

#: Класс приоритета по стратегии: меньше - важнее.
STRATEGY_RANK = {1: 0, 2: 1, 4: 2, 5: 3, 3: 4}
#: Виды, что гасят scale.
KILLS_SCALE = frozenset({"pause", "block_scale"})
WRITING = frozenset({"pause", "budget_up", "enable"})

PROPOSED = "proposed"
SUPPRESSED = "suppressed"
DUPLICATE = "duplicate"

SUPPRESSED_BY_PAUSE = "Suppressed: a pause or scale block exists on this object, its parent or its children"
SUPPRESSED_ONE_MUTATION = "Suppressed: one object takes at most one mutation"


def _kills_scale(strategy: Any, kind: str | None) -> bool:
    return kind in KILLS_SCALE or (strategy == 1 and kind == "alert")


def _blocked_killers(items: list[dict]) -> list[dict]:
    """Объекты, у которых условия совпали, а действие заблокировано: они только гасят scale."""
    out: list[dict] = []
    for item in items:
        result = item.get("result") or {}
        strategy = item.get("strategy", result.get("strategy"))
        for obj in result.get("objects") or []:
            blocked = obj.get("blocked_action")
            if (obj.get("verdict") == "blocked" and obj.get("conditions_matched") and blocked
                    and _kills_scale(strategy, blocked.get("kind"))):
                out.append({"rule_id": item.get("rule_id") or "", "key": obj["key"], "kind": blocked["kind"],
                            "strategy": strategy, "lineage": set(obj.get("lineage") or [obj["key"]]),
                            "reasons": list(obj.get("reasons") or [])})
    return sorted(out, key=lambda k: (k["rule_id"], k["key"], k["kind"]))


def _flatten(items: list[dict]) -> list[dict]:
    out: list[dict] = []
    for item in items:
        result = item.get("result") or {}
        for obj in result.get("objects") or []:
            action = obj.get("action")
            if obj.get("verdict") != "matched" or not action:
                continue
            out.append({
                "rule_id": item.get("rule_id") or "", "version": item.get("version"),
                "strategy": item.get("strategy", result.get("strategy")),
                "priority": item.get("priority", 50), "mode": item.get("mode", "suggest"),
                "key": obj["key"], "account": obj.get("account"), "kind": action["kind"],
                "action": action, "lineage": set(obj.get("lineage") or [obj["key"]]),
                "fingerprint": obj.get("fingerprint", ""),
            })
    return out


def _sort_key(p: dict) -> tuple:
    return (STRATEGY_RANK.get(p["strategy"], 9), p["priority"], p["rule_id"], p["version"] or 0,
            p["key"], p["kind"])


def _related(a: dict, b: dict) -> bool:
    return b["key"] in a["lineage"] or a["key"] in b["lineage"]


def arbitrate(items: list[dict]) -> dict:
    """`items`: `[{rule_id, version, strategy, priority, mode, result: evaluate(...)}]`.

    Возвращает `{"proposals": [...все, с status/order/reason...], "accepted": [...],
    "suppressed": [...], "duplicates": [...]}`. Входные результаты не меняются.
    """
    ordered = sorted(_flatten(items), key=_sort_key)
    blocked_killers = _blocked_killers(items)
    accepted: list[dict] = []
    decided: list[dict] = []
    seen: dict[tuple, dict] = {}
    for n, p in enumerate(ordered, start=1):
        rec = {k: p[k] for k in ("rule_id", "version", "strategy", "priority", "mode", "key", "account",
                                 "kind", "action", "fingerprint")}
        rec["order"] = n
        rec["status"] = PROPOSED
        twin = seen.get((p["key"], p["kind"]))
        if twin is not None:
            rec.update(status=DUPLICATE, duplicate_of=twin["order"],
                       reason=f"Duplicate of #{twin['order']} ({twin['rule_id']})")
        elif p["kind"] == "budget_up":
            killer = next((a for a in accepted if _kills_scale(a["strategy"], a["kind"])
                           and _related(a["_p"], p)), None)
            if killer is not None:
                rec.update(status=SUPPRESSED, suppressed_by=killer["order"], reason=SUPPRESSED_BY_PAUSE)
            else:
                dead = next((k for k in blocked_killers if _related(k, p)), None)
                if dead is not None:
                    rec.update(status=SUPPRESSED, suppressed_by=f"blocked:{dead['rule_id']}:{dead['key']}",
                               reason=f"{SUPPRESSED_BY_PAUSE} (the {dead['kind']} is blocked: "
                                      f"{', '.join(dead['reasons']) or 'not allowed now'})")
        if rec["status"] == PROPOSED and p["kind"] in WRITING:
            taken = next((a for a in accepted if a["key"] == p["key"] and a["kind"] in WRITING), None)
            if taken is not None:
                rec.update(status=SUPPRESSED, suppressed_by=taken["order"], reason=SUPPRESSED_ONE_MUTATION)
        if rec["status"] == PROPOSED:
            rec["_p"] = p
            accepted.append(rec)
        seen.setdefault((p["key"], p["kind"]), rec)
        decided.append(rec)
    for rec in decided:
        rec.pop("_p", None)
    return {
        "proposals": decided,
        "accepted": [r for r in decided if r["status"] == PROPOSED],
        "suppressed": [r for r in decided if r["status"] == SUPPRESSED],
        "duplicates": [r for r in decided if r["status"] == DUPLICATE],
        "blocked_killers": [{k: v for k, v in b.items() if k != "lineage"} for b in blocked_killers],
    }


def decision_for(arbitration: dict, rule_id: str, key: str) -> dict | None:
    """Решение арбитра по объекту правила (для записи в журнал оценки)."""
    for rec in arbitration.get("proposals") or []:
        if rec["rule_id"] == rule_id and rec["key"] == key:
            return {k: rec.get(k) for k in ("status", "order", "reason", "suppressed_by", "duplicate_of")
                    if rec.get(k) is not None}
    return None


__all__: list[Any] = ["arbitrate", "decision_for", "PROPOSED", "SUPPRESSED", "DUPLICATE"]
