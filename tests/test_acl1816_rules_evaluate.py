"""ACL-1816: оценка правил - evaluate, арбитр, триггеры, журналы, без записи в Meta.

Только заглушки: ни Meta, ни Multilogin. Чистые проверки `evaluate` идут на
собранных вручную доказательствах; сквозные (gather + движок) - на маленькой
книге и снимке описи, как в тестах T2.
"""
from __future__ import annotations

import copy
import re
import sys
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.analysis_agent import campaign_inventory as ci  # noqa: E402
from src.analysis_agent.rules import arbiter, engine as engine_mod, evaluate as E, evidence as ev, gather, model  # noqa: E402
from src.analysis_agent.rules.store import RulesStore  # noqa: E402

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
BUILT = (NOW - timedelta(hours=2)).isoformat()


# ───────────────────────── сборка входа вручную ─────────────────────────

def info(level="adset", oid="11", **kw):
    base = {"account": "Acc", "level": level, "id": oid, "name": f"{level} {oid}", "status": "ACTIVE",
            "effective_status": "ACTIVE", "parent_status": "ACTIVE", "age_days": 8, "ancestors": [],
            "descendants": []}
    return {**base, **kw}


def num(spend=350.0, ftds=0, leads=10, state=None, **kw):
    state = state or ("zero_ftd" if ftds == 0 and spend else "ok" if spend else "no_mature_spend")
    return {"key": "x", "verdict": "exact", "state": state, "spend": spend, "leads": leads, "ftds": ftds,
            "cpa": (spend / ftds) if (spend and ftds) else None, "zero_ftd": state == "zero_ftd",
            "reason": "", **kw}


def ident(verdict=ev.EXACT):
    return {"verdict": verdict, "automatic_ok": verdict in ev.AUTOMATIC_VERDICTS, "blocker": "", "reason": verdict}


def evid(key="adset:11", *, numbers=None, verdict=ev.EXACT, obj=None, target=100.0, windows=None,
         auto=None, blocks=None, compare=None, holds=None, last_action_at=None, global_blockers=()):
    obj = obj or info(*key.split(":"))
    numbers = num() if numbers is None else numbers
    return {
        "target": {"value": target, "source": "goal" if target else None,
                   "reason": "" if target else "No target for Chile"},
        "accounts": {"Acc": {
            "identity": {key: ident(verdict)}, "numbers": {key: numbers},
            "windows": windows if windows is not None else {"spend": ["2026-10-05", "2026-10-09"]},
            "automatic": {key: auto or {"allowed": True, "reasons": []}},
            "global_blockers": list(global_blockers), "bi_through": "2026-10-11"}},
        "objects": {key: obj}, "blocks": {key: blocks} if blocks else {}, "compare": {key: compare} if compare else {},
        "holds": holds or {}, "last_action_at": last_action_at or {},
    }


def stop_loss(**over):
    v, _mode = model.preset("stop_loss_new_tests", **over)
    return {**v, "version": 1}


def scale(**over):
    v, _mode = model.preset("scale_proven", **over)
    return {**v, "version": 1}


def one(result):
    assert len(result["objects"]) == 1
    return result["objects"][0]


# ───────────────────────── S1 stop-loss ─────────────────────────

def test_s1_match_proposes_a_pause_and_not_a_write():
    res = E.evaluate(stop_loss(), evid(), NOW)
    o = one(res)
    assert res["verdict"] == E.MATCHED and o["verdict"] == E.MATCHED
    assert o["action"]["kind"] == "pause" and o["action"]["requested"] == {"status": "PAUSED"}
    assert o["action"]["target"] == "adset:11" and o["affected"] == ["adset:11"]
    assert o["conditions_matched"] is True and o["reasons"] == []


def test_s1_alert_between_2t_and_3t():
    o = one(E.evaluate(stop_loss(), evid(numbers=num(spend=250.0)), NOW))
    assert o["verdict"] == E.MATCHED and o["action"]["kind"] == "alert"


@pytest.mark.parametrize("numbers", [num(spend=100.0), num(spend=500.0, ftds=2, leads=30)])
def test_s1_not_matched(numbers):
    res = E.evaluate(stop_loss(), evid(numbers=numbers), NOW)
    assert res["verdict"] == E.NOT_MATCHED and one(res)["action"] is None and one(res)["reasons"] == []


def test_s1_reasons_each_have_their_own_label():
    cases = {
        E.MISSING_MAPPING: evid(verdict=ev.NOT_IN_WORKBOOK),
        E.AMBIGUOUS_MAPPING: evid(verdict=ev.DUPLICATE_NAME),
        E.NO_TARGET: evid(target=None),
        E.MANUAL_HOLD: evid(holds={"adset:11": {"reason": "manual"}}),
        E.INSUFFICIENT_EVIDENCE: evid(numbers=num(state="no_data", spend=None, ftds=None, leads=None)),
        E.PENDING_OUTCOMES: evid(windows={"spend": None}),
        E.PARENT_PAUSED: evid(obj=info(parent_status="PAUSED")),
        E.COOLDOWN: evid(last_action_at={"adset:11": (NOW - timedelta(hours=5)).isoformat()}),
    }
    for want, ev_in in cases.items():
        res = E.evaluate(stop_loss(), ev_in, NOW)
        assert res["verdict"] == E.BLOCKED, want
        assert want in one(res)["reasons"], want
        assert one(res)["action"] is None
    # cooldown истёк - снова совпало
    old = evid(last_action_at={"adset:11": (NOW - timedelta(hours=30)).isoformat()})
    assert E.evaluate(stop_loss(), old, NOW)["verdict"] == E.MATCHED
    # ребёнок под остановленной кампанией: Meta показывает CAMPAIGN_PAUSED
    child = evid(obj=info(effective_status="CAMPAIGN_PAUSED"))
    assert E.PARENT_PAUSED in one(E.evaluate(stop_loss(), child, NOW))["reasons"]


@pytest.mark.parametrize("verdict", [ev.DUPLICATE_NAME, ev.PARTIAL, ev.MULTI_COUNTRY, ev.RENAMED])
def test_ambiguous_identities_block(verdict):
    o = one(E.evaluate(stop_loss(), evid(verdict=verdict), NOW))
    assert o["verdict"] == E.BLOCKED and o["reasons"] == [E.AMBIGUOUS_MAPPING]


@pytest.mark.parametrize("verdict", [ev.ALIAS_HEURISTIC, ev.COPY_SUFFIX])
def test_weak_identity_evaluates_with_a_note_but_is_not_automatic(verdict):
    e = evid(verdict=verdict, auto={"allowed": False, "reasons": [E.AMBIGUOUS_MAPPING]})
    o = one(E.evaluate(stop_loss(), e, NOW))
    assert o["verdict"] == E.MATCHED and not o["automatic_eligible"] and any(verdict in n for n in o["notes"])


