"""ACL-2052: реестр Meta — value rules, account controls, soft opt-out."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from src.campaign_bridge import meta_rules as mr

ROOT = Path(__file__).resolve().parents[1]
NEW_URLS = {
    "https://developers.facebook.com/documentation/ads-commerce/marketing-api/bidding/value-rules.md",
    "https://developers.facebook.com/documentation/ads-commerce/marketing-api/reference/ad-account/account_controls",
    "https://developers.facebook.com/documentation/ads-commerce/marketing-api/bidding-and-optimization/bid-multiplier",
    "https://developers.facebook.com/blog/post/2025/10/08/introducing-limited-spend-on-excluded-placements-in-marketing-api/",
}


def _new_rules():
    return [r for r in mr.load_rules().rules if r["checked_date"] == "2026-10-21"]


def test_sources_include_the_four_pages():
    data = json.loads((ROOT / "config" / "meta_rules" / "sources.json").read_text(encoding="utf-8"))
    urls = [s["url"] for s in data["sources"]]
    assert NEW_URLS <= set(urls) and len(urls) == len(set(urls))


def test_registry_entries_validate_and_are_not_ga():
    rules = _new_rules()
    ids = {r["id"] for r in rules}
    assert {"value_rules.bid_strategy", "value_rules.attach", "value_rules.decrease_cap",
            "account_controls.placement_exclusions", "account_controls.post",
            "bid_adjustments.retired", "placement_soft_opt_out.spend",
            "placements.classic_non_advantage_leads"} <= ids
    for r in rules:
        assert r["status"] in ("limited", "test", "deprecated"), r["id"]
        assert r["source_url"] in NEW_URLS or r["id"].startswith("placements."), r["id"]
        assert "feature" in r["applies_to"], r["id"]
    by = {r["id"]: r for r in rules}
    assert by["value_rules.bid_strategy"]["allowed"] == ["LOWEST_COST_WITHOUT_CAP", "COST_CAP"]
    assert "audience_controls" in by["account_controls.post"]["required"]
    assert "DECREASE" in by["value_rules.decrease_cap"]["note"] and "90" in by["value_rules.decrease_cap"]["note"]


def _job(**kw):
    base = dict(objective="OUTCOME_LEADS", optimization_goal="OFFSITE_CONVERSIONS",
                billing_event="IMPRESSIONS", bid_strategy="COST_CAP", destination_type="WEBSITE",
                ad_title="T", ad_body="B", ad_link_description="",
                api_preview={"campaign": {"special_ad_categories": []}, "ad_set": {"placements": ["facebook"]},
                             "ad_creative": {"call_to_action": {"type": "LEARN_MORE"}}})
    base.update(kw)
    return SimpleNamespace(**base)


def test_check_job_unchanged_for_existing_builder_jobs():
    full = mr.load_rules()
    old = mr.RuleSet(meta=full.meta, rules=tuple(r for r in full.rules if r["checked_date"] != "2026-10-21"))
    assert len(old.rules) < len(full.rules)
    jobs = [_job(), _job(objective="OUTCOME_SALES"), _job(objective="LEAD_GENERATION"),
            _job(bid_strategy="LOWEST_COST_WITH_BID_CAP"), _job(optimization_goal="BOGUS"),
            _job(objective="OUTCOME_SALES", bid_strategy="LOWEST_COST_WITHOUT_CAP")]
    for j in jobs:
        assert mr.check_job(j, full) == mr.check_job(j, old)
