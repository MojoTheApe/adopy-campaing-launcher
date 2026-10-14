"""Rules T1: модель, пресеты, цель T, версии, грант, журналы (ACL-1814)."""
from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest

from src.analysis_agent.rules import model, store as store_mod
from src.analysis_agent.rules.store import RulesStore

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def automatic_gate_on(monkeypatch):
    """Замок Automatic по умолчанию закрыт; тесты семантики гранта открывают его через env."""
    monkeypatch.setenv(store_mod.AUTOMATIC_ENV, "1")


@pytest.fixture
def gate_off(monkeypatch):
    monkeypatch.delenv(store_mod.AUTOMATIC_ENV, raising=False)
    monkeypatch.setattr(store_mod, "AUTOMATIC_AVAILABLE", False)


@pytest.fixture
def st(tmp_path):
    return RulesStore(tmp_path)


def _scope(**kw):
    base = {"accounts": ["act_1"], "objects": [{"account": "act_1", "level": "adset", "id": "111"}],
            "country": "Chile", "asset": ""}
    base.update(kw)
    return base


def _auto_rule(st):
    """Правило S1, готовое к гранту Automatic."""
    r = st.create_from_preset("stop_loss_new_tests", "Dan", by="dan", scope=_scope())
    return st.grant_automatic(r["id"], by="dan")


# --- схема и валидация --------------------------------------------------------

def test_defaults_are_valid_once_named():
    v = model.make_version(name="x")
    assert model.validate_version(v) == []


@pytest.mark.parametrize("patch,needle", [
    ({"name": ""}, "name"),
    ({"strategy": 9}, "strategy"),
    ({"priority": 101}, "priority"),
    ({"evidence": {"window_days": 0}}, "window_days"),
    ({"evidence": {"base": "weekly"}}, "evidence.base"),
    ({"action": {"kind": "delete"}}, "action.kind"),
    ({"action": {"kind": "budget_up", "step_pct": 25}}, "step_pct"),
    ({"caps": {"per_rule_day": -1}}, "per_rule_day"),
    ({"caps": {"max_daily_budget": 0}}, "max_daily_budget"),
    ({"conditions": [{"metric": "nope", "cmp": ">=", "value": 1, "unit": "abs"}]}, "metric"),
    ({"conditions": [{"op": "XOR", "items": []}]}, "group"),
    ({"target": {"use_goal": True, "override": {"value": -1}}}, "override"),
    ({"scope": {"accounts": ["a"], "objects": [{"account": "b", "level": "ad", "id": "1"}]}}, "not in scope"),
    ({"scope": {"include_new_objects": True}}, "include_new_objects"),
])
def test_validation_rejects(patch, needle):
    v = model.make_version(**{"name": "x", **patch})
    problems = model.validate_version(v)
    assert any(needle in p for p in problems), problems


def test_create_rejects_invalid_and_never_creates_automatic(st):
    with pytest.raises(model.RuleValidationError):
        st.create_rule("Dan", {"name": ""})
    with pytest.raises(model.RuleValidationError):
        st.create_rule("Dan", {"name": "x"}, mode="automatic")
    assert st.list_rules() == []


# --- пресеты --------------------------------------------------------------------

def test_preset_stop_loss_defaults():
    v, mode = model.preset("stop_loss_new_tests")
    assert mode == "observe" and v["strategy"] == 1
    assert v["action"]["kind"] == "pause" and v["action"]["level"] == "adset"
    assert v["caps"]["per_account_day"] == 3 and v["caps"]["cooldown_hours"] == 24
    assert v["evidence"]["maturity_days"] == 2 and v["evidence"]["min_age_days"] == 3
    item = {i["metric"]: i for g in v["conditions"] for i in g["items"]}
    assert item["spend"]["value"] == 3 and item["spend"]["unit"] == "t_multiple"
    assert item["ftd"]["value"] == 0
    assert model.validate_version(v) == []