def test_object_missing_from_the_workbook_is_blocked_never_zero():
    """«Нет строк / нет объекта» - Missing mapping, а не FTD=0 и не пауза."""
    e = evid()
    e["accounts"]["Acc"]["identity"] = {}
    e["accounts"]["Acc"]["numbers"] = {}
    o = one(E.evaluate(stop_loss(), e, NOW))
    assert o["verdict"] == E.BLOCKED and o["reasons"] == [E.MISSING_MAPPING] and o["action"] is None
    # и явный not_in_workbook с нулевыми числами тоже
    e2 = evid(verdict=ev.NOT_IN_WORKBOOK, numbers=num(spend=500.0, ftds=0))
    o2 = one(E.evaluate(stop_loss(), e2, NOW))
    assert o2["verdict"] == E.BLOCKED and o2["reasons"] == [E.MISSING_MAPPING] and o2["action"] is None


def test_young_or_ageless_objects_are_insufficient_evidence():
    assert E.INSUFFICIENT_EVIDENCE in one(E.evaluate(stop_loss(), evid(obj=info(age_days=2)), NOW))["reasons"]
    assert E.INSUFFICIENT_EVIDENCE in one(E.evaluate(stop_loss(), evid(obj=info(age_days=None)), NOW))["reasons"]


def test_already_paused_object_is_not_a_pause_candidate():
    o = one(E.evaluate(stop_loss(), evid(obj=info(status="PAUSED", effective_status="PAUSED")), NOW))
    assert o["verdict"] == E.NOT_MATCHED and o["action"] is None


def test_freshness_is_a_label_for_suggest_and_a_blocker_for_automatic():
    auto = {"allowed": False, "reasons": [E.STALE_BI, "Not reconciled: currency EUR"]}
    soft = one(E.evaluate(stop_loss(), evid(auto=auto), NOW, mode="suggest"))
    assert soft["verdict"] == E.MATCHED and soft["freshness"] == [E.STALE_BI, E.NOT_RECONCILED]
    assert not soft["automatic_eligible"]
    hard = one(E.evaluate(stop_loss(), evid(auto=auto), NOW, mode="automatic"))
    assert hard["verdict"] == E.BLOCKED and set(hard["reasons"]) == {E.STALE_BI, E.NOT_RECONCILED}


def test_the_reason_list_is_the_design_list():
    assert E.REASONS == ("Insufficient evidence", "Pending outcomes", "Stale BI", "Missing mapping",
                         "Ambiguous mapping", "Not reconciled", "No target", "Manual hold", "Parent paused",
                         "Cooldown", "Unsupported action")


# ───────────────────────── неопределённое остаётся неопределённым ─────────────────────────

def test_zero_outcomes_never_make_a_zero_cpa():
    assert E._cpa(100.0, 0) is None and E._cpa(0.0, 0) is None and E._cpa(None, 3) is None  # noqa: SLF001
    assert E._ratio(5, 0) is None  # noqa: SLF001
    m = E._metrics(1, num(spend=300.0, ftds=0), None, None, 5)  # noqa: SLF001
    assert m["cpa"] is None and m["cr"] == 0.0 and m["ftd"] == 0
    # условие «cpa <= 0.8T» при нуле исходов не совпадает «победой»: оно неизвестно
    v = stop_loss(conditions=[{"metric": "cpa", "cmp": "<=", "value": 0.8, "unit": "t_multiple"}])
    o = one(E.evaluate(v, evid(numbers=num(spend=300.0, ftds=0)), NOW))
    assert o["verdict"] == E.BLOCKED and o["reasons"] == [E.INSUFFICIENT_EVIDENCE] and o["action"] is None


def test_three_valued_logic_in_groups():
    t, f, n = True, False, None
    assert E._all3([t, n]) is None and E._all3([f, n]) is False and E._all3([t, t]) is True  # noqa: SLF001
    assert E._any3([f, n]) is None and E._any3([t, n]) is True and E._any3([f, f]) is False  # noqa: SLF001
    assert E._cmp(None, ">=", 1) is None and E._cmp(float("nan"), ">=", 1) is None  # noqa: SLF001


# ───────────────────────── S3 scale ─────────────────────────

def scale_ev(blocks, *, target=50.0, obj=None, numbers=None, **kw):
    obj = obj or info("campaign", "1", budget_owner=True, daily_budget_amount=100.0, age_days=10)
    numbers = numbers or num(spend=220.0, ftds=11, leads=60)
    return evid("campaign:1", numbers=numbers, obj=obj, target=target, blocks=blocks, **kw)


GOOD = [{"spend": 120.0, "ftds": 5}, {"spend": 100.0, "ftds": 6}]


def test_s3_match_raises_budget_ten_percent_rounded_down():
    o = one(E.evaluate(scale(), scale_ev(GOOD), NOW))
    assert o["verdict"] == E.MATCHED
    assert o["action"]["kind"] == "budget_up"
    assert o["action"]["before"] == {"daily_budget": 100.0} and o["action"]["requested"] == {"daily_budget": 110}
    assert o["observed"]["ftd_total"] == 11 and o["observed"]["cpa_block"] == pytest.approx(24.0)


def test_s3_cap_limits_the_step_and_stops_at_the_cap():
    capped = scale(caps={"max_daily_budget": 105})
    assert one(E.evaluate(capped, scale_ev(GOOD), NOW))["action"]["requested"] == {"daily_budget": 105}
    at_cap = scale(caps={"max_daily_budget": 100})
    assert one(E.evaluate(at_cap, scale_ev(GOOD), NOW))["verdict"] == E.NOT_MATCHED


def test_s3_not_matched_when_a_block_is_too_expensive_or_thin():
    dear = [{"spend": 300.0, "ftds": 5}, {"spend": 100.0, "ftds": 6}]  # 60 > 0.8 * 50
    assert E.evaluate(scale(), scale_ev(dear), NOW)["verdict"] == E.NOT_MATCHED
    thin = [{"spend": 100.0, "ftds": 2}, {"spend": 100.0, "ftds": 9}]  # ftd_block 2 < 3
    assert E.evaluate(scale(), scale_ev(thin), NOW)["verdict"] == E.NOT_MATCHED


