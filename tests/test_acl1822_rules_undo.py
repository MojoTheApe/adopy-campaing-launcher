"""Undo restores the actual confirmed command through another human command.

Only temporary journals and saved fake operations are used; no session or Meta call.
"""
from __future__ import annotations

import copy

import pytest

from src.web import campaign_commands, rules_api
from tests.test_acl1822_rules_api import ADSET, CAMP, OWNER, make_rule, propose


@pytest.fixture
def undo_env(tmp_path, monkeypatch):
    from src.analysis_agent.rules.store import RulesStore

    monkeypatch.setattr(campaign_commands, "api_enabled", lambda: True)
    monkeypatch.setattr(campaign_commands, "bound_to_loopback", lambda: True)
    monkeypatch.setattr(campaign_commands, "open_session", lambda *a, **k: pytest.fail("Undo API must not open a session"))
    store = RulesStore(tmp_path)
    rid = make_rule(store)
    action = propose(store, rid)
    # Deliberately stale evaluator 'before'; the actual confirmed operation wins.
    action = store.append_action({**action, "state": "verified", "op_id": "op_apply", "before": {"status": "PAUSED"}})
    ops = {"op_apply": {"state": "Succeeded", "account": "Acc", "command": "set_status", "value": "PAUSED",
                        "currency": "USD", "unit": 100,
                        "affected": [{"level": "adset", "id": ADSET, "field": "status", "before": "ACTIVE",
                                      "after": "PAUSED", "saved": "PAUSED", "result": "saved"}]}}
    ops["op_undo"] = {**copy.deepcopy(ops["op_apply"]), "value": "ACTIVE"}
    ops["op_undo"]["affected"][0].update(before="PAUSED", after="ACTIVE", saved="ACTIVE")
    monkeypatch.setattr(campaign_commands, "get_status", lambda op: (copy.deepcopy(ops[op]), 200) if op in ops else ({}, 404))
    return store, action, ops


def test_undo_request_uses_the_saved_command_before_not_stale_evaluation(undo_env):
    store, action, _ = undo_env
    before = store.actions()
    data, code = rules_api.prepare_undo({"action_id": action["action_id"], "owner": OWNER}, store=store)
    assert code == 200
    assert data["request"] == {"account": "Acc", "command": "set_status", "value": "ACTIVE",
                                "targets": [{"level": "adset", "id": ADSET}]}
    assert store.actions() == before, "request preparation must not change Meta or journal state"


@pytest.mark.parametrize("unit,before,value", [(100, 10025, "100.25"), (1, 10025, "10025")])
def test_undo_budget_preserves_minor_units(undo_env, unit, before, value):
    store, action, ops = undo_env
    store.append_action({**action, "kind": "budget_up", "level": "campaign", "id": CAMP,
                         "requested": {"daily_budget": 11000}, "before": {"daily_budget": 1}})
    ops["op_apply"].update(command="set_budget", value="11000", unit=unit,
                            currency="JPY" if unit == 1 else "USD",
                            affected=[{"level": "campaign", "id": CAMP, "field": "daily_budget", "before": before,
                                       "after": 11000 * unit, "saved": 11000 * unit, "result": "saved"}])
    data, code = rules_api.prepare_undo({"action_id": action["action_id"], "owner": OWNER}, store=store)
    assert code == 200 and data["request"]["value"] == value
    ops["op_undo"].update(command="set_budget", value=value, unit=unit,
                           currency=ops["op_apply"]["currency"],
                           affected=[{"level": "campaign", "id": CAMP, "field": "daily_budget", "before": 11000 * unit,
                                      "after": before, "saved": before, "result": "saved"}])
    data, code = rules_api.record_undo_result({"action_id": action["action_id"], "owner": OWNER, "op_id": "op_undo"}, store=store)
    assert code == 200 and data["undo_state"] == "verified"


@pytest.mark.parametrize("owner,state,undo_state", [("", "verified", ""), ("Other", "verified", ""),
                                                   (OWNER, "proposed", ""), (OWNER, "unknown", ""),
                                                   (OWNER, "undone", "verified"), (OWNER, "verified", "unknown")])