def test_preset_scale_defaults_and_overrides():
    v, mode = model.preset("scale_proven", caps={"max_daily_budget": 80})
    assert mode == "suggest" and v["strategy"] == 3
    assert v["action"]["kind"] == "budget_up" and v["action"]["step_pct"] == 10
    assert v["caps"]["max_daily_budget"] == 80 and v["caps"]["cooldown_hours"] == 24
    item = {i["metric"]: i for g in v["conditions"] for i in g["items"]}
    assert item["ftd_total"]["value"] == 10 and item["cpa_block"]["value"] == 0.8
    assert model.validate_version(v) == []
    with pytest.raises(KeyError):
        model.preset("nope")


def test_presets_are_independent_copies():
    a, _ = model.preset("stop_loss_new_tests")
    a["caps"]["per_account_day"] = 99
    b, _ = model.preset("stop_loss_new_tests")
    assert b["caps"]["per_account_day"] == 3


# --- цель T ---------------------------------------------------------------------

GOALS = [{"country": "Chile", "code": "CL", "cpa": 40.0},
         {"country": "Kuwait", "code": "KW", "cpa": None},
         {"country": "Peru", "code": "PE", "cpa": 55, "assets": {"1121": {"cpa": 30}}}]


def test_target_from_country_goal():
    v = model.make_version(name="x", scope=_scope(country="Chile"))
    t = model.resolve_target(v, GOALS)
    assert (t["value"], t["source"]) == (40.0, "goal")
    assert model.resolve_target(v, GOALS, country="cl")["value"] == 40.0


def test_target_override_wins():
    v = model.make_version(name="x", scope=_scope(country="Chile"),
                           target={"override": {"value": 25, "currency": "USD"}})
    t = model.resolve_target(v, GOALS)
    assert (t["value"], t["source"]) == (25.0, "override")


def test_target_asset_override_falls_back_to_country():
    v = model.make_version(name="x", scope=_scope(country="Peru", asset="1121"))
    assert model.resolve_target(v, GOALS)["source"] == "goal_asset"
    v2 = model.make_version(name="x", scope=_scope(country="Peru", asset="9999"))
    t = model.resolve_target(v2, GOALS)
    assert (t["value"], t["source"]) == (55.0, "goal")


@pytest.mark.parametrize("country,goals", [
    ("Kuwait", GOALS), ("Atlantis", GOALS), ("Chile", []), ("Chile", None), ("", GOALS)])
def test_no_target(country, goals):
    v = model.make_version(name="x", scope=_scope(country=country))
    t = model.resolve_target(v, goals)
    assert t["value"] is None and t["source"] is None
    assert t["reason"].startswith(model.NO_TARGET)


def test_use_goal_false_without_override_is_no_target():
    v = model.make_version(name="x", scope=_scope(country="Chile"), target={"use_goal": False})
    assert model.resolve_target(v, GOALS)["value"] is None


# --- версии и грант -------------------------------------------------------------

def test_edit_creates_new_version_and_keeps_old(st):
    r = st.create_from_preset("stop_loss_new_tests", "Dan", scope=_scope())
    assert r["current_version"] == 1 and r["mode"] == "observe"
    r2 = st.edit_rule(r["id"], {"name": "Renamed", "priority": 5}, by="dan")
    assert r2["current_version"] == 2 and len(r2["versions"]) == 2
    assert r2["versions"][0]["name"] == "Stop-loss · new tests"
    assert r2["versions"][1]["name"] == "Renamed" and r2["versions"][1]["created_by"] == "dan"
    assert st.get_rule(r["id"])["current_version"] == 2


def test_edit_without_changes_makes_no_version(st):
    r = st.create_from_preset("stop_loss_new_tests", "Dan", scope=_scope())
    r2 = st.edit_rule(r["id"], {"name": r["versions"][0]["name"]})
    assert r2["current_version"] == 1


def test_edit_invalid_leaves_rule_untouched(st):
    r = st.create_from_preset("stop_loss_new_tests", "Dan", scope=_scope())
    with pytest.raises(model.RuleValidationError):
        st.edit_rule(r["id"], {"caps": {"per_rule_day": -5}})
    assert st.get_rule(r["id"])["current_version"] == 1


def test_expected_version_conflict(st):
    r = st.create_from_preset("stop_loss_new_tests", "Dan", scope=_scope())
    st.edit_rule(r["id"], {"priority": 1})
    with pytest.raises(store_mod.VersionConflict):
        st.edit_rule(r["id"], {"priority": 2}, expected_version=1)