def test_s3_blockers():
    assert E.INSUFFICIENT_EVIDENCE in one(E.evaluate(scale(), scale_ev([GOOD[0]]), NOW))["reasons"]
    few = [{"spend": 100.0, "ftds": 2}, {"spend": 100.0, "ftds": 3}]  # 5 исходов < min_sample 10
    assert E.INSUFFICIENT_EVIDENCE in one(E.evaluate(scale(), scale_ev(few), NOW))["reasons"]
    zero_block = [{"spend": 100.0, "ftds": 0}, {"spend": 100.0, "ftds": 9}]
    o = one(E.evaluate(scale(), scale_ev(zero_block), NOW))
    assert o["verdict"] != E.MATCHED and o["action"] is None  # ноль исходов не даёт CPA=0
    pending = scale_ev(GOOD, numbers=num(spend=220.0, ftds=11, leads=60, reason=E.PENDING_OUTCOMES))
    assert E.PENDING_OUTCOMES in one(E.evaluate(scale(), pending, NOW))["reasons"]
    assert E.NO_TARGET in one(E.evaluate(scale(), scale_ev(GOOD, target=None), NOW))["reasons"]
    lifetime = scale_ev(GOOD, obj=info("campaign", "1", budget_owner=True, lifetime_budget=True,
                                       daily_budget_amount=None, age_days=10))
    assert E.UNSUPPORTED_ACTION in one(E.evaluate(scale(), lifetime, NOW))["reasons"]
    under_cbo = evid("adset:11", numbers=num(spend=220.0, ftds=11, leads=60), target=50.0, blocks=GOOD,
                     obj=info("adset", "11", budget_owner=False, daily_budget_amount=None, age_days=10))
    assert E.UNSUPPORTED_ACTION in one(E.evaluate(scale(), under_cbo, NOW))["reasons"]
    paused = scale_ev(GOOD, obj=info("campaign", "1", status="PAUSED", effective_status="PAUSED",
                                     budget_owner=True, daily_budget_amount=100.0, age_days=10))
    assert E.MANUAL_HOLD in one(E.evaluate(scale(), paused, NOW))["reasons"]
    cooling = scale_ev(GOOD, last_action_at={"campaign:1": (NOW - timedelta(hours=3)).isoformat()})
    assert E.COOLDOWN in one(E.evaluate(scale(), cooling, NOW))["reasons"]


# ───────────────────────── S2 / S4 / S5 ─────────────────────────

def test_s2_funnel_quality_blocks_scale_only_as_an_alert():
    v = model.make_version(name="Quality guard", strategy=2, priority=20, evidence={"min_age_days": 0},
                           conditions=[{"op": "AND", "items": [
                               {"metric": "leads", "cmp": ">=", "value": 30, "unit": "abs"},
                               {"metric": "cr", "cmp": "<", "value": 2, "unit": "pct"}]}],
                           action={"kind": "block_scale"})
    v["version"] = 1
    bad = one(E.evaluate(v, evid(numbers=num(spend=500.0, ftds=1, leads=100)), NOW))
    assert bad["verdict"] == E.MATCHED and bad["action"]["kind"] == "block_scale"
    good = one(E.evaluate(v, evid(numbers=num(spend=500.0, ftds=10, leads=100)), NOW))
    assert good["verdict"] == E.NOT_MATCHED
    few = one(E.evaluate(v, evid(numbers=num(spend=500.0, ftds=0, leads=10)), NOW))
    assert few["verdict"] == E.NOT_MATCHED  # мало лидов: блока нет


def test_s4_deterioration_partial_alert_and_draft():
    v = model.make_version(name="Creative drift", strategy=4, priority=40,
                           evidence={"min_age_days": 0, "min_sample": 5},
                           conditions=[{"op": "AND", "items": [
                               {"metric": "cpa_change_pct", "cmp": ">=", "value": 25, "unit": "pct"},
                               {"metric": "cr_change_pct", "cmp": "<=", "value": -25, "unit": "pct"}]}],
                           action={"kind": "draft_replacement"})
    v["version"] = 1
    worse = {"current": {"spend": 200.0, "leads": 40, "ftds": 2}, "base": {"spend": 200.0, "leads": 40, "ftds": 6}}
    o = one(E.evaluate(v, evid("ad:7", compare=worse, obj=info("ad", "7")), NOW))
    assert o["verdict"] == E.MATCHED and o["action"]["kind"] == "draft_replacement"
    same = {"current": worse["base"], "base": worse["base"]}
    assert one(E.evaluate(v, evid("ad:7", compare=same, obj=info("ad", "7")), NOW))["verdict"] == E.NOT_MATCHED
    nobase = {"current": worse["current"], "base": {"spend": 200.0, "leads": 40, "ftds": 0}}
    o2 = one(E.evaluate(v, evid("ad:7", compare=nobase, obj=info("ad", "7")), NOW))
    assert o2["verdict"] == E.BLOCKED and E.INSUFFICIENT_EVIDENCE in o2["reasons"]  # база < 5 FTD
    assert one(E.evaluate(v, evid("ad:7", obj=info("ad", "7")), NOW))["verdict"] == E.BLOCKED  # без сравнения


def test_s5_and_enable_are_unsupported():
    v = model.make_version(name="Pacing", strategy=5)
    v["version"] = 1
    res = E.evaluate(v, evid(), NOW)
    assert res["unsupported"] and res["verdict"] == E.BLOCKED
    assert one(res)["reasons"] == [E.UNSUPPORTED_ACTION] and res["proposals"] == []
    empty = E.evaluate(v, {"objects": {}, "accounts": {}}, NOW)
    assert empty["verdict"] == E.BLOCKED and empty["reasons"] == [E.UNSUPPORTED_ACTION]
    v2 = stop_loss()
    v2["action"] = {"kind": "enable"}
    assert E.evaluate(v2, evid(), NOW)["unsupported"]


# ───────────────────────── арбитр ─────────────────────────

def item(rule_id, version, key, kind, *, strategy, priority=50, lineage=None, mode="suggest", fp="f"):
    action = {"kind": kind, "target": key, "account": "Acc"}
    obj = {"key": key, "account": "Acc", "verdict": "matched", "action": action,
           "lineage": lineage or [key], "fingerprint": fp}
    return {"rule_id": rule_id, "version": version, "strategy": strategy, "priority": priority, "mode": mode,
            "result": {"strategy": strategy, "objects": [obj]}}


def statuses(out):
    return {(p["rule_id"], p["key"], p["kind"]): p["status"] for p in out["proposals"]}


