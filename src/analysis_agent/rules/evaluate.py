"""Rules T3 (ACL-1816): чистая оценка правила — `evaluate(rule_version, evidence, now)`.

Дизайн: `C:\\acl-notes\\rules-design.md`, §1.4 (слой 3), §1.6 (почему не сработало),
§2.2-2.3 (S1-S4), §3.2 (вердикты идентичности).

Функция чистая: ничего не читает с диска, не ходит в Meta и не открывает профили.
Всё нужное приходит в `evidence`, собранном `gather.py` из `evidence.build_evidence`.

Форма `evidence` (по ключам объектов `level:id`):

* `target` — ответ `model.resolve_target` (`value`, `source`, `reason`);
* `accounts` — `{аккаунт: ответ build_evidence}` (identity, numbers, windows, automatic…);
* `objects` — `{ключ: {account, level, id, name, status, parent_status, daily_budget_amount,
  budget_owner, age_days, ancestors, descendants}}`;
* `blocks` — для S3: `{ключ: [{spend, ftds}, …]}` от старого блока к новому;
* `compare` — для S4: `{ключ: {current: {...}, base: {...}}}`;
* `holds`, `last_action_at` — ручные удержания и время последнего исполненного действия.

Итог по объекту — один из трёх вердиктов: `matched` / `not_matched` / `blocked`.
«Условие совпало» (`conditions_matched`) и «разрешено» — разные вещи (§1.4).

Неопределённое остаётся неопределённым: отношение без знаменателя — `None`,
нулевое число исходов не даёт нулевого CPA, сравнение с `None` — «неизвестно», и
такая оценка не уходит в `matched`, а упирается в `Insufficient evidence`.
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timedelta, timezone
from typing import Any

from . import evidence as ev
from . import model

MATCHED = "matched"
NOT_MATCHED = "not_matched"
BLOCKED = "blocked"

# Причины «почему не сработало» (§1.6). Часть приходит из evidence.py.
INSUFFICIENT_EVIDENCE = ev.INSUFFICIENT_EVIDENCE
PENDING_OUTCOMES = ev.PENDING_OUTCOMES
STALE_BI = ev.STALE_BI
MISSING_MAPPING = ev.MISSING_MAPPING
AMBIGUOUS_MAPPING = ev.AMBIGUOUS_MAPPING
NOT_RECONCILED = "Not reconciled"
NO_TARGET = model.NO_TARGET
MANUAL_HOLD = "Manual hold"
PARENT_PAUSED = "Parent paused"
COOLDOWN = "Cooldown"
UNSUPPORTED_ACTION = "Unsupported action"

REASONS = (INSUFFICIENT_EVIDENCE, PENDING_OUTCOMES, STALE_BI, MISSING_MAPPING, AMBIGUOUS_MAPPING,
           NOT_RECONCILED, NO_TARGET, MANUAL_HOLD, PARENT_PAUSED, COOLDOWN, UNSUPPORTED_ACTION)

#: Эти вердикты идентичности доказательств не дают: объект неоднозначен, цифры — смесь.
_AMBIGUOUS_BLOCK = frozenset({ev.DUPLICATE_NAME, ev.PARTIAL, ev.MULTI_COUNTRY, ev.RENAMED})
#: Эти допускают Observe/Suggest, но не Automatic: оценка идёт с пометкой.
_WEAK_NOTE = frozenset({ev.ALIAS_HEURISTIC, ev.COPY_SUFFIX})

#: Действия, которые v1 умеет предлагать. `enable` - только Suggest у человека, в оценке его нет.
_SUPPORTED_ACTIONS = frozenset({"pause", "budget_up", "alert", "block_scale", "draft_replacement"})

#: Статусы Meta, при которых объект уже выключен.
_PAUSED = frozenset({"PAUSED", "CAMPAIGN_PAUSED", "ADSET_PAUSED"})


# ───────────────────────── сравнения (три значения) ─────────────────────────

def _cmp(left: float | None, op: str, right: float | None) -> bool | None:
    """`None` - неизвестно: хотя бы одна сторона не определена."""
    if left is None or right is None:
        return None
    if isinstance(left, float) and not math.isfinite(left):
        return None
    if op == ">=":
        return left >= right
    if op == ">":
        return left > right
    if op == "<=":
        return left <= right
    if op == "<":
        return left < right
    return left == right


def _all3(values: list[bool | None]) -> bool | None:
    """И по Клини: любой False - False, иначе любой None - None."""
    if any(v is False for v in values):
        return False
    return None if any(v is None for v in values) else True


def _any3(values: list[bool | None]) -> bool | None:
    if any(v is True for v in values):
        return True
    return None if any(v is None for v in values) else False


def _ratio(num: float | None, den: float | None) -> float | None:
    """Отношение без знаменателя не определено (не 0 и не бесконечность)."""
    if num is None or not den:
        return None
    return num / den


def _cpa(spend: float | None, ftds: int | float | None) -> float | None:
    """Нулевые исходы не дают нулевого CPA: ноль исходов - CPA не определён."""
    if spend is None or not ftds:
        return None
    return spend / ftds


def _pct_change(cur: float | None, base: float | None) -> float | None:
    if cur is None or base is None or base == 0:
        return None
    return (cur / base - 1.0) * 100.0


# ───────────────────────── метрики объекта ─────────────────────────

def _metrics(strategy: int, num: dict | None, blocks: list[dict] | None, compare: dict | None,
             age_days: int | None) -> dict[str, float | None]:
    """Значения метрик условий для одного объекта. Чего нет - `None`."""
    num = num or {}
    spend, ftds, leads = num.get("spend"), num.get("ftds"), num.get("leads")
    out: dict[str, float | None] = {
        "spend": spend, "ftd": ftds, "leads": leads,
        "cpa": _cpa(spend, ftds), "cr": _ratio(ftds, leads), "age_days": age_days,
        "ftd_total": None, "ftd_block": None, "cpa_block": None,
        "cpa_change_pct": None, "cr_change_pct": None,
    }
    if blocks:
        known = [b for b in blocks if b.get("ftds") is not None and b.get("spend") is not None]
        if len(known) == len(blocks):
            out["ftd_total"] = float(sum(b["ftds"] for b in blocks))
            # «в обоих блоках»: берём худший блок, поэтому одно сравнение закрывает все
            out["ftd_block"] = float(min(b["ftds"] for b in blocks))
            cpas = [_cpa(b["spend"], b["ftds"]) for b in blocks]
            out["cpa_block"] = None if any(c is None for c in cpas) else max(cpas)
    if compare:
        cur, base = compare.get("current") or {}, compare.get("base") or {}
        out["cpa_change_pct"] = _pct_change(_cpa(cur.get("spend"), cur.get("ftds")),
                                            _cpa(base.get("spend"), base.get("ftds")))
        out["cr_change_pct"] = _pct_change(_ratio(cur.get("ftds"), cur.get("leads")),
                                           _ratio(base.get("ftds"), base.get("leads")))
    return out


def _leaf(cond: dict, metrics: dict, target: float | None) -> bool | None:
    value = metrics.get(cond.get("metric"))
    unit, right = cond.get("unit"), cond.get("value")
    if unit == "t_multiple":
        if target is None:
            return None
        right = float(right) * target
    elif unit == "pct" and cond.get("metric") == "cr" and value is not None:
        value = value * 100.0  # доля -> проценты; *_change_pct уже в процентах
    return _cmp(value, cond.get("cmp"), right)


def _conditions(conds: list, metrics: dict, target: float | None) -> tuple[bool | None, list[dict]]:
    """Все верхнеуровневые условия связаны через AND; группа - своим `op`."""
    detail: list[dict] = []

    def one(c: dict) -> bool | None:
        if "items" in c:
            parts = [one(x) for x in c["items"]]
            return _all3(parts) if c.get("op") == "AND" else _any3(parts)
        res = _leaf(c, metrics, target)
        detail.append({"metric": c.get("metric"), "cmp": c.get("cmp"), "value": c.get("value"),
                       "unit": c.get("unit"), "observed": metrics.get(c.get("metric")), "result": res})
        return res

    if not conds:
        return None, detail
    return _all3([one(c) for c in conds]), detail


def _needs_target(version: dict) -> bool:
    def walk(items: list) -> bool:
        for c in items:
            if isinstance(c, dict):
                if "items" in c and walk(c["items"]):
                    return True
                if c.get("unit") == "t_multiple":
                    return True
        return False

    act = version.get("action") or {}
    return walk(version.get("conditions") or []) or act.get("alert_at_t_multiple") is not None


# ───────────────────────── вспомогательное ─────────────────────────

def _now(value: Any) -> datetime:
    """Момент оценки обязателен: скрытых часов у чистой функции нет."""
    moment = ev._utc(value)  # noqa: SLF001
    if moment is None:
        raise ValueError("evaluate() needs an explicit `now`")
    return moment


def _fingerprint(strategy: int, key: str, observed: dict, windows: Any, blocks: Any, compare: Any,
                 verdict: str, target: Any, action: Any, bi_through: str) -> str:
    """Отпечаток доказательств: не зависит от времени оценки, только от данных."""
    raw = json.dumps({"s": strategy, "k": key, "o": observed, "w": windows, "b": blocks, "c": compare,
                      "i": verdict, "t": target, "a": action, "d": bi_through},
                     sort_keys=True, default=str, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _label(reason: str) -> str:
    """`Not reconciled: currency EUR` -> `Not reconciled` (деталь уходит в notes)."""
    return NOT_RECONCILED if reason.startswith(NOT_RECONCILED) else reason


def _blocks_need(version: dict) -> int:
    act = version.get("action") or {}
    return int(act.get("blocks") or 2) if version.get("strategy") == 3 else 0


# ───────────────────────── оценка одного объекта ─────────────────────────

def _evaluate_object(version: dict, evidence: dict, key: str, now: datetime, mode: str,
                     unsupported: bool) -> dict:
    strategy = version.get("strategy")
    act = version.get("action") or {}
    kind = act.get("kind")
    info = (evidence.get("objects") or {}).get(key) or {}
    acct = info.get("account")
    build = (evidence.get("accounts") or {}).get(acct) or {}
    ident = (build.get("identity") or {}).get(key)
    num = (build.get("numbers") or {}).get(key)
    windows = build.get("windows") or {}
    target = evidence.get("target") or {}
    t_value = target.get("value")
    blocks = (evidence.get("blocks") or {}).get(key)
    compare = (evidence.get("compare") or {}).get(key)
    descendants = list(info.get("descendants") or [])
    ancestors = list(info.get("ancestors") or [])

    reasons: list[str] = []
    notes: list[str] = []

    def add(reason: str, note: str = "") -> None:
        if reason not in reasons:
            reasons.append(reason)
        if note:
            notes.append(note)

    out: dict[str, Any] = {
        "key": key, "account": acct, "level": info.get("level"), "id": info.get("id"),
        "name": info.get("name"), "verdict": BLOCKED, "reasons": reasons, "notes": notes,
        "identity": (ident or {}).get("verdict"), "observed": {}, "conditions": [],
        "conditions_matched": None, "action": None, "affected": [key] + descendants,
        "lineage": [key] + descendants + ancestors, "automatic_eligible": False, "fingerprint": "",
    }

    # 1. что не оценивается вовсе
    if unsupported:
        add(UNSUPPORTED_ACTION, "strategy 5 or an action the rules cannot propose in v1")
    # Ручное удержание не мешает считать условия: проигрывающий объект остаётся проигрывающим
    # и гасит scale, даже если пауза ему сейчас не разрешена.
    hold = bool((evidence.get("holds") or {}).get(key))

    # 2. идентичность: «нет строк» и «неоднозначно» - разные причины
    verdict_id = (ident or {}).get("verdict")
    if ident is None or verdict_id == ev.NOT_IN_WORKBOOK:
        add(MISSING_MAPPING, (ident or {}).get("reason") or (build.get("error") or "no workbook rows for the object"))
    elif verdict_id in _AMBIGUOUS_BLOCK:
        add(AMBIGUOUS_MAPPING, ident.get("reason") or verdict_id)
    elif verdict_id in _WEAK_NOTE:
        notes.append(f"identity is {verdict_id}: Observe/Suggest only, Automatic is not allowed")

    # 3. цель T
    if _needs_target(version) and t_value is None:
        add(NO_TARGET, target.get("reason") or NO_TARGET)

    # 4. достаточность доказательств (только если идентичность не блок)
    if MISSING_MAPPING not in reasons and AMBIGUOUS_MAPPING not in reasons and not unsupported:
        _sufficiency(version, num, windows, blocks, compare, info, add)

    metrics = _metrics(strategy, num, blocks, compare, info.get("age_days"))
    out["observed"] = {k: v for k, v in metrics.items() if v is not None}
    out["observed"]["target"] = t_value

    # freshness - ярлык для Observe/Suggest, блок для Automatic (§4.2)
    auto = (build.get("automatic") or {}).get(key) or {}
    fresh_labels = [_label(r) for r in (auto.get("reasons") or build.get("global_blockers") or [])
                    if _label(r) in (STALE_BI, NOT_RECONCILED, ev.STALE_SNAPSHOT, ev.DATA_MISMATCH)]
    out["freshness"] = fresh_labels
    out["automatic_eligible"] = bool(auto.get("allowed")) and not unsupported
    # Automatic не пропускает то, что evidence не признал годным (слабая идентичность,
    # свежесть, сверка): блокеры берутся из его причин.
    auto_blockers: list[str] = []
    if mode == "automatic" and not out["automatic_eligible"]:
        auto_blockers = [_label(r) for r in (auto.get("reasons") or [])] or [AMBIGUOUS_MAPPING]

    if reasons:
        if hold:
            add(MANUAL_HOLD, "a person put a hold on this object")
        out["fingerprint"] = _fingerprint(strategy, key, out["observed"], windows, blocks, compare,
                                          verdict_id or "", target, None, build.get("bi_through", ""))
        return out

    # 5. условия
    res, detail = _conditions(version.get("conditions") or [], metrics, t_value)
    out["conditions"], out["conditions_matched"] = detail, res
    proposed = None
    if res is None:
        add(INSUFFICIENT_EVIDENCE, "a condition has an undefined value, so nothing is proven")
    elif res is False:
        proposed = _alert_path(version, metrics, t_value)
        if proposed is not None:
            out["conditions_matched"] = True
            res = True
    else:
        proposed = {"kind": kind}
    if res is not True:
        out["verdict"] = BLOCKED if reasons else NOT_MATCHED
        out["fingerprint"] = _fingerprint(strategy, key, out["observed"], windows, blocks, compare,
                                          verdict_id or "", target, None, build.get("bi_through", ""))
        return out

    # 6. совпало: теперь «разрешено ли» (пауза родителя, cooldown, бюджет)
    action = _build_action(version, proposed, key, info, now, add)
    if hold:
        add(MANUAL_HOLD, "a person put a hold on this object")
    if action is None and not reasons:
        # нечего делать: уже выключен или упёрлись в потолок
        out["verdict"] = NOT_MATCHED
        out["fingerprint"] = _fingerprint(strategy, key, out["observed"], windows, blocks, compare,
                                          verdict_id or "", target, None, build.get("bi_through", ""))
        return out
    if action is not None and action["kind"] in ("pause", "budget_up"):
        _post_blockers(version, evidence, key, info, now, add)
        for r in auto_blockers:
            add(r, "Automatic is not allowed for this object")
    out["action"] = action
    out["fingerprint"] = _fingerprint(strategy, key, out["observed"], windows, blocks, compare,
                                      verdict_id or "", target, action, build.get("bi_through", ""))
    out["verdict"] = BLOCKED if reasons else MATCHED
    if reasons:
        out["action"] = None
        out["blocked_action"] = action
    return out


def _sufficiency(version: dict, num: dict | None, windows: dict, blocks: Any, compare: Any, info: dict,
                 add) -> None:
    strategy = version.get("strategy")
    evi = version.get("evidence") or {}
    if num is None or num.get("state") == "no_data":
        add(INSUFFICIENT_EVIDENCE, "no numbers for the object: missing rows are not zero")
        return
    need_age = int(evi.get("min_age_days") or 0)
    if need_age > 0:
        age = info.get("age_days")
        if age is None:
            add(INSUFFICIENT_EVIDENCE, "the age of the object is unknown")
        elif age < need_age:
            add(INSUFFICIENT_EVIDENCE, f"the object is {age} days old, the minimum is {need_age}")
    if windows and windows.get("spend") is None:
        add(PENDING_OUTCOMES, "no mature days yet: all days of the window are still maturing")
        return
    if num.get("state") == "no_mature_spend":
        add(INSUFFICIENT_EVIDENCE, "no spend in the mature window")
        return
    if strategy == 3:
        need = _blocks_need(version)
        if not blocks or len(blocks) < need:
            add(INSUFFICIENT_EVIDENCE, f"scale needs {need} mature blocks")
        elif any(b.get("spend") is None or b.get("ftds") is None for b in blocks):
            add(INSUFFICIENT_EVIDENCE, "a block has no numbers")
        elif any(not b.get("spend") for b in blocks):
            add(INSUFFICIENT_EVIDENCE, "a block has no spend")
        if num.get("reason") == PENDING_OUTCOMES:
            add(PENDING_OUTCOMES, "the freshest days do not hold the target yet")
    if strategy == 4:
        cur, base = (compare or {}).get("current") or {}, (compare or {}).get("base") or {}
        if not compare or cur.get("spend") is None or base.get("spend") is None:
            add(INSUFFICIENT_EVIDENCE, "no matched periods to compare")
    sample = int(evi.get("min_sample") or 0)
    if strategy in (2, 3, 4) and sample > 0:
        total = None
        if strategy == 3 and blocks:
            total = sum(b.get("ftds") or 0 for b in blocks)
        elif strategy == 4:
            total = ((compare or {}).get("base") or {}).get("ftds")
        else:
            total = num.get("leads") if evi.get("outcome") == "leads" else num.get("ftds")
        if total is None or total < sample:
            add(INSUFFICIENT_EVIDENCE, f"fewer than {sample} outcomes")


def _alert_path(version: dict, metrics: dict, t_value: float | None) -> dict | None:
    """S1: пауза не совпала, но spend уже за порогом тревоги при нуле исходов - алерт."""
    act = version.get("action") or {}
    k = act.get("alert_at_t_multiple")
    if version.get("strategy") != 1 or act.get("kind") != "pause" or k is None or t_value is None:
        return None
    spend, ftd = metrics.get("spend"), metrics.get("ftd")
    if spend is not None and ftd == 0 and spend >= float(k) * t_value:
        return {"kind": "alert", "reason": f"spend >= {k}T with no outcomes"}
    return None


def _build_action(version: dict, proposed: dict, key: str, info: dict, now: datetime, add) -> dict | None:
    act = version.get("action") or {}
    kind = proposed.get("kind")
    if kind not in _SUPPORTED_ACTIONS:
        add(UNSUPPORTED_ACTION, f"action {kind!r} is not supported")
        return None
    base = {"kind": kind, "target": key, "level": info.get("level"), "id": info.get("id"),
            "account": info.get("account")}
    if proposed.get("reason"):
        base["reason"] = proposed["reason"]
    status = str(info.get("status") or "").upper()
    eff = str(info.get("effective_status") or "").upper()
    # свой статус PAUSED - выключено; CAMPAIGN_PAUSED/ADSET_PAUSED - выключен родитель (другая причина)
    paused_self = status == "PAUSED" or eff == "PAUSED"
    if kind == "pause":
        if paused_self:
            return None  # уже выключен: нечего писать
        base["requested"] = {"status": "PAUSED"}
        base["before"] = {"status": status or None}
        return base
    if kind == "budget_up":
        if paused_self:
            add(MANUAL_HOLD, "the object is paused: a pause is somebody's decision, not scaled")
            return None
        if info.get("level") == "ad" or info.get("lifetime_budget") or not info.get("budget_owner"):
            add(UNSUPPORTED_ACTION, "scale needs a daily budget owned by this object (CBO campaign or own ad set)")
            return None
        before = info.get("daily_budget_amount")
        if before is None or before <= 0:
            add(UNSUPPORTED_ACTION, "the daily budget is unknown")
            return None
        step = float(act.get("step_pct") or 10)
        requested = math.floor(before * (1.0 + step / 100.0))
        cap = (version.get("caps") or {}).get("max_daily_budget")
        if cap:
            requested = min(requested, math.floor(cap))
        if requested <= before:
            return None  # потолок или округление: подъёма нет
        base["before"] = {"daily_budget": before}
        base["requested"] = {"daily_budget": requested}
        return base
    return base


def _post_blockers(version: dict, evidence: dict, key: str, info: dict, now: datetime, add) -> None:
    parent = str(info.get("parent_status") or "").upper()
    if parent in _PAUSED or str(info.get("effective_status") or "").upper() in ("CAMPAIGN_PAUSED", "ADSET_PAUSED"):
        add(PARENT_PAUSED, "a parent is paused: nothing to write")
    last = _now((evidence.get("last_action_at") or {}).get(key)) if (evidence.get("last_action_at") or {}).get(key) else None
    hours = float((version.get("caps") or {}).get("cooldown_hours") or 0)
    if last is not None and hours > 0 and now - last < timedelta(hours=hours):
        add(COOLDOWN, f"the last action was {last.isoformat(timespec='minutes')}, cooldown {hours:g} h")


# ───────────────────────── публичная функция ─────────────────────────

def evaluate(rule_version: dict, evidence: dict, now: Any, *, mode: str = "suggest") -> dict:
    """Оценить одну версию правила на готовых доказательствах.

    `mode="automatic"` превращает устаревшую книгу/снимок и отсутствие сверки в
    блокеры (`Stale BI`, `Not reconciled`); в остальных режимах они - ярлык
    `freshness` у объекта (§4.2). Вердикт правила: `matched`, если совпал хоть
    один объект; `blocked`, если все заблокированы (или правило не поддерживается);
    иначе `not_matched`.
    """
    moment = _now(now)  # ValueError без явного `now`
    strategy = rule_version.get("strategy")
    kind = (rule_version.get("action") or {}).get("kind")
    unsupported = strategy in model.UNSUPPORTED_STRATEGIES or kind not in _SUPPORTED_ACTIONS
    keys = list((evidence.get("objects") or {}).keys())
    results = [_evaluate_object(rule_version, evidence, k, moment, mode, unsupported) for k in keys]
    counts = {MATCHED: 0, NOT_MATCHED: 0, BLOCKED: 0}
    for r in results:
        counts[r["verdict"]] += 1
    reasons: list[str] = []
    for r in results:
        for x in r["reasons"]:
            if x not in reasons:
                reasons.append(x)
    if unsupported and not results:
        reasons.append(UNSUPPORTED_ACTION)
    if counts[MATCHED]:
        verdict = MATCHED
    elif unsupported or (results and counts[BLOCKED] == len(results)):
        verdict = BLOCKED
    else:
        verdict = NOT_MATCHED
    return {
        "strategy": strategy, "verdict": verdict, "reasons": reasons, "unsupported": unsupported,
        "mode": mode, "evaluated_at": moment.isoformat(timespec="seconds"),
        "target": evidence.get("target"), "counts": counts, "objects": results,
        "proposals": [r for r in results if r["verdict"] == MATCHED and r["action"]],
    }