def test_grant_requires_blockers_cleared(st):
    r = st.create_from_preset("scale_proven", "Dan", scope=_scope(objects=[
        {"account": "act_1", "level": "campaign", "id": "5"}]))
    with pytest.raises(model.RuleValidationError, match="max_daily_budget"):
        st.grant_automatic(r["id"], by="dan")  # budget_up без потолка
    r = st.edit_rule(r["id"], {"caps": {"max_daily_budget": 100}})
    g = st.grant_automatic(r["id"], by="dan")
    assert g["mode"] == "automatic" and g["grant"]["by"] == "dan"
    assert g["grant"]["version"] == r["current_version"]
    # alert не пишет в Meta
    a = st.create_rule("Dan", {"name": "a", "scope": _scope()})
    with pytest.raises(model.RuleValidationError):
        st.grant_automatic(a["id"], by="dan")
    # и S5 не поддерживается
    s5 = st.create_rule("Dan", {"name": "s5", "strategy": 5, "action": {"kind": "pause"}, "scope": _scope()})
    with pytest.raises(model.RuleValidationError, match="strategy"):
        st.grant_automatic(s5["id"], by="dan")


@pytest.mark.parametrize("changes", [
    {"scope": {"objects": [{"account": "act_1", "level": "adset", "id": "222"}]}},
    {"conditions": [{"metric": "spend", "cmp": ">=", "value": 5, "unit": "t_multiple"}]},
    {"action": {"level": "campaign"}},
    {"caps": {"per_account_day": 1}},
    {"target": {"override": {"value": 30, "currency": "USD"}}},
    {"evidence": {"min_sample": 4}},
    {"strategy": 2},
])
def test_grant_revoked_on_guarded_edit(st, changes):
    r = _auto_rule(st)
    assert st.active_grant(r) is not None
    r2 = st.edit_rule(r["id"], changes, by="dan")
    assert r2["grant"] is None and r2["mode"] == "suggest"
    assert r2["grant_revoked"]["reason"] == "edit"
    assert st.active_grant(r2) is None
    assert st.get_rule(r["id"])["grant"] is None


@pytest.mark.parametrize("changes", [
    {"name": "Other name"}, {"priority": 3},
])
def test_grant_survives_unguarded_edit(st, changes):
    r = _auto_rule(st)
    r2 = st.edit_rule(r["id"], changes)
    assert r2["current_version"] == 2 and r2["mode"] == "automatic"
    assert st.active_grant(r2) is not None


def test_active_grant_checks_hashes_not_just_flag(st):
    """Подмена версии в обход edit_rule не должна оставлять действующий грант."""
    r = _auto_rule(st)
    path = st.directory / "rules.json"
    data = json.loads(path.read_text("utf-8"))
    data["rules"][r["id"]]["versions"][0]["caps"]["per_account_day"] = 99
    path.write_text(json.dumps(data), "utf-8")
    assert st.active_grant(st.get_rule(r["id"])) is None


def test_set_mode_cannot_grant_and_leaving_automatic_revokes(st):
    r = _auto_rule(st)
    with pytest.raises(model.RuleValidationError):
        st.set_mode(r["id"], "automatic")
    r2 = st.set_mode(r["id"], "suggest")
    assert r2["mode"] == "suggest" and r2["grant"] is None
    r3 = st.grant_automatic(r["id"], by="dan")
    r4 = st.revoke_grant(r3["id"], by="dan")
    assert r4["mode"] == "suggest" and r4["grant"] is None


def test_duplicate_is_disabled_without_grant(st):
    r = _auto_rule(st)
    st.edit_rule(r["id"], {"priority": 2})
    d = st.duplicate(r["id"], by="dan")
    assert d["id"] != r["id"] and d["mode"] == "disabled" and d["grant"] is None
    assert d["current_version"] == 1 and len(d["versions"]) == 1
    assert d["versions"][0]["name"].endswith("(copy)") and d["versions"][0]["priority"] == 2
    assert d["copied_from"] == {"rule_id": r["id"], "version": 2}
    assert st.get_rule(r["id"])["mode"] == "automatic"  # оригинал не тронут