def test_pause_suppresses_scale_on_the_same_object_and_on_children_and_parents():
    # пауза кампании: гасит scale той же кампании и scale ad set под ней (со своим бюджетом)
    camp_lineage = ["campaign:1", "adset:11", "ad:111"]
    same = arbiter.arbitrate([
        item("r_stop", 1, "campaign:1", "pause", strategy=1, lineage=camp_lineage),
        item("r_scale", 1, "campaign:1", "budget_up", strategy=3, lineage=camp_lineage)])
    assert statuses(same)[("r_scale", "campaign:1", "budget_up")] == arbiter.SUPPRESSED
    assert same["accepted"][0]["kind"] == "pause"
    child = arbiter.arbitrate([
        item("r_stop", 1, "campaign:1", "pause", strategy=1, lineage=camp_lineage),
        item("r_scale", 1, "adset:11", "budget_up", strategy=3, lineage=["adset:11", "campaign:1", "ad:111"])])
    assert statuses(child)[("r_scale", "adset:11", "budget_up")] == arbiter.SUPPRESSED
    # пауза дочернего ad set гасит scale кампании-владельца бюджета (дизайн 2.5)
    parent = arbiter.arbitrate([
        item("r_stop", 1, "adset:11", "pause", strategy=1, lineage=["adset:11", "campaign:1"]),
        item("r_scale", 1, "campaign:1", "budget_up", strategy=3, lineage=["campaign:1", "adset:11"])])
    assert statuses(parent)[("r_scale", "campaign:1", "budget_up")] == arbiter.SUPPRESSED
    # чужая кампания не страдает
    other = arbiter.arbitrate([
        item("r_stop", 1, "adset:11", "pause", strategy=1, lineage=["adset:11", "campaign:1"]),
        item("r_scale", 1, "campaign:2", "budget_up", strategy=3, lineage=["campaign:2", "adset:22"])])
    assert statuses(other)[("r_scale", "campaign:2", "budget_up")] == arbiter.PROPOSED


def test_quality_block_also_suppresses_scale():
    out = arbiter.arbitrate([
        item("r_q", 1, "campaign:1", "block_scale", strategy=2),
        item("r_scale", 1, "campaign:1", "budget_up", strategy=3)])
    assert statuses(out)[("r_scale", "campaign:1", "budget_up")] == arbiter.SUPPRESSED


def test_arbiter_order_is_deterministic_and_independent_of_input_order():
    items = [
        item("r_b", 1, "adset:11", "alert", strategy=4, priority=40),
        item("r_scale", 2, "campaign:1", "budget_up", strategy=3, priority=70),
        item("r_stop", 1, "adset:12", "pause", strategy=1, priority=10),
        item("r_q", 1, "adset:13", "block_scale", strategy=2, priority=20),
        item("r_c", 1, "adset:14", "alert", strategy=4, priority=40)]
    base = arbiter.arbitrate(items)
    order = [(p["rule_id"], p["key"]) for p in base["proposals"]]
    assert [r for r, _ in order] == ["r_stop", "r_q", "r_b", "r_c", "r_scale"]
    for shuffled in (items[::-1], items[2:] + items[:2], sorted(items, key=lambda i: i["rule_id"])):
        assert [(p["rule_id"], p["key"]) for p in arbiter.arbitrate(copy.deepcopy(shuffled))["proposals"]] == order


def test_duplicates_are_removed_and_one_mutation_per_object():
    dup = arbiter.arbitrate([
        item("r_a", 1, "adset:11", "pause", strategy=1, priority=10),
        item("r_b", 1, "adset:11", "pause", strategy=1, priority=20)])
    s = statuses(dup)
    assert s[("r_a", "adset:11", "pause")] == arbiter.PROPOSED and s[("r_b", "adset:11", "pause")] == arbiter.DUPLICATE
    assert len(dup["accepted"]) == 1 and len(dup["duplicates"]) == 1
    # тот же объект и вид действия от двух правил - дубль; одна мутация на объект
    two = arbiter.arbitrate([
        item("r_a", 1, "campaign:1", "budget_up", strategy=3, priority=60),
        item("r_b", 1, "campaign:1", "budget_up", strategy=3, priority=70, fp="g")])
    assert len(two["accepted"]) == 1
    # алерт не мутация: два разных алерта на объект остаются
    alerts = arbiter.arbitrate([
        item("r_a", 1, "adset:11", "alert", strategy=4), item("r_b", 1, "adset:11", "block_scale", strategy=2)])
    assert len(alerts["accepted"]) == 2


def test_arbiter_ignores_unmatched_and_blocked_objects():
    it = item("r_a", 1, "adset:11", "pause", strategy=1)
    it["result"]["objects"][0]["verdict"] = "blocked"
    assert arbiter.arbitrate([it])["proposals"] == []


# ───────────────────────── сквозной сценарий: gather + движок ─────────────────────────

def _obj(i, name, **kw):
    return {"id": str(i), "name": name, "status": "ACTIVE", "effective_status": "ACTIVE", **kw}


def make_snapshot(campaigns, account="Acc", **over) -> ci.Snapshot:
    snap = ci.Snapshot(account=account, state="ok", currency="USD", date_from="2026-10-01", date_to="2026-10-11",
                       finished_at="2026-10-12 11:00:00", finished_at_utc=(NOW - timedelta(hours=1)).isoformat())
    snap.campaigns, _ = ci.parse_inventory([_obj(c[0], c[1], daily_budget=10000) for c in campaigns], "campaign")
    snap.adsets, _ = ci.parse_inventory(
        [_obj(a[0], a[1], campaign_id=str(c[0])) for c in campaigns for a in c[2]], "adset")
    snap.steps = {s: "ok" for s in ci.STEPS}
    for key, value in over.items():
        setattr(snap, key, value)
    return snap


def book(rows):
    return pd.DataFrame(rows, columns=["Date", "Country Final", "Account Alias", "Adset Name", "Ad Title",
                                       "# Leads", "# FTDs", "$ Spend"])


def days(name, spend, ftds, first="2026-10-06", last="2026-10-11", leads=5, alias="Acc"):
    d = date.fromisoformat(first)
    out = []
    while d <= date.fromisoformat(last):
        out.append((d.isoformat(), "BR", alias, name, "Ad", leads, ftds, spend))
        d += timedelta(days=1)
    return out


SET_BURN = "BR_1121_05.10 burn"      # дата в имени: возраст 8 дней
SET_OK = "BR_1121_05.10 fine"
DF = book(days(SET_BURN, 100.0, 0) + days(SET_OK, 100.0, 2))
CAMPS = [(1, "Camp1", [(11, SET_BURN), (12, SET_OK)])]


