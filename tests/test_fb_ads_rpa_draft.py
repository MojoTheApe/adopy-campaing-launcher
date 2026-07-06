"""Tests for fb_ads_rpa draft building and preview."""
from __future__ import annotations

from src.fb_ads_rpa.build_draft import slots_to_campaign_drafts
from src.fb_ads_rpa.preview import preview_campaign_drafts


def test_slots_group_into_one_campaign_with_two_ads():
    slots = [
        {
            "name": "MX_Test_A",
            "targeting": "Broad",
            "budget": "15",
            "creative_type": "Image",
            "creative_id": "101",
            "copy": "Copy A",
        },
        {
            "name": "MX_Test_A",
            "targeting": "Broad",
            "budget": "15",
            "creative_type": "Image",
            "creative_id": "102",
            "copy": "Copy B",
        },
    ]
    drafts = slots_to_campaign_drafts(
        slots,
        account_label="Test Account",
        country="MX",
        activate=False,
    )
    assert len(drafts) == 1
    assert drafts[0].name == "MX_Test_A"
    assert drafts[0].status == "PAUSED"
    assert len(drafts[0].ad_sets) == 1
    assert len(drafts[0].ad_sets[0].ads) == 2
    assert drafts[0].ad_sets[0].daily_budget == 15.0


def test_slots_split_by_campaign_name():
    slots = [
        {"name": "Camp_A", "budget": "10", "creative_id": "1"},
        {"name": "Camp_B", "budget": "20", "creative_id": "2"},
    ]
    drafts = slots_to_campaign_drafts(
        slots,
        account_label="Acc",
        country="EC",
    )
    assert len(drafts) == 2
    assert {d.name for d in drafts} == {"Camp_A", "Camp_B"}


def test_activate_sets_active_status():
    slots = [{"name": "Live", "budget": "5", "creative_id": "1"}]
    drafts = slots_to_campaign_drafts(
        slots,
        account_label="Acc",
        country="MX",
        activate=True,
    )
    assert drafts[0].status == "ACTIVE"


def test_preview_includes_ad_rows():
    slots = [
        {"name": "C1", "budget": "10", "creative_type": "Image", "creative_id": "1", "copy": "x"},
        {"name": "C1", "budget": "10", "creative_type": "Video", "creative_id": "2", "copy": "y"},
    ]
    drafts = slots_to_campaign_drafts(slots, account_label="A", country="MX")
    plan = preview_campaign_drafts(drafts)
    assert len(plan) == 1
    assert plan[0]["Ads in ad set"] == 2
    assert "Ad 1 — Creative" in plan[0]
    assert "Ad 2 — Creative" in plan[0]
    assert plan[0]["Publish backend"] == "RPA (Ads Manager UI)"