# --- настройки / kill switch ----------------------------------------------------

def test_kill_switch_defaults_off_and_persists(tmp_path):
    s = RulesStore(tmp_path)
    assert s.load_settings()["automatic_enabled"] is False
    s.set_automatic_enabled(True, by="dan")
    s2 = RulesStore(tmp_path)  # «рестарт»
    cur = s2.load_settings()
    assert cur["automatic_enabled"] is True and cur["changed_by"] == "dan"
    assert cur["limits"]["per_machine_day"] == 10
    s2.set_automatic_enabled(False, by="dan")
    assert RulesStore(tmp_path).load_settings()["automatic_enabled"] is False


def test_garbage_settings_mean_switched_off(tmp_path):
    (tmp_path / "rules_settings.json").write_text('{"automatic_enabled": "yes"}', "utf-8")
    assert RulesStore(tmp_path).load_settings()["automatic_enabled"] is False
    (tmp_path / "rules_settings.json").write_text("{torn", "utf-8")
    assert RulesStore(tmp_path).load_settings()["automatic_enabled"] is False


def test_default_dir_is_local_config_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("ACL_CONFIG_DIR", str(tmp_path))
    s = RulesStore()
    s.create_rule("Dan", {"name": "x"})
    assert (tmp_path / "rules.json").is_file()


# --- журналы и восстановление ---------------------------------------------------

def test_jsonl_append_and_indexes(st):
    st.append_evaluation({"rule_id": "r", "version": 1})
    a = st.append_action({"rule_id": "r", "id": "111", "state": "proposed"})
    st.append_action({"action_id": a["action_id"], "rule_id": "r", "id": "111", "state": "verified"})
    assert len(st.evaluations()) == 1 and len(st.actions()) == 2
    assert st.latest_actions()[a["action_id"]]["state"] == "verified"
    assert st.last_action_for("111")["state"] == "verified" and st.last_action_for("x") is None
    st.add_hold("111", "manual change")
    assert "111" in st.active_holds()
    st.release_hold("111", by="dan")
    assert st.active_holds() == {}


def test_torn_jsonl_tail_is_dropped_then_repaired(st):
    st.append_action({"rule_id": "r", "id": "1", "state": "proposed"})
    path = st.directory / "rule_actions.jsonl"
    with open(path, "ab") as fh:
        fh.write(b'{"action_id": "act_torn", "state": "runn')  # убили посреди записи
    assert len(st.actions()) == 1  # хвост не читается
    st.append_action({"rule_id": "r", "id": "2", "state": "proposed"})
    assert [r["id"] for r in st.actions()] == ["1", "2"]
    assert all(line.endswith(b"\n") for line in path.read_bytes().splitlines(True))
    assert b"act_torn" not in path.read_bytes()


def test_complete_record_missing_only_newline_is_kept(st):
    st.append_action({"rule_id": "r", "id": "1", "state": "proposed"})
    path = st.directory / "rule_actions.jsonl"
    path.write_bytes(path.read_bytes().rstrip(b"\n"))
    st.append_action({"rule_id": "r", "id": "2", "state": "proposed"})
    assert [r["id"] for r in st.actions()] == ["1", "2"]


def test_torn_rules_json_recovers_from_backup(st):
    r = st.create_from_preset("stop_loss_new_tests", "Dan", scope=_scope())
    st.edit_rule(r["id"], {"priority": 2})  # теперь есть .bak с версией 1
    path = st.directory / "rules.json"
    path.write_text('{"rules": {"rule_', "utf-8")  # битый файл
    got = RulesStore(st.directory).get_rule(r["id"])
    assert got["current_version"] == 1  # откат на последнюю целую копию, не пустота
    assert list(st.directory.glob("rules.json.corrupt-*")), "битый файл надо сохранить"


def test_torn_rules_json_without_backup_is_empty_not_crash(tmp_path):
    (tmp_path / "rules.json").write_text("{nope", "utf-8")
    s = RulesStore(tmp_path)
    assert s.list_rules() == []
    s.create_rule("Dan", {"name": "x"})
    assert len(s.list_rules()) == 1