def make_sources(*, df=DF, snap=None, calls=None, goals=None, current_names=None):
    calls = calls if calls is not None else []
    snap = snap if snap is not None else make_snapshot(CAMPS)

    def workbook():
        calls.append("workbook")
        return df, BUILT, ("book.xlsx", 1.0)

    return gather.Sources(
        workbook=workbook, snapshot=lambda acct: snap if acct == "Acc" else None,
        aliases=lambda acct, country: {"BR": "Acc"},
        goals=lambda cm, countries: goals if goals is not None else [{"country": "Brazil", "code": "BR", "cpa": 100.0}],
        current_names=(lambda acct: current_names or {}))


def make_rule(st, *, mode="suggest", objects=None, accounts=("Acc",), preset="stop_loss_new_tests", **over):
    objects = objects if objects is not None else [{"account": "Acc", "level": "adset", "id": "11"}]
    rule = st.create_from_preset(preset, "Dan", by="t", scope={"accounts": list(accounts), "objects": objects,
                                                             "country": "BR", "asset": ""}, **over)
    return st.set_mode(rule["id"], mode, by="t")


@pytest.fixture
def st(tmp_path):
    return RulesStore(tmp_path)


def make_engine(st, **kw):
    return engine_mod.RulesEngine(st, make_sources(**kw), now=lambda: NOW)


def test_end_to_end_suggest_creates_one_proposal_and_an_evaluation_record(st):
    rule = make_rule(st)
    out = make_engine(st).run_job({"trigger": "manual", "scope": ""})
    assert len(out["suggestions"]) == 1
    sug = out["suggestions"][0]
    assert sug["state"] == "proposed" and sug["kind"] == "pause" and sug["id"] == "11"
    assert sug["rule_id"] == rule["id"] and sug["requested"] == {"status": "PAUSED"} and sug["dedup_key"]
    recs = st.evaluations()
    assert len(recs) == 1 and recs[0]["verdict"] == "matched" and recs[0]["trigger"] == "manual"
    assert recs[0]["bi_through"] == "2026-10-11" and recs[0]["T"] == {"value": 100.0, "source": "goal"}
    obj = recs[0]["objects"][0]
    assert obj["key"] == "adset:11" and obj["identity"] == "exact" and obj["arbiter"]["status"] == "proposed"
    assert obj["observed"]["ftd"] == 0 and obj["observed"]["spend"] == 400.0


def test_reevaluating_the_same_workbook_makes_no_duplicate_suggestions(st):
    make_rule(st)
    eng = make_engine(st)
    first = eng.run_job({"trigger": "workbook", "scope": "k"})
    second = eng.run_job({"trigger": "workbook", "scope": "k"})
    third = make_engine(st).run_job({"trigger": "manual", "scope": ""})  # даже новый движок на тех же данных
    assert len(first["suggestions"]) == 1 and second["suggestions"] == [] and third["suggestions"] == []
    assert len([a for a in st.actions() if a.get("state") == "proposed"]) == 1
    assert len(st.evaluations()) == 3  # каждая оценка в журнале есть, предложение одно


def test_changed_evidence_makes_a_new_suggestion(st):
    make_rule(st)
    make_engine(st).run_job({"trigger": "manual", "scope": ""})
    more = book(days(SET_BURN, 120.0, 0) + days(SET_OK, 100.0, 2))
    out = make_engine(st, df=more).run_job({"trigger": "manual", "scope": ""})
    assert len(out["suggestions"]) == 1  # другие цифры - другой отпечаток


def test_observe_mode_evaluates_but_proposes_nothing(st):
    make_rule(st, mode="observe")
    out = make_engine(st).run_job({"trigger": "manual", "scope": ""})
    assert out["suggestions"] == [] and st.actions() == []
    assert st.evaluations()[0]["verdict"] == "matched"


def test_disabled_rules_are_not_evaluated(st):
    make_rule(st, mode="disabled")
    assert make_engine(st).run_job({"trigger": "manual", "scope": ""})["evaluations"] == []


def test_proposal_does_not_start_a_cooldown_but_an_executed_action_does(st):
    make_rule(st)
    make_engine(st).run_job({"trigger": "manual", "scope": ""})
    assert gather.executed_actions(st.actions()) == {}
    st.append_action({"id": "11", "state": "verified", "kind": "pause"})
    assert gather.executed_actions(st.actions()) == {"11": st.actions()[-1]["at"]}


def test_not_in_workbook_object_is_blocked_end_to_end(st):
    make_rule(st, objects=[{"account": "Acc", "level": "adset", "id": "11"},
                           {"account": "Acc", "level": "adset", "id": "999"}])
    out = make_engine(st).run_job({"trigger": "manual", "scope": ""})
    rec = {o["key"]: o for o in st.evaluations()[0]["objects"]}
    assert rec["adset:999"]["verdict"] == "blocked" and rec["adset:999"]["reasons"] == [E.MISSING_MAPPING]
    assert rec["adset:11"]["verdict"] == "matched"
    assert [s["id"] for s in out["suggestions"]] == ["11"]


def test_snapshot_object_without_workbook_rows_is_missing_mapping_never_zero(st):
    snap = make_snapshot([(1, "Camp1", [(11, "BR_1121_05.10 no rows")])])
    make_rule(st)
    out = make_engine(st, snap=snap).run_job({"trigger": "manual", "scope": ""})
    o = st.evaluations()[0]["objects"][0]
    assert o["verdict"] == "blocked" and o["reasons"] == [E.MISSING_MAPPING] and out["suggestions"] == []


def test_renamed_object_is_blocked_because_the_names_are_wired(st):
    """`assigned_names` из scope и `current_names` из источника доходят до T2 и дают `renamed`."""
    objects = [{"account": "Acc", "level": "adset", "id": "11", "name_at_assign": "BR_1121_05.10 old name"}]
    make_rule(st, objects=objects)
    out = make_engine(st).run_job({"trigger": "manual", "scope": ""})
    o = st.evaluations()[0]["objects"][0]
    assert o["identity"] == ev.RENAMED and o["verdict"] == "blocked" and o["reasons"] == [E.AMBIGUOUS_MAPPING]
    assert out["suggestions"] == []
    # то же от имени из повторного чтения
    st2 = RulesStore(st.directory / "second")
    make_rule(st2)
    make_engine(st2, current_names={"adset:11": "BR_1121_05.10 someone renamed"}).run_job(
        {"trigger": "manual", "scope": ""})
    assert st2.evaluations()[0]["objects"][0]["identity"] == ev.RENAMED


def test_no_goal_means_no_target_end_to_end(st):
    make_rule(st)
    make_engine(st, goals=[]).run_job({"trigger": "manual", "scope": ""})
    o = st.evaluations()[0]["objects"][0]
    assert o["verdict"] == "blocked" and E.NO_TARGET in o["reasons"]


