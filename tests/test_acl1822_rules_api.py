"""ACL-1822: API страницы Rules (список, сохранение, режимы, Dismiss, результат Apply, ячейки).

Всё на временном каталоге правил и подменах: Meta, Multilogin и сеть не трогаются.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.analysis_agent import campaign_inventory as ci  # noqa: E402
from src.analysis_agent.rules.store import RulesStore  # noqa: E402
from src.web import campaign_commands, rules_api  # noqa: E402

OWNER = "Buyer One"
NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
ADSET = "120000000000001"
CAMP = "120000000000002"


@pytest.fixture(autouse=True)
def api_mode(monkeypatch):
    monkeypatch.setattr(campaign_commands, "api_enabled", lambda: True)
    monkeypatch.setattr(campaign_commands, "bound_to_loopback", lambda: True)


@pytest.fixture
def st(tmp_path):
    return RulesStore(tmp_path)


def make_rule(st, preset="stop_loss_new_tests", mode="suggest", objects=None):
    rule = st.create_from_preset(preset, OWNER, by=OWNER)
    objs = objects if objects is not None else [
        {"account": "Acc", "level": "adset", "id": ADSET, "name_at_assign": "MX_1_test 10.10"}]
    st.edit_rule(rule["id"], {"scope": {"accounts": ["Acc"], "objects": objs, "country": "Mexico"}}, by=OWNER)
    st.set_mode(rule["id"], mode, by=OWNER)
    return rule["id"]


def evaluation(st, rule_id, verdict="matched", **obj):
    rule = st.get_rule(rule_id)
    version = rule["current_version"]
    base = {"key": f"adset:{ADSET}", "account": "Acc", "level": "adset", "id": ADSET, "name": "MX_1_test 10.10",
            "identity": "exact", "verdict": verdict, "reasons": [], "notes": [], "conditions_matched": True,
            "observed": {"spend": 620.0, "ftd": 0, "target": 60.0},
            "action": {"kind": "pause", "level": "adset", "id": ADSET, "account": "Acc",
                       "requested": {"status": "PAUSED"}, "before": {"status": "ACTIVE"}}}
    base.update(obj)
    return st.append_evaluation({"trigger": "manual", "rule_id": rule_id, "version": version, "mode": rule["mode"],
                                 "strategy": 1, "verdict": verdict, "reasons": base["reasons"],
                                 "counts": {"matched": 1, "not_matched": 0, "blocked": 0},
                                 "bi_through": "2026-10-11", "bi_built_at": "2026-10-12T08:00:00+00:00",
                                 "snapshot_at": {"Acc": "2026-10-12T07:00:00+00:00"},
                                 "T": {"value": 60.0, "source": "goal"}, "objects": [base]})


def propose(st, rule_id, **extra):
    rule = st.get_rule(rule_id)
    return st.append_action({"state": "proposed", "rule_id": rule_id, "version": rule["current_version"],
                             "account": "Acc", "level": "adset", "id": ADSET, "kind": "pause",
                             "before": {"status": "ACTIVE"}, "requested": {"status": "PAUSED"}, **extra})


# ── охрана ──

def test_every_endpoint_refuses_under_rpa_and_off_loopback(monkeypatch, st):
    calls = [lambda: rules_api.overview(OWNER, store=st), lambda: rules_api.save_rule({"owner": OWNER}, store=st),
             lambda: rules_api.set_rule_mode({}, store=st), lambda: rules_api.duplicate_rule({}, store=st),
             lambda: rules_api.dismiss_proposal({}, store=st), lambda: rules_api.record_action_result({}, store=st),
             lambda: rules_api.scope_options("Acc"), lambda: rules_api.cells("Acc", store=st)]
    monkeypatch.setattr(campaign_commands, "api_enabled", lambda: False)
    assert [c()[1] for c in calls] == [409] * len(calls)
    assert calls[0]()[0]["code"] == "rpa_backend"
    monkeypatch.setattr(campaign_commands, "api_enabled", lambda: True)
    monkeypatch.setattr(campaign_commands, "bound_to_loopback", lambda: False)
    assert [c()[1] for c in calls] == [403] * len(calls)


# ── список ──

def test_overview_lists_rules_with_counts_health_and_templates(st):
    rid = make_rule(st)
    evaluation(st, rid)
    data, code = rules_api.overview(OWNER, store=st, now=NOW, engine_status=lambda: {"queued": 0})
    assert code == 200
    rule = data["rules"][0]
    assert rule["id"] == rid and rule["mode"] == "suggest" and rule["counts"]["adset"] == 1
    assert rule["health"]["state"] == "ok" and rule["last_eval"]["verdict"] == "matched"
    assert [t["id"] for t in data["templates"]] == ["stop_loss_new_tests", "scale_proven"]
    assert data["sources"]["bi_through"] == "2026-10-11" and data["sources"]["snapshot_accounts"] == 1
    assert data["engine"] == {"queued": 0} and "T5" in data["automatic_reason"]
    assert data["settings"]["automatic_enabled"] is False


def test_overview_is_per_owner_and_flags_never_evaluated_stale_and_disabled(st):
    make_rule(st)
    other = st.create_from_preset("scale_proven", "Someone Else", by="x")
    assert [r["owner"] for r in rules_api.overview(OWNER, store=st, now=NOW)[0]["rules"]] == [OWNER]
    assert len(rules_api.overview("", store=st, now=NOW)[0]["rules"]) == 2
    assert rules_api.overview(OWNER, store=st, now=NOW)[0]["rules"][0]["health"]["state"] == "never"
    rid = st.list_rules(OWNER)[0]["id"]
    evaluation(st, rid)
    late = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)   # книге больше 36 часов
    assert rules_api.overview(OWNER, store=st, now=late)[0]["rules"][0]["health"]["state"] == "stale"
    st.set_mode(rid, "disabled", by=OWNER)
    assert rules_api.overview(OWNER, store=st, now=NOW)[0]["rules"][0]["health"]["state"] == "disabled"
    assert other["id"] not in {r["id"] for r in rules_api.overview(OWNER, store=st)[0]["rules"]}


# ── сохранение и режимы ──

def test_create_from_template_starts_disabled_and_never_activates(st):
    data, code = rules_api.save_rule({"owner": OWNER, "preset": "scale_proven", "name": "My scale"}, store=st)
    assert code == 201
    rule = st.get_rule(data["rule_id"])
    assert rule["mode"] == "disabled" and rule["grant"] is None
    assert st.current_version(rule)["name"] == "My scale"
    assert rules_api.save_rule({"owner": OWNER, "preset": "nope"}, store=st)[1] == 400
    assert rules_api.save_rule({"preset": "scale_proven"}, store=st)[0]["code"] == "no_owner"


def test_automatic_is_refused_with_the_reason_everywhere(st):
    rid = make_rule(st)
    for fn, body in ((rules_api.save_rule, {"owner": OWNER, "rule_id": rid, "mode": "automatic"}),
                     (rules_api.set_rule_mode, {"owner": OWNER, "rule_id": rid, "mode": "automatic"})):
        data, code = fn(body, store=st)
        assert code == 409 and data["code"] == "automatic_unavailable"
        assert data["error"] == rules_api.AUTOMATIC_REASON
    assert st.get_rule(rid)["mode"] == "suggest" and st.get_rule(rid)["grant"] is None
    assert rules_api.set_rule_mode({"owner": OWNER, "rule_id": rid, "mode": "weird"}, store=st)[1] == 400


def test_save_edits_make_a_new_version_and_keep_unchanged_ones(st):
    rid = make_rule(st, mode="observe")
    version = st.get_rule(rid)["current_version"]
    body = {"owner": OWNER, "rule_id": rid, "expected_version": version, "mode": "suggest",
            "changes": {"caps": {"cooldown_hours": 12}, "strategy": 3, "priority": 1}}
    data, code = rules_api.save_rule(body, store=st)
    assert code == 200 and data["version"] == version + 1 and data["mode"] == "suggest"
    cur = st.current_version(st.get_rule(rid))
    assert cur["caps"]["cooldown_hours"] == 12
    assert cur["strategy"] == 1 and cur["priority"] == 10, "strategy and priority are not editable here"
    again, _ = rules_api.save_rule({**body, "expected_version": version + 1}, store=st)
    assert again["version"] == version + 1, "no change - no new version"


def test_save_reports_problems_conflicts_and_foreign_rules(st):
    rid = make_rule(st)
    version = st.get_rule(rid)["current_version"]
    data, code = rules_api.save_rule({"owner": OWNER, "rule_id": rid, "changes": {"caps": {"cooldown_hours": -1}}}, store=st)
    assert code == 422 and data["problems"]
    assert rules_api.save_rule({"owner": OWNER, "rule_id": rid, "expected_version": version + 5, "changes": {}}, store=st)[1] == 409
    assert rules_api.save_rule({"owner": "Mallory", "rule_id": rid, "changes": {}}, store=st)[1] == 403
    assert rules_api.save_rule({"owner": OWNER, "rule_id": "rule_missing", "changes": {}}, store=st)[1] == 404


def test_mode_toggle_and_duplicate(st):
    rid = make_rule(st)
    assert rules_api.set_rule_mode({"owner": OWNER, "rule_id": rid, "mode": "disabled"}, store=st)[0]["mode"] == "disabled"
    data, code = rules_api.duplicate_rule({"owner": OWNER, "rule_id": rid}, store=st)
    assert code == 201 and data["mode"] == "disabled" and data["rule_id"] != rid
    assert st.current_version(st.get_rule(data["rule_id"]))["name"].endswith("(copy)")
    assert rules_api.duplicate_rule({"owner": "Mallory", "rule_id": rid}, store=st)[1] == 403


# ── предложения ──

def test_dismiss_marks_the_proposal_and_nothing_else(st):
    rid = make_rule(st)
    act = propose(st, rid)
    data, code = rules_api.dismiss_proposal({"owner": OWNER, "action_id": act["action_id"]}, store=st)
    assert code == 200 and data["state"] == "dismissed"
    assert st.latest_actions()[act["action_id"]]["state"] == "dismissed"
    assert st.latest_actions()[act["action_id"]]["actor"] == "human:" + OWNER
    again = rules_api.dismiss_proposal({"owner": OWNER, "action_id": act["action_id"]}, store=st)
    assert again[1] == 409 and again[0]["code"] == "not_pending"
    assert rules_api.dismiss_proposal({"owner": "Mallory", "action_id": propose(st, rid)["action_id"]}, store=st)[1] == 403
    assert rules_api.dismiss_proposal({"owner": OWNER, "action_id": "act_nope"}, store=st)[1] == 404


def good_op(op_state="Succeeded", **over):
    """Снимок операции команд для предложения `propose(...)`: пауза ad set ADSET на счёте Acc."""
    op = {"state": op_state, "account": "Acc", "command": "set_status", "value": "PAUSED",
          "affected": [{"level": "adset", "id": ADSET, "field": "status", "after": "PAUSED",
                        "result": "saved" if op_state == "Succeeded" else "not_sent", "saved": "PAUSED"}]}
    op.update(over)
    return op


@pytest.mark.parametrize("op_state,expected", [("Succeeded", "verified"), ("Failed", "failed"),
                                               ("Outcome unknown", "unknown")])
def test_action_result_takes_the_state_from_the_command_server(st, op_state, expected):
    rid = make_rule(st)
    act = propose(st, rid)
    seen = []

    def status(op_id):
        seen.append(op_id)
        return good_op(op_state), 200

    data, code = rules_api.record_action_result({"owner": OWNER, "action_id": act["action_id"], "op_id": "op_1"},
                                                store=st, status=status)
    assert code == 200 and data["state"] == expected and seen == ["op_1"]
    last = st.latest_actions()[act["action_id"]]
    assert last["state"] == expected and last["op_id"] == "op_1"


def test_action_result_refuses_running_unknown_or_missing_op(st):
    rid = make_rule(st)
    act = propose(st, rid)
    body = {"owner": OWNER, "action_id": act["action_id"], "op_id": "op_1"}
    assert rules_api.record_action_result(body, store=st, status=lambda o: ({"state": "Running"}, 200))[1] == 409
    assert rules_api.record_action_result(body, store=st, status=lambda o: ({}, 404))[1] == 404
    assert rules_api.record_action_result({**body, "op_id": ""}, store=st, status=lambda o: ({}, 200))[1] == 400
    assert st.latest_actions()[act["action_id"]]["state"] == "proposed"


# ── объекты снимка ──

def test_scope_options_lists_only_objects_with_ids_and_says_when_unread(monkeypatch):
    snap = ci.Snapshot(account="Acc", state="ok", currency="USD", finished_at_utc="2026-10-12T07:00:00+00:00")
    snap.campaigns = [{"id": CAMP, "name": "Camp", "status": "ACTIVE", "effective_status": "ACTIVE"},
                      {"id": "", "name": "No id", "status": "ACTIVE"}]
    snap.adsets = [{"id": ADSET, "name": "Set", "campaign_id": CAMP, "status": "ACTIVE", "effective_status": "ACTIVE"}]
    data, code = rules_api.scope_options("Acc", snapshot=snap)
    assert code == 200 and data["has_snapshot"] and [c["id"] for c in data["campaigns"]] == [CAMP]
    assert data["campaigns"][0]["adsets"][0]["id"] == ADSET and data["currency"] == "USD"
    monkeypatch.setattr(ci, "get_snapshot", lambda a: None)
    none, _ = rules_api.scope_options("Other")
    assert none["has_snapshot"] is False and "Read campaigns first" in none["note"]
    assert rules_api.scope_options("")[1] == 400


# ── ячейки ──

def test_cells_show_count_mode_and_latest_result_by_object_key(st):
    rid = make_rule(st)
    evaluation(st, rid)
    propose(st, rid)
    other = make_rule(st, preset="scale_proven", mode="observe")
    data, code = rules_api.cells("Acc", OWNER, store=st)
    cell = data["cells"][f"adset:{ADSET}"]
    assert code == 200 and cell["count"] == 2 and cell["mode"] == "suggest"
    assert cell["result"] == "Pause suggested" and cell["tone"] == "act"
    names = {r["rule_id"]: r["result"] for r in cell["rules"]}
    assert names[other] == "Not evaluated yet" and names[rid] == "Pause suggested"
    assert rules_api.cells("Elsewhere", OWNER, store=st)[0]["cells"] == {}


def test_cells_report_blocked_hold_and_observe_results_and_ignore_older_versions(st):
    rid = make_rule(st, mode="observe")
    evaluation(st, rid)
    cell = rules_api.cells("Acc", OWNER, store=st)[0]["cells"][f"adset:{ADSET}"]
    assert cell["result"] == "Pause · would act"
    rid2 = make_rule(st, mode="observe")
    evaluation(st, rid2, verdict="blocked", reasons=["Ambiguous mapping"], action=None)
    results = {r["rule_id"]: r["result"] for r in rules_api.cells("Acc", OWNER, store=st)[0]["cells"][f"adset:{ADSET}"]["rules"]}
    assert results[rid2] == "Blocked · Ambiguous mapping"
    st.add_hold(ADSET, "manual")
    assert rules_api.cells("Acc", OWNER, store=st)[0]["cells"][f"adset:{ADSET}"]["result"] == "Held · manual"
    st.release_hold(ADSET, by=OWNER)
    st.edit_rule(rid, {"caps": {"cooldown_hours": 3}}, by=OWNER)   # новая версия: старый итог не показываем
    results = {r["rule_id"]: r["result"] for r in rules_api.cells("Acc", OWNER, store=st)[0]["cells"][f"adset:{ADSET}"]["rules"]}
    assert results[rid] == "Not evaluated yet"


@pytest.mark.parametrize("label,over", [
    ("another account", {"account": "Other"}),
    ("another command", {"command": "set_budget", "value": "100"}),
    ("another status", {"value": "ACTIVE"}),
    ("another object", {"affected": [{"level": "adset", "id": "999999999", "result": "saved"}]}),
    ("another level", {"affected": [{"level": "campaign", "id": ADSET, "result": "saved"}]}),
    ("extra object", {"affected": [{"level": "adset", "id": ADSET, "result": "saved"},
                                   {"level": "adset", "id": "999999999", "result": "saved"}]}),
    ("skipped object", {"affected": [{"level": "adset", "id": ADSET, "result": "not_sent"}]}),
    ("no objects", {"affected": []}),
])
def test_action_result_refuses_an_op_that_is_not_this_suggestion(st, label, over):
    rid = make_rule(st)
    act = propose(st, rid)
    data, code = rules_api.record_action_result({"owner": OWNER, "action_id": act["action_id"], "op_id": "op_x"},
                                                store=st, status=lambda o: (good_op(**over), 200))
    assert code == 409 and data["code"] == "op_mismatch", label
    assert st.latest_actions()[act["action_id"]]["state"] == "proposed", "the journal must not record it"


def test_action_result_binds_a_budget_suggestion_and_refuses_another_value(st):
    rid = make_rule(st, preset="scale_proven", objects=[{"account": "Acc", "level": "campaign", "id": CAMP,
                                                         "name_at_assign": "c"}])
    act = st.append_action({"state": "proposed", "rule_id": rid, "version": st.get_rule(rid)["current_version"],
                            "account": "Acc", "level": "campaign", "id": CAMP, "kind": "budget_up",
                            "before": {"daily_budget": 100}, "requested": {"daily_budget": 110}})

    def op(value):
        return {"state": "Succeeded", "account": "Acc", "command": "set_budget", "value": value,
                "affected": [{"level": "campaign", "id": CAMP, "result": "saved"}]}
    body = {"owner": OWNER, "action_id": act["action_id"], "op_id": "op_b"}
    assert rules_api.record_action_result(body, store=st, status=lambda o: (op("120"), 200))[1] == 409
    assert rules_api.record_action_result(body, store=st, status=lambda o: (op("110.00"), 200))[1] == 200
    assert st.latest_actions()[act["action_id"]]["state"] == "verified"


def test_action_result_refuses_an_op_id_already_used_by_another_suggestion(st):
    rid = make_rule(st)
    first, second = propose(st, rid), propose(st, rid)
    ok = lambda o: (good_op(), 200)
    assert rules_api.record_action_result({"owner": OWNER, "action_id": first["action_id"], "op_id": "op_1"},
                                          store=st, status=ok)[1] == 200
    data, code = rules_api.record_action_result({"owner": OWNER, "action_id": second["action_id"], "op_id": "op_1"},
                                                store=st, status=ok)
    assert code == 409 and data["code"] == "op_reused"
    assert st.latest_actions()[second["action_id"]]["state"] == "proposed"


def test_owner_is_required_by_mode_duplicate_cells_dismiss_and_result(st):
    rid = make_rule(st)
    act = propose(st, rid)
    assert rules_api.set_rule_mode({"rule_id": rid, "mode": "observe"}, store=st)[0]["code"] == "no_owner"
    assert rules_api.duplicate_rule({"rule_id": rid}, store=st)[0]["code"] == "no_owner"
    assert rules_api.cells("Acc", "", store=st)[0]["code"] == "no_owner"
    assert rules_api.dismiss_proposal({"action_id": act["action_id"]}, store=st)[0]["code"] == "no_owner"
    assert rules_api.record_action_result({"action_id": act["action_id"], "op_id": "o"}, store=st)[0]["code"] == "no_owner"
    assert st.get_rule(rid)["mode"] == "suggest"


def test_scope_object_ids_must_be_numeric(st):
    rid = make_rule(st)
    bad = {"accounts": ["Acc"], "country": "Mexico",
           "objects": [{"account": "Acc", "level": "adset", "id": "12x45", "name_at_assign": "n"}]}
    data, code = rules_api.save_rule({"owner": OWNER, "rule_id": rid, "changes": {"scope": bad}}, store=st)
    assert code == 422 and data["code"] == "bad_object_id"