def test_undo_requires_owned_confirmed_action_and_no_uncertain_undo(undo_env, owner, state, undo_state):
    store, action, _ = undo_env
    store.append_action({**action, "state": state, "undo_state": undo_state})
    assert rules_api.prepare_undo({"action_id": action["action_id"], "owner": owner}, store=store)[1] in (400, 403, 409)


@pytest.mark.parametrize("change", [{"state": "Outcome unknown"}, {"account": "Other"}, {"affected": []}])
def test_undo_refuses_unverified_or_mismatched_original_operation(undo_env, change):
    store, action, ops = undo_env
    ops["op_apply"].update(change)
    assert rules_api.prepare_undo({"action_id": action["action_id"], "owner": OWNER}, store=store)[1] == 409


@pytest.mark.parametrize("state,result,expected", [("Succeeded", "saved", "verified"),
                                                  ("Failed", "not_sent", "failed"),
                                                  ("Outcome unknown", "unknown", "unknown")])
def test_undo_result_uses_command_server_and_keeps_both_operation_ids(undo_env, state, result, expected):
    store, action, ops = undo_env
    ops["op_undo"]["state"] = state
    ops["op_undo"]["affected"][0]["result"] = result
    data, code = rules_api.record_undo_result({"action_id": action["action_id"], "op_id": "op_undo",
                                               "owner": OWNER, "state": "Succeeded"}, store=store)
    assert code == 200 and data["undo_state"] == expected
    last = store.latest_actions()[action["action_id"]]
    assert last["state"] == ("undone" if expected == "verified" else "verified")
    assert last["op_id"] == "op_apply" and last["undo_op_id"] == "op_undo"
    assert last["actor"] == "human:" + OWNER


@pytest.mark.parametrize("change", [{"account": "Other"}, {"command": "set_budget"}, {"value": "PAUSED"},
                                     {"affected": []}, {"affected": [{"level": "campaign", "id": ADSET, "result": "saved"}]},
                                     {"affected": [{"level": "adset", "id": ADSET, "result": "not_sent"}]}])
def test_undo_does_not_accept_unrelated_or_unsaved_commands(undo_env, change):
    store, action, ops = undo_env
    ops["op_undo"].update(change)
    data, code = rules_api.record_undo_result({"action_id": action["action_id"], "op_id": "op_undo", "owner": OWNER}, store=store)
    assert code == 409 and data["code"] == "op_mismatch"
    assert "undo_op_id" not in store.latest_actions()[action["action_id"]]


def test_undo_refuses_original_or_reused_operation_and_preserves_guards(undo_env, monkeypatch):
    store, action, _ = undo_env
    body = {"action_id": action["action_id"], "op_id": "op_apply", "owner": OWNER}
    assert rules_api.record_undo_result(body, store=store)[1] == 409
    store.append_action({"state": "undone", "op_id": "op_other", "undo_op_id": "op_undo", "rule_id": action["rule_id"]})
    assert rules_api.record_undo_result({**body, "op_id": "op_undo"}, store=store)[0]["code"] == "op_reused"
    monkeypatch.setattr(campaign_commands, "bound_to_loopback", lambda: False)
    assert rules_api.prepare_undo(body, store=store)[1] == 403
    assert rules_api.record_undo_result(body, store=store)[1] == 403
    monkeypatch.setattr(campaign_commands, "api_enabled", lambda: False)
    assert rules_api.prepare_undo(body, store=store)[1] == 409


def test_applied_actions_remain_visible_in_rule_and_object_views(undo_env):
    store, action, _ = undo_env
    data, _ = rules_api.overview(OWNER, store=store)
    assert data["rules"][0]["recent_actions"][0]["action_id"] == action["action_id"]
    data, _ = rules_api.cells("Acc", OWNER, store=store)
    assert data["cells"]["adset:" + ADSET]["rules"][0]["applied"]["action_id"] == action["action_id"]