# Кампания с датой в имени (возраст известен) и двумя ad set: burn (40/день, 0 FTD) и fine (20/день, 5 FTD)
LCAMP = "BR_1121_01.10 camp"
LBURN, LFINE = "BR_1121_01.10 burn", "BR_1121_01.10 fine"
LDF = book(days(LBURN, 40.0, 0, first="2026-10-01") + days(LFINE, 20.0, 5, first="2026-10-01"))
LSNAP = [(1, LCAMP, [(11, LBURN), (12, LFINE)])]


def two_rules(st):
    pause_rule = make_rule(st)  # S1: пауза ad set 11
    scale_rule = make_rule(st, preset="scale_proven", objects=[{"account": "Acc", "level": "campaign", "id": "1"}])
    return pause_rule, scale_rule


def run_lineage(st, **kw):
    eng = engine_mod.RulesEngine(st, make_sources(df=LDF, snap=make_snapshot(LSNAP), **kw), now=lambda: NOW)
    out = eng.run_job({"trigger": "manual", "scope": ""})
    return out, {e["rule_id"]: {o["key"]: o for o in e["objects"]} for e in out["evaluations"]}


def test_pause_beats_scale_in_one_run(st):
    pause_rule, scale_rule = two_rules(st)
    out, objs = run_lineage(st)
    assert objs[pause_rule["id"]]["adset:11"]["verdict"] == "matched"
    camp = objs[scale_rule["id"]]["campaign:1"]
    assert camp["verdict"] == "matched" and camp["action"]["kind"] == "budget_up"   # без паузы scale прошёл бы
    assert camp["arbiter"]["status"] == arbiter.SUPPRESSED
    kinds = {(s["id"], s["kind"]) for s in out["suggestions"]}
    assert kinds == {("11", "pause")}


def test_blocked_pause_by_manual_hold_still_kills_the_scale(st):
    pause_rule, scale_rule = two_rules(st)
    st.add_hold("11", "buyer is testing it by hand")
    out, objs = run_lineage(st)
    burn = objs[pause_rule["id"]]["adset:11"]
    assert burn["verdict"] == "blocked" and burn["reasons"] == [E.MANUAL_HOLD] and burn["conditions_matched"] is True
    assert burn["action"] is None
    camp = objs[scale_rule["id"]]["campaign:1"]
    assert camp["arbiter"]["status"] == arbiter.SUPPRESSED
    assert out["suggestions"] == [] and out["arbitration"]["accepted"] == []   # ни паузы, ни scale
    assert out["arbitration"]["blocked_killers"][0]["key"] == "adset:11"


def test_blocked_pause_by_cooldown_still_kills_the_scale(st):
    pause_rule, scale_rule = two_rules(st)
    st.append_action({"id": "11", "state": "verified", "kind": "pause", "at": (NOW - timedelta(hours=10)).isoformat()})
    out, objs = run_lineage(st)
    burn = objs[pause_rule["id"]]["adset:11"]
    assert burn["verdict"] == "blocked" and E.COOLDOWN in burn["reasons"]
    assert objs[scale_rule["id"]]["campaign:1"]["arbiter"]["status"] == arbiter.SUPPRESSED
    assert out["suggestions"] == []


def test_the_lineage_wiring_in_gather_is_what_suppresses(st):
    """Если gather не заполняет предков/потомков, scale уцелеет - а тест должен это ловить."""
    _pause, scale_rule = two_rules(st)
    _out, objs = run_lineage(st)
    info = gather._object_info(make_snapshot(LSNAP), "adset", "11", "", date(2026, 10, 12))  # noqa: SLF001
    assert info["ancestors"] == ["campaign:1"]
    info = gather._object_info(make_snapshot(LSNAP), "campaign", "1", "", date(2026, 10, 12))  # noqa: SLF001
    assert info["descendants"] == ["adset:11", "adset:12"]


def test_blocked_pause_kills_scale_unit_and_never_becomes_a_proposal():
    pause = item("r_stop", 1, "adset:11", "pause", strategy=1, lineage=["adset:11", "campaign:1"])
    obj = pause["result"]["objects"][0]
    obj.update(verdict="blocked", action=None, conditions_matched=True, reasons=["Manual hold"],
               blocked_action={"kind": "pause"})
    scale_it = item("r_scale", 1, "campaign:1", "budget_up", strategy=3, lineage=["campaign:1", "adset:11"])
    out = arbiter.arbitrate([pause, scale_it])
    assert statuses(out) == {("r_scale", "campaign:1", "budget_up"): arbiter.SUPPRESSED}
    assert out["accepted"] == []
    # блок без совпавших условий гасить не должен
    obj["conditions_matched"] = False
    assert statuses(arbiter.arbitrate([pause, scale_it]))[("r_scale", "campaign:1", "budget_up")] == arbiter.PROPOSED


def test_s1_alert_also_suppresses_scale_on_object_children_and_ancestors():
    alert = item("r_stop", 1, "adset:11", "alert", strategy=1, lineage=["adset:11", "campaign:1"])
    scale_it = item("r_scale", 1, "campaign:1", "budget_up", strategy=3, lineage=["campaign:1", "adset:11"])
    assert statuses(arbiter.arbitrate([alert, scale_it]))[("r_scale", "campaign:1", "budget_up")] == arbiter.SUPPRESSED
    # алерт другой стратегии (S4) scale не гасит
    drift = item("r_d", 1, "adset:11", "alert", strategy=4, lineage=["adset:11", "campaign:1"])
    assert statuses(arbiter.arbitrate([drift, scale_it]))[("r_scale", "campaign:1", "budget_up")] == arbiter.PROPOSED


@pytest.mark.parametrize("verdict,reason", [(ev.COPY_SUFFIX, E.AMBIGUOUS_MAPPING), (ev.ALIAS_HEURISTIC, E.AMBIGUOUS_MAPPING)])
def test_automatic_blocks_what_evidence_does_not_allow(verdict, reason):
    e = evid(verdict=verdict, auto={"allowed": False, "reasons": [reason]})
    soft = one(E.evaluate(stop_loss(), e, NOW, mode="suggest"))
    assert soft["verdict"] == E.MATCHED and not soft["automatic_eligible"]
    hard = one(E.evaluate(stop_loss(), e, NOW, mode="automatic"))
    assert hard["verdict"] == E.BLOCKED and hard["reasons"] == [reason] and hard["action"] is None
    assert hard["blocked_action"]["kind"] == "pause"
    # без причин от evidence всё равно блок
    bare = one(E.evaluate(stop_loss(), evid(auto={"allowed": False, "reasons": []}), NOW, mode="automatic"))
    assert bare["verdict"] == E.BLOCKED and bare["reasons"] == [E.AMBIGUOUS_MAPPING]
    ok = one(E.evaluate(stop_loss(), evid(), NOW, mode="automatic"))
    assert ok["verdict"] == E.MATCHED and ok["automatic_eligible"]