def test_missing_rule(st):
    with pytest.raises(store_mod.RuleNotFound):
        st.get_rule("rule_nope")
    with pytest.raises(store_mod.RuleNotFound):
        st.edit_rule("rule_nope", {})


# --- параллельность -------------------------------------------------------------

def test_concurrent_threads_append_no_loss(st):
    def work(n):
        for i in range(25):
            st.append_action({"rule_id": "r", "id": f"{n}-{i}", "state": "proposed"})
    ts = [threading.Thread(target=work, args=(n,)) for n in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(st.actions()) == 100


def test_concurrent_processes_append_under_lock(tmp_path):
    code = textwrap.dedent("""
        import sys
        sys.path.insert(0, sys.argv[2])
        from src.analysis_agent.rules.store import RulesStore
        s = RulesStore(sys.argv[3])
        n = sys.argv[1]
        for i in range(15):
            s.append_action({"rule_id": "r", "id": n + "-" + str(i), "state": "proposed",
                             "pad": "x" * 3000})
        for i in range(3):
            s.create_rule("P" + n, {"name": "r" + str(i)})
    """)
    procs = [subprocess.Popen([sys.executable, "-c", code, str(n), str(ROOT), str(tmp_path)])
             for n in range(3)]
    assert [p.wait(timeout=120) for p in procs] == [0, 0, 0]
    s = RulesStore(tmp_path)
    ids = [r["action_id"] for r in s.actions()]
    assert len(ids) == 45 and len(set(ids)) == 45
    assert len(s.list_rules()) == 9  # ни одна запись rules.json не потерялась


# --- ревью #817: восстановление не включает автоматику заново --------------------

def test_restore_from_bak_never_rearms_grant(st):
    r = _auto_rule(st)
    st.revoke_grant(r["id"], by="dan")           # .bak теперь хранит версию с грантом
    (st.directory / "rules.json").write_text("{torn", "utf-8")
    got = RulesStore(st.directory).get_rule(r["id"])
    assert got["mode"] == "suggest" and got["grant"] is None
    assert RulesStore.active_grant(got) is None


def test_restore_from_bak_strips_grant_and_persists(st):
    r = _auto_rule(st)
    st.edit_rule(r["id"], {"priority": 2})        # грант цел, .bak тоже с грантом
    (st.directory / "rules.json").write_text("{torn", "utf-8")
    got = RulesStore(st.directory).get_rule(r["id"])
    assert got["mode"] == "suggest" and got["grant"] is None
    assert got["grant_revoked"]["reason"] == "restored_from_backup"
    on_disk = json.loads((st.directory / "rules.json").read_text("utf-8"))
    assert on_disk["rules"][r["id"]]["grant"] is None
    assert on_disk["rules"][r["id"]]["mode"] == "suggest"


def test_settings_fallback_forces_automatic_off_and_persists(tmp_path):
    s = RulesStore(tmp_path)
    s.set_automatic_enabled(True, by="dan")
    s.set_automatic_enabled(False, by="dan")
    s.set_automatic_enabled(True, by="dan")
    s.set_automatic_enabled(True, by="dan")       # .bak хранит True
    (tmp_path / "rules_settings.json").write_text("{torn", "utf-8")
    assert RulesStore(tmp_path).load_settings()["automatic_enabled"] is False
    saved = json.loads((tmp_path / "rules_settings.json").read_text("utf-8"))
    assert saved["automatic_enabled"] is False
    assert RulesStore(tmp_path).load_settings()["automatic_enabled"] is False


def test_settings_bak_true_is_ignored_when_main_missing_content(tmp_path):
    s = RulesStore(tmp_path)
    s.set_automatic_enabled(True, by="dan")
    s.set_automatic_enabled(True, by="dan")       # .bak = True
    (tmp_path / "rules_settings.json").write_bytes(bytes([0, 0]))
    assert RulesStore(tmp_path).load_settings()["automatic_enabled"] is False


def test_load_normalises_automatic_without_grant(st):
    r = _auto_rule(st)
    path = st.directory / "rules.json"
    data = json.loads(path.read_text("utf-8"))
    data["rules"][r["id"]]["grant"] = None        # mode остался automatic
    path.write_text(json.dumps(data), "utf-8")
    got = st.get_rule(r["id"])
    assert got["mode"] == "suggest"
    assert json.loads(path.read_text("utf-8"))["rules"][r["id"]]["mode"] == "suggest"
    assert any(a.get("kind") == "grant_dropped" and a["rule_id"] == r["id"] for a in st.actions())


def test_load_normalises_automatic_with_hash_mismatch(st):
    r = _auto_rule(st)
    path = st.directory / "rules.json"
    data = json.loads(path.read_text("utf-8"))
    data["rules"][r["id"]]["versions"][0]["target"]["override"] = {"value": 9, "currency": "USD"}
    path.write_text(json.dumps(data), "utf-8")
    got = st.get_rule(r["id"])
    assert got["mode"] == "suggest" and got["grant"] is None


def test_bak_is_written_atomically(st, monkeypatch):
    r = st.create_rule("Dan", {"name": "x"})
    seen = []
    real = store_mod.os.replace
    monkeypatch.setattr(store_mod.os, "replace", lambda a, b: (seen.append(Path(b).name), real(a, b))[1])
    st.edit_rule(r["id"], {"priority": 1})
    assert "rules.json.bak" in seen and "rules.json" in seen


# --- замок Automatic ----------------------------------------------------------

def test_gate_is_closed_by_default_in_code():
    assert store_mod.AUTOMATIC_AVAILABLE is False


def test_gate_refuses_grant(st, gate_off):
    r = st.create_from_preset("stop_loss_new_tests", "Dan", by="dan", scope=_scope())
    with pytest.raises(model.RuleValidationError, match="Automatic mode is not available yet."):
        st.grant_automatic(r["id"], by="dan")
    got = st.get_rule(r["id"])
    assert got["mode"] != "automatic" and got["grant"] is None


def test_gate_refuses_enable_flag_but_allows_disable(st, gate_off):
    with pytest.raises(store_mod.RulesStoreError, match="Automatic mode is not available yet."):
        st.set_automatic_enabled(True, by="dan")
    assert st.load_settings()["automatic_enabled"] is False
    assert st.set_automatic_enabled(False, by="dan")["automatic_enabled"] is False


def test_gate_env_value_must_be_exactly_one(st, gate_off, monkeypatch):
    monkeypatch.setenv(store_mod.AUTOMATIC_ENV, "true")
    with pytest.raises(store_mod.RulesStoreError):
        st.set_automatic_enabled(True, by="dan")


def test_stored_automatic_loads_as_suggest_while_gate_off(st, monkeypatch):
    r = _auto_rule(st)                                   # gate on: grant works
    monkeypatch.delenv(store_mod.AUTOMATIC_ENV)
    got = st.get_rule(r["id"])
    assert got["mode"] == "suggest" and got["grant"] is None
    assert json.loads((st.directory / "rules.json").read_text("utf-8"))["rules"][r["id"]]["mode"] == "suggest"


def test_stored_automatic_flag_reads_off_while_gate_off(st, monkeypatch):
    st.set_automatic_enabled(True, by="dan")
    monkeypatch.delenv(store_mod.AUTOMATIC_ENV)
    assert st.load_settings()["automatic_enabled"] is False


# --- schema_version -----------------------------------------------------------

def test_files_get_schema_version_on_save(st):
    st.create_from_preset("stop_loss_new_tests", "Dan", by="dan", scope=_scope())
    st.update_settings({"limits": {"per_rule_day": 4}}, by="dan")
    assert json.loads((st.directory / "rules.json").read_text("utf-8"))["schema_version"] == 1
    assert json.loads((st.directory / "rules_settings.json").read_text("utf-8"))["schema_version"] == 1
    assert st.load_settings()["schema_version"] == 1


def test_rules_without_schema_version_are_persisted_as_1(st):
    r = st.create_from_preset("stop_loss_new_tests", "Dan", by="dan", scope=_scope())
    path = st.directory / "rules.json"
    data = json.loads(path.read_text("utf-8"))
    del data["schema_version"]
    path.write_text(json.dumps(data), "utf-8")
    assert st.get_rule(r["id"])["id"] == r["id"]
    assert json.loads(path.read_text("utf-8"))["schema_version"] == 1


def test_unversioned_rules_do_not_lose_grants(st):
    r = _auto_rule(st)
    path = st.directory / "rules.json"
    data = json.loads(path.read_text("utf-8"))
    del data["schema_version"]
    path.write_text(json.dumps(data), "utf-8")
    assert st.get_rule(r["id"])["mode"] == "automatic"


def test_settings_without_schema_version_are_persisted_as_1(tmp_path):
    path = tmp_path / "rules_settings.json"
    path.write_text(json.dumps({"automatic_enabled": False, "limits": {"per_rule_day": 2}}), "utf-8")
    cur = RulesStore(tmp_path).load_settings()
    assert cur["schema_version"] == 1 and cur["limits"]["per_rule_day"] == 2
    assert json.loads(path.read_text("utf-8"))["schema_version"] == 1


def test_newer_rules_schema_is_refused_and_never_overwritten(st):
    st.create_from_preset("stop_loss_new_tests", "Dan", by="dan", scope=_scope())
    path = st.directory / "rules.json"
    data = json.loads(path.read_text("utf-8"))
    data["schema_version"] = 2
    path.write_text(json.dumps(data), "utf-8")
    before = path.read_bytes()
    with pytest.raises(store_mod.SchemaTooNew, match="schema_version 2"):
        st.list_rules()
    with pytest.raises(store_mod.SchemaTooNew):
        st.create_from_preset("stop_loss_new_tests", "Dan", by="dan", scope=_scope())
    assert path.read_bytes() == before
    assert not list(st.directory.glob("rules.json.corrupt-*"))


def test_newer_settings_schema_is_refused_and_never_overwritten(tmp_path):
    path = tmp_path / "rules_settings.json"
    path.write_text(json.dumps({"schema_version": 7, "automatic_enabled": False}), "utf-8")
    before = path.read_bytes()
    s = RulesStore(tmp_path)
    with pytest.raises(store_mod.SchemaTooNew):
        s.load_settings()
    with pytest.raises(store_mod.SchemaTooNew):
        s.update_settings({"limits": {"per_rule_day": 1}}, by="dan")
    assert path.read_bytes() == before


@pytest.mark.parametrize("bad", [0, -1, "1", True, 1.5])
def test_invalid_schema_version_is_refused(tmp_path, bad):
    path = tmp_path / "rules.json"
    path.write_text(json.dumps({"schema_version": bad, "rules": {}}), "utf-8")
    with pytest.raises(store_mod.RulesStoreError, match="schema_version"):
        RulesStore(tmp_path).list_rules()


# --- битое правило не прячет остальные -----------------------------------------

def _break(st, rule_id, **changes):
    path = st.directory / "rules.json"
    data = json.loads(path.read_text("utf-8"))
    rule = data["rules"][rule_id]
    for k, v in changes.items():
        if v is KeyError:
            rule.pop(k)
        else:
            rule[k] = v
    path.write_text(json.dumps(data), "utf-8")


@pytest.mark.parametrize("changes", [{"versions": KeyError}, {"current_version": 99},
                                     {"versions": []}, {"current_version": KeyError}])
def test_malformed_rule_is_quarantined_not_fatal(st, caplog, changes):
    good = st.create_from_preset("stop_loss_new_tests", "Dan", by="dan", scope=_scope())
    bad = st.create_from_preset("scale_proven", "Dan", by="dan", scope=_scope())
    _break(st, bad["id"], **changes)
    with caplog.at_level("WARNING"):
        listed = st.list_rules()
    assert [r["id"] for r in listed] == [good["id"]]
    assert [p["rule_id"] for p in st.problems] == [bad["id"]]
    assert bad["id"] in caplog.text
    rules, problems = st.list_rules_checked()
    assert len(rules) == 1 and problems[0]["rule_id"] == bad["id"] and problems[0]["error"]
    with pytest.raises(store_mod.RulesStoreError, match=bad["id"]):
        st.get_rule(bad["id"])
    # битое правило остаётся на диске для разбора, новые правила создаются как обычно
    st.create_from_preset("stop_loss_new_tests", "Dan", by="dan", scope=_scope())
    assert bad["id"] in json.loads((st.directory / "rules.json").read_text("utf-8"))["rules"]
