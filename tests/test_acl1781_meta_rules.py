"""ACL-1781: реестр правил Meta и проверка задания до загрузки."""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest

from src.campaign_bridge import meta_rules as mr
from src.campaign_bridge.dashboard_publisher import slots_to_publish_jobs
from src.web import publish_preflight as pf


def _job(**kw):
    base = dict(objective="OUTCOME_LEADS", optimization_goal="OFFSITE_CONVERSIONS",
                billing_event="IMPRESSIONS", bid_strategy="LOWEST_COST_WITHOUT_CAP",
                destination_type="WEBSITE", ad_title="Title", ad_body="Body", ad_link_description="",
                api_preview={"campaign": {"special_ad_categories": []},
                             "ad_creative": {"call_to_action": {"type": "LEARN_MORE"}}})
    base.update(kw)
    return SimpleNamespace(**base)


def _ruleset(rules, tmp_path=None):
    return mr.RuleSet(meta={}, rules=tuple(rules))


def _rule(**kw):
    r = {"id": "t.1", "kind": "objective", "applies_to": {}, "allowed": ["A"], "status": "ga",
         "source_url": "https://example.com/doc", "checked_date": "2026-10-11", "note": ""}
    r.update(kw)
    return r


def test_shipped_registry_loads_and_every_rule_has_source():
    rs = mr.load_rules()
    assert rs.meta["api_version_current"] == "v26.0"
    assert rs.rules and all(r["source_url"].startswith("https://") and r["checked_date"] for r in rs.rules)


def test_valid_production_combination_passes():
    assert mr.check_job(_job()) == []


def test_real_slot_fixture_passes():
    slots = [{"name": "Camp - US", "targeting": "", "os": "All", "age": "35", "budget": "20",
              "creative_type": "Image", "creative_id": "1", "copy": "T1"}]
    jobs = slots_to_publish_jobs(slots, account_label="Acc", country="US", preview_mode=True,
                                 upload_fb_path="missing.xlsx", ad_account_map_path="missing.csv")
    assert jobs and all(mr.check_job(j) == [] for j in jobs)


def test_legacy_objective_blocked():
    v = mr.check_job(_job(objective="LINK_CLICKS"))
    assert len(v) == 1 and v[0].field == "objective" and "deprecated" in v[0].message_en
    assert v[0].source_url.startswith("https://")


def test_unknown_goal_cta_billing_blocked():
    assert mr.check_job(_job(optimization_goal="MAKE_MONEY"))[0].field == "optimization_goal"
    cta = _job(api_preview={"campaign": {"special_ad_categories": []},
                            "ad_creative": {"call_to_action": {"type": "CLICK_ME"}}})
    assert mr.check_job(cta)[0].field == "call_to_action"
    assert mr.check_job(_job(billing_event="FREE"))[0].field == "billing_event"


def test_missing_special_ad_categories_and_bid_amount():
    v = mr.check_job(_job(api_preview={}))
    assert any(x.field == "api_preview.campaign.special_ad_categories" for x in v)
    v = mr.check_job(_job(bid_strategy="LOWEST_COST_WITH_BID_CAP"))
    assert any(x.field == "bid_amount" for x in v)
    assert not mr.check_job(_job(bid_strategy="LOWEST_COST_WITH_BID_CAP", bid_amount=5))


def test_non_ga_rule_never_allows_value():
    rs = _ruleset([_rule(allowed=["X"], status="beta")])
    v = mr.check_job(SimpleNamespace(objective="X"), rs)
    assert len(v) == 1 and "not generally available" in v[0].message_en
    rs = _ruleset([_rule(allowed=["X"], status="ga"), _rule(id="t.2", allowed=["Y"], status="test")])
    assert mr.check_job(SimpleNamespace(objective="X"), rs) == []
    assert mr.check_job(SimpleNamespace(objective="Y"), rs)