def test_proposed_rows_carry_automatic_eligibility_and_freshness(st):
    rule = make_rule(st, mode="suggest")
    out = make_engine(st).run_job({"trigger": "manual", "scope": ""})
    row = out["suggestions"][0]
    assert row["automatic_eligible"] is False and isinstance(row["freshness"], list)
    assert E.NOT_RECONCILED in row["freshness"]  # снимок не сверяется с окном
    assert rule["id"] == row["rule_id"]


def test_new_evidence_supersedes_older_pending_proposals(st):
    make_rule(st)
    make_engine(st).run_job({"trigger": "manual", "scope": ""})
    more = book(days(SET_BURN, 120.0, 0) + days(SET_OK, 100.0, 2))
    out = make_engine(st, df=more).run_job({"trigger": "manual", "scope": ""})
    new = out["suggestions"][0]
    latest = st.latest_actions()
    states = sorted(r["state"] for r in latest.values())
    assert states == ["proposed", "superseded"]
    old = next(r for r in latest.values() if r["state"] == "superseded")
    assert old["superseded_by"] == new["dedup_key"] and old["action_id"] != new["action_id"]
    assert len([a for a in st.actions()]) == 3   # старая строка осталась в журнале


def test_evaluate_requires_an_explicit_now():
    with pytest.raises(ValueError):
        E.evaluate(stop_loss(), evid(), None)


def test_swallowed_listener_errors_are_logged(monkeypatch, caplog):
    from src.web import cache

    monkeypatch.setattr(cache, "_BUILD_LISTENERS", [lambda k: 1 / 0])
    with caplog.at_level("ERROR"):
        cache._notify_new_workbook("k")  # noqa: SLF001
    assert "Workbook listener failed" in caplog.text


def test_gather_helpers():
    assert gather.age_days_from_name("BR_1121_05.10 x", date(2026, 10, 12)) == 8
    assert gather.age_days_from_name("BR_1121_31.12 x", date(2026, 1, 5)) == 6   # прошлый год
    assert gather.age_days_from_name("no date", date(2026, 10, 12)) is None
    assert gather.age_days_from_name("BR 40.13", date(2026, 10, 12)) is None


# ───────────────────────── триггеры, поток, журналы ─────────────────────────

def test_workbook_trigger_runs_in_the_worker_thread(st):
    make_rule(st)
    eng = make_engine(st)
    assert eng.enqueue("workbook", "k1")
    assert eng.wait_idle(10)
    assert len(st.evaluations()) == 1 and st.evaluations()[0]["trigger"] == "workbook"
    assert eng.status()["worker"]
    eng.stop()


def test_evaluate_now_and_snapshot_triggers(st):
    a = make_rule(st)
    other = make_rule(st, accounts=("Other",), objects=[{"account": "Other", "level": "adset", "id": "5"}])
    eng = make_engine(st)
    eng.enqueue("manual", a["id"])
    assert eng.wait_idle(10)
    assert [e["rule_id"] for e in st.evaluations()] == [a["id"]]
    eng.enqueue("snapshot", "Other")  # только правила, где аккаунт в scope
    assert eng.wait_idle(10)
    assert [e["rule_id"] for e in st.evaluations()][-1] == other["id"] and len(st.evaluations()) == 2
    eng.enqueue("snapshot", "Nobody")
    assert eng.wait_idle(10) and len(st.evaluations()) == 2
    eng.stop()


def test_pending_jobs_of_one_kind_collapse(st):
    make_rule(st)
    gate, entered = threading.Event(), threading.Event()
    sources = make_sources()
    inner = sources.workbook

    def blocking():
        entered.set()
        gate.wait(10)
        return inner()

    sources.workbook = blocking
    eng = engine_mod.RulesEngine(st, sources, now=lambda: NOW)
    assert eng.enqueue("workbook", "k1")
    assert entered.wait(10)               # первое задание уже исполняется
    assert eng.enqueue("workbook", "k2")  # второе встало в очередь
    assert not eng.enqueue("workbook", "k3") and not eng.enqueue("workbook", "k4")  # схлопнуты
    gate.set()
    assert eng.wait_idle(10)
    assert len(st.evaluations()) == 2
    eng.stop()


def test_unknown_trigger_is_refused(st):
    with pytest.raises(ValueError):
        make_engine(st).enqueue("timer")


def test_broken_gather_is_journaled_not_raised(st):
    make_rule(st)
    eng = make_engine(st, df=None)
    out = eng.run_job({"trigger": "manual", "scope": ""})
    rec = st.evaluations()[0]
    assert rec["verdict"] == "blocked" and "workbook" in rec["error"] and out["suggestions"] == []


def test_cache_listener_fires_only_for_a_new_workbook(monkeypatch):
    from src.web import cache

    seen = []
    monkeypatch.setattr(cache, "_BUILD_LISTENERS", [seen.append])
    monkeypatch.setattr(cache, "_CACHE", {"key": None, "reports": {}, "df": None, "warnings": [], "built_at": None,
                                          "excel_key": None, "excel_df": None, "entry": None})
    monkeypatch.setattr(cache, "load_morning_report", lambda enriched_df=None, my_cm=None: ({}, pd.DataFrame(), []))
    cache._build_locked(("book.xlsx", 1.0), "Dan")  # noqa: SLF001
    cache._build_locked(("book.xlsx", 1.0), "Eve")  # noqa: SLF001 - тот же ключ: другой менеджер
    assert seen == [("book.xlsx", 1.0)]
    cache._build_locked(("book.xlsx", 2.0), "Dan")  # noqa: SLF001
    assert seen == [("book.xlsx", 1.0), ("book.xlsx", 2.0)]
    monkeypatch.setattr(cache, "_BUILD_LISTENERS", [lambda k: 1 / 0])
    cache._build_locked(("book.xlsx", 3.0), "Dan")  # сбой слушателя не ломает сборку  # noqa: SLF001


