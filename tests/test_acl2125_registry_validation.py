"""Sourced limits reach the actual pre-upload API path, without Meta writes."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.campaign_bridge import meta_rules as mr
from src.fb_api import creator as cr
from src.fb_api import graph as g
from src.fb_api import write_policy as wp
from test_acl1761_api_create import ACT, FakeMeta, _draft, _run


def focused(**changes):
    job = dict(objective="OUTCOME_LEADS", optimization_goal="LEAD_GENERATION",
               destination_type="ON_AD", call_to_action="SIGN_UP", link="https://fb.me/",
               placements=["facebook.feed", "instagram.stream"])
    job.update(changes)
    rules = mr.load_rules()
    return mr.check_job(job, mr.RuleSet(rules.meta, tuple(
        r for r in rules.rules if r["id"].startswith("acl2125."))))


def test_shipped_rules_cover_three_kinds_with_current_sources():
    rules = [r for r in mr.load_rules().rules if r["id"].startswith("acl2125.")]
    assert {r["kind"] for r in rules} == {"text_limit", "cta_by_objective", "placements_by_objective"}
    assert all(r["checked_date"] == "2026-10-27" and "developers.facebook.com/" in r["source_url"] for r in rules)


def test_capabilities_keep_unconditional_builder_choices():
    full = mr.load_rules()
    prior = mr.RuleSet(full.meta, tuple(r for r in full.rules if not r["id"].startswith("acl2125.")))
    assert mr.capabilities(full) == mr.capabilities(prior)
    assert mr._ga_lists(full, "placements_by_objective") == []


@pytest.mark.parametrize("extra", [{"allowed": ["facebook.feed"]}, {"disallowed": []},
                                  {"disallowed": "facebook.right_hand_column"}])
def test_placement_prohibition_schema_is_unambiguous(extra):
    rule = dict(next(r for r in mr.load_rules().rules
                     if r["id"] == "acl2125.placements.instant_form.no_rhc"))
    rule.update(extra)
    with pytest.raises(mr.MetaRulesError):
        mr.validate_rules([rule])


def test_prohibition_does_not_grant_or_override_other_eligibility():
    deny = dict(next(r for r in mr.load_rules().rules
                     if r["id"] == "acl2125.placements.instant_form.no_rhc"))
    allow = {**deny, "id": "other.allow", "allowed": ["facebook.feed", "facebook.right_hand_column"]}
    del allow["disallowed"]
    job = {"objective": "OUTCOME_LEADS", "destination_type": "ON_AD",
           "optimization_goal": "LEAD_GENERATION", "placements": ["facebook.right_hand_column"]}
    assert mr.check_job(job, mr.RuleSet({}, (deny, allow)))[0].rule_id == deny["id"]
    job["placements"] = ["facebook.new_position"]
    assert mr.check_job(job, mr.RuleSet({}, (deny, allow)))[0].rule_id == allow["id"]
    assert not mr.check_job(job, mr.RuleSet({}, (deny,)))
    job["placements"] = ["facebook.right_hand_column"]
    assert not mr.check_job(job, mr.RuleSet({}, ({**deny, "status": "test"},)))


def test_link_hard_boundary_does_not_invent_copy_limits():
    assert not focused(link="x" * 1000, ad_title="x" * 200, ad_body="x" * 500)
    (v,) = focused(link="x" * 1001)
    assert v.field == "link" and "1000" in v.message_en and "Shorten" in v.message_en


@pytest.mark.parametrize("goal", ["LEAD_GENERATION", "QUALITY_LEAD"])
def test_form_ctas_are_scoped_to_form_conversion_location(goal):
    assert not focused(optimization_goal=goal)
    (v,) = focused(optimization_goal=goal, call_to_action="SHOP_NOW")
    assert v.field == "call_to_action" and "Choose" in v.message_en
    assert not focused(destination_type="WEBSITE", optimization_goal="OFFSITE_CONVERSIONS", call_to_action="SHOP_NOW")


def test_form_ctas_reuse_existing_write_policy_values():
    rule = next(r for r in mr.load_rules().rules if r["id"] == "acl2125.cta.instant_form")
    assert set(rule["allowed"]) == set(wp.CTA_FORM)
    assert all(not focused(call_to_action=cta) for cta in wp.CTA_FORM)


def test_only_proven_placement_restriction_is_enforced():
    (v,) = focused(placements=["facebook.feed", "facebook.right_hand_column"])
    assert v.field == "placements" and "Remove" in v.message_en
    assert not focused(placements=["facebook.future_position"])
    assert not focused(destination_type="WEBSITE", optimization_goal="OFFSITE_CONVERSIONS",
                       placements=["facebook.right_hand_column"])
    assert not focused(optimization_goal="QUALITY_LEAD", placements=["facebook.right_hand_column"])
    assert focused(placements={"facebook_positions": ["right_hand_column"]})


@pytest.mark.parametrize("kind", ["image", "video"])
def test_unused_form_website_url_is_not_validated(tmp_path, kind):
    meta = FakeMeta()
    client = g.GraphClient(meta, allow_writes=False, sleep=lambda _: None, min_interval_s=0, max_calls=200)
    creator = cr.CampaignCreator(client, cr.preflight_account(client, ACT), cr.TargetingResolver(client))
    plan = cr.plan_from_draft(_draft(tmp_path, ads=1))
    plan.link = "https://example.invalid/" + "x" * 1001
    plan.ad_sets[0].destination = cr.DEST_FORM
    plan.ad_sets[0].ads[0].kind = kind
    (_, job), = creator.meta_rule_jobs(plan, [{}])
    assert job["link"] == (cr.INSTANT_FORM_LINK if kind == "image" else None)
    assert not [v for v in mr.check_job(job) if v.field == "link"]


def test_preupload_checks_exact_cta_and_targeting(tmp_path):
    meta = FakeMeta()
    client = g.GraphClient(meta, allow_writes=False, sleep=lambda _: None, min_interval_s=0, max_calls=200)
    creator = cr.CampaignCreator(client, cr.preflight_account(client, ACT), cr.TargetingResolver(client))
    plan = cr.plan_from_draft(_draft(tmp_path, ads=1))
    plan.ad_sets[0].destination = cr.DEST_FORM
    plan.ad_sets[0].ads[0].cta = "CONTACT_US"
    with pytest.raises(cr.RulesViolation) as caught:
        creator._check_meta_rules(plan, [{"facebook_positions": ["right_hand_column"]}])
    assert "Choose" in str(caught.value) and "Remove" in str(caught.value)
    assert not meta.posts and client.writes == 0


def test_url_refused_by_actual_create_before_any_write(tmp_path):
    draft = _draft(tmp_path)
    draft.link = "https://example.invalid/" + "x" * 1001
    meta = FakeMeta()
    out, client = _run(meta, draft)
    assert not meta.posts and client.writes == 0
    assert out.stop_kind == "plan" and "1000" in out.error


def test_check_plan_reports_same_link_failure_without_writes(tmp_path):
    draft = _draft(tmp_path)
    draft.link = "https://example.invalid/" + "x" * 1001
    meta = FakeMeta()
    client = g.GraphClient(meta, allow_writes=False, sleep=lambda _: None, min_interval_s=0, max_calls=200)
    creator = cr.CampaignCreator(client, cr.preflight_account(client, ACT),
                                 cr.TargetingResolver(client), sleep=lambda _: None)
    out = creator.plan_only(cr.plan_from_draft(draft))
    assert not meta.posts and client.writes == 0
    assert out.stop_kind == "plan" and "1000" in out.error


def test_creator_adapter_exposes_exact_targeting_and_link(tmp_path):
    meta = FakeMeta()
    client = g.GraphClient(meta, allow_writes=False, sleep=lambda _: None, min_interval_s=0, max_calls=200)
    creator = cr.CampaignCreator(client, cr.preflight_account(client, ACT),
                                 cr.TargetingResolver(client), sleep=lambda _: None)
    plan = cr.plan_from_draft(_draft(tmp_path, ads=1))
    targets = [{"facebook_positions": ["feed"], "instagram_positions": ["stream"]}]
    (_, job), = creator.meta_rule_jobs(plan, targets)
    assert job["link"] == plan.link
    assert job["placements"] == targets[0]