def test_scoped_rule_kinds():
    rs = _ruleset([
        _rule(id="g", kind="optimization_goal_by_objective", applies_to={"objective": "OUTCOME_LEADS"},
              allowed=["LEAD_GENERATION"]),
        _rule(id="b", kind="billing_event_by_goal", applies_to={"optimization_goal": "REACH"},
              allowed=["IMPRESSIONS"]),
        _rule(id="c", kind="cta_by_objective", applies_to={"objective": "OUTCOME_LEADS"}, allowed=["SIGN_UP"]),
        _rule(id="p", kind="placements_by_objective", applies_to={"objective": "OUTCOME_LEADS"},
              allowed=["facebook_feed"]),
    ])
    j = SimpleNamespace(objective="OUTCOME_LEADS", optimization_goal="REACH", billing_event="CLICKS",
                        call_to_action="LEARN_MORE", placements=["facebook_feed", "audience_network"])
    fields = sorted(v.field for v in mr.check_job(j, rs))
    assert fields == ["billing_event", "call_to_action", "optimization_goal", "placements"]
    other = SimpleNamespace(objective="OUTCOME_SALES", optimization_goal="REACH")
    assert mr.check_job(other, rs) == []  # правило не про эту цель


def test_text_limit_and_unknown_field_skipped():
    rs = _ruleset([_rule(id="t", kind="text_limit", applies_to={"field": "ad_title"}, max_len=5, allowed=None)])
    rs = mr.RuleSet(meta={}, rules=({**rs.rules[0]},))
    v = mr.check_job(SimpleNamespace(ad_title="toolong"), rs)
    assert v[0].field == "ad_title" and "at most 5" in v[0].message_en
    assert mr.check_job(SimpleNamespace(ad_title="ok"), rs) == []
    assert mr.check_job(SimpleNamespace(), rs) == []


def test_loader_schema_validation(tmp_path):
    good_rules = json.loads(open(mr.RULES_DIR / "rules.json", encoding="utf-8").read())
    meta = json.loads(open(mr.RULES_DIR / "meta.json", encoding="utf-8").read())

    def write(m, r):
        (tmp_path / "meta.json").write_text(json.dumps(m), encoding="utf-8")
        (tmp_path / "rules.json").write_text(json.dumps(r), encoding="utf-8")
        return mr.load_rules(tmp_path)

    assert write(meta, good_rules).rules
    bad = copy.deepcopy(good_rules)
    bad[0]["status"] = "maybe"
    with pytest.raises(mr.MetaRulesError):
        write(meta, bad)
    bad = copy.deepcopy(good_rules); bad[0]["kind"] = "nope"
    with pytest.raises(mr.MetaRulesError):
        write(meta, bad)
    bad = copy.deepcopy(good_rules); del bad[0]["allowed"]
    with pytest.raises(mr.MetaRulesError):
        write(meta, bad)
    bad = copy.deepcopy(good_rules); bad[0]["checked_date"] = "11.10.2026"
    with pytest.raises(mr.MetaRulesError):
        write(meta, bad)
    bad = copy.deepcopy(good_rules); bad.append(copy.deepcopy(bad[0]))
    with pytest.raises(mr.MetaRulesError):
        write(meta, bad)
    bad_meta = copy.deepcopy(meta); bad_meta["api_version_current"] = "v99.0"
    with pytest.raises(mr.MetaRulesError):
        write(bad_meta, good_rules)
    with pytest.raises(mr.MetaRulesError):
        mr.load_rules(tmp_path / "nowhere")


def test_preflight_blocks_with_english_message(monkeypatch):
    import src.campaign_bridge.dashboard_publisher as dp

    bad = _job(objective="LINK_CLICKS", campaign_name="Camp A")
    ok = _job(campaign_name="Camp B")
    monkeypatch.setattr(dp, "slots_to_publish_jobs", lambda *a, **k: [bad, ok])
    out = pf.meta_rules_errors([{"name": "x"}], account_label="A", country="US")
    assert list(out) == ["Camp A"]
    assert "Campaign objective 'LINK_CLICKS'" in out["Camp A"][0] and "source:" in out["Camp A"][0]


def test_preflight_fails_open_on_infrastructure_error(monkeypatch):
    monkeypatch.setattr(mr, "load_rules", lambda *a, **k: (_ for _ in ()).throw(mr.MetaRulesError("x")))
    assert pf.meta_rules_errors([], account_label="A", country="US") == {}