def test_read_campaigns_listener_fires_after_a_successful_read_only(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(ci, "_RESULT_LISTENERS", [seen.append])
    monkeypatch.setattr(ci, "_store_path", lambda: tmp_path / "inv.json")
    ci.put_result(make_snapshot(CAMPS))
    assert [s.account for s in seen] == ["Acc"]
    ci.put_result(ci.Snapshot(account="Acc", state="failed", error="x"))
    assert len(seen) == 1


class FakeEngine:
    def __init__(self):
        self.calls = []

    def enqueue(self, trigger, scope=None):
        self.calls.append((trigger, scope))
        return True

    def status(self):
        return {"queued": 1, "busy": False, "worker": True}


def test_runtime_install_wires_both_triggers_to_the_queue(monkeypatch):
    from src.web import cache, rules_runtime

    fake = FakeEngine()
    monkeypatch.setattr(rules_runtime, "_ENGINE", fake)
    monkeypatch.setattr(rules_runtime, "_INSTALLED", False)
    monkeypatch.setattr(cache, "_BUILD_LISTENERS", [])
    monkeypatch.setattr(ci, "_RESULT_LISTENERS", [])
    rules_runtime.install()
    rules_runtime.install()  # повтор безвреден
    cache._notify_new_workbook(("b.xlsx", 5.0))  # noqa: SLF001
    ci._notify_result(make_snapshot(CAMPS))  # noqa: SLF001
    assert fake.calls == [("workbook", "('b.xlsx', 5.0)"), ("snapshot", "Acc")]


# ───────────────────────── API ─────────────────────────

def test_api_is_guarded_and_only_queues(monkeypatch, st):
    from src.web import campaign_commands, rules_api

    fake = FakeEngine()
    monkeypatch.setattr(campaign_commands, "api_enabled", lambda: False)
    assert rules_api.evaluate_now({}, engine=fake)[1] == 409 and rules_api.list_evaluations("", store=st)[1] == 409
    monkeypatch.setattr(campaign_commands, "api_enabled", lambda: True)
    monkeypatch.setattr(campaign_commands, "bound_to_loopback", lambda: False)
    assert rules_api.evaluate_now({}, engine=fake)[1] == 403 and rules_api.list_evaluations("", store=st)[1] == 403
    assert fake.calls == []
    monkeypatch.setattr(campaign_commands, "bound_to_loopback", lambda: True)
    data, code = rules_api.evaluate_now({"rule_id": "rule_x"}, engine=fake)
    assert code == 202 and data["queued"] is True and fake.calls == [("manual", "rule_x")]
    assert rules_api.evaluate_now({}, engine=fake)[0]["rule_id"] == "" and fake.calls[-1] == ("manual", None)
    make_rule(st)
    make_engine(st).run_job({"trigger": "manual", "scope": ""})
    rows, code = rules_api.list_evaluations(st.evaluations()[0]["rule_id"], store=st)
    assert code == 200 and len(rows["evaluations"]) == 1
    assert rules_api.list_evaluations("rule_nothing", store=st)[0]["evaluations"] == []
    assert len(rules_api.list_evaluations("", "1", store=st)[0]["evaluations"]) == 1


def test_routes_are_registered():
    from src.web import app as web

    paths = {(r.path, m) for r in web.app.routes if hasattr(r, "methods") for m in r.methods}
    assert ("/rules/evaluations", "GET") in paths
    assert ("/rules/evaluate-now", "POST") in paths


# ───────────────────────── никаких сессий и таймеров ─────────────────────────

def test_evaluation_never_opens_a_session(monkeypatch, st):
    """Observe и Suggest не открывают профиль: любая попытка открыть сессию роняет тест."""
    from src.analysis_agent import account_spend_api
    from src.web import campaign_commands, rules_api

    opened = []

    def forbidden(*a, **k):
        opened.append((a, k))
        raise AssertionError("a rules evaluation tried to open a session")

    monkeypatch.setattr(account_spend_api, "_session", forbidden)
    monkeypatch.setattr(account_spend_api, "_open_gated", forbidden)
    monkeypatch.setattr(campaign_commands, "open_session", forbidden)
    monkeypatch.setattr(ci, "read_via_api", forbidden)
    monkeypatch.setattr(ci, "start_read", forbidden)
    monkeypatch.setattr(campaign_commands, "api_enabled", lambda: True)
    monkeypatch.setattr(campaign_commands, "bound_to_loopback", lambda: True)
    make_rule(st, mode="suggest")
    make_rule(st, mode="observe", preset="scale_proven", objects=[{"account": "Acc", "level": "campaign", "id": "1"}])
    eng = make_engine(st)
    for trig in (("workbook", "k"), ("manual", ""), ("snapshot", "Acc")):
        eng.enqueue(*trig)
        assert eng.wait_idle(10)
    rules_api.evaluate_now({}, engine=eng)
    assert eng.wait_idle(10)
    rules_api.list_evaluations("", store=st)
    eng.stop()
    assert opened == []
    assert len(st.evaluations()) >= 8


RULES_CODE = [ROOT / "src" / "analysis_agent" / "rules", ROOT / "src" / "web" / "rules_runtime.py",
              ROOT / "src" / "web" / "rules_api.py"]
BANNED_TIMERS = re.compile(r"setInterval|setTimeout|threading\.Timer|\bTimer\(|schedule|apscheduler|cron|"
                           r"call_later|call_at|\bsched\b|time\.sleep|\bsleep\(|while\s+True|asyncio\.sleep", re.I)
BANNED_SESSIONS = re.compile(r"open_session|\b_session\b|_open_gated|account_spend_api|start_read|read_via_api|"
                             r"fb_api|graph_client|Multilogin|mlx", re.I)


def _code_files():
    for base in RULES_CODE:
        yield from ([base] if base.is_file() else sorted(base.glob("*.py")))


def test_no_timers_or_polling_in_the_rules_code():
    bad = []
    for path in _code_files():
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            # замок файла журнала (store.py) повторяет попытку через 20 мс - это ожидание замка,
            # а не таймер: цикл захвата и его пауза допущены поимённо
            if path.name == "store.py" and line.strip() in ("while True:", "time.sleep(0.02)"):
                continue
            if BANNED_TIMERS.search(line):
                bad.append(f"{path.name}:{n}: {line.strip()}")
    assert bad == []


def test_the_rules_code_never_references_opening_a_session():
    bad = []
    for path in _code_files():
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if BANNED_SESSIONS.search(line):
                bad.append(f"{path.name}:{n}: {line.strip()}")
    assert bad == []


def test_the_greps_would_catch_a_timer_or_a_session():
    assert BANNED_TIMERS.search("setInterval(tick, 1000)") and BANNED_TIMERS.search("threading.Timer(5, f)")
    assert BANNED_TIMERS.search("time.sleep(1)") and BANNED_TIMERS.search("# schedule it")
    assert not BANNED_TIMERS.search("for entry in iter(self._queue.get, _STOP):")
    assert BANNED_SESSIONS.search("with _api._session(account) as s") and BANNED_SESSIONS.search("open_session(a)")
