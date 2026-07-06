"""Tests for _parse_build_upload_form — multi-ad per campaign."""
from __future__ import annotations

from src.web.app import _parse_build_upload_form


def _form(**kw):
    """Simulate form dict."""
    return {k: str(v) for k, v in kw.items()}


def test_single_campaign_single_ad():
    form = _form(
        country="MX",
        account_label="MX-acc-1",
        camp_count=1,
        **{
            "camp_0_targeting": "Broad",
            "camp_0_os": "All",
            "camp_0_age": "35",
            "camp_0_budget": "50",
            "camp_0_asset": "Amazon",
            "camp_0_name": "MX_Amazon_Test",
            "camp_0_ad_count": "1",
            "camp_0_ad_0_creative_type": "Image",
            "camp_0_ad_0_creative_file": "photo.jpg",
            "camp_0_ad_0_copy": "Copy A",
        },
    )
    country, label, slots = _parse_build_upload_form(form)
    assert country == "MX"
    assert label == "MX-acc-1"
    assert len(slots) == 1
    assert slots[0]["name"] == "MX_Amazon_Test"
    assert slots[0]["creative_file"] == "photo.jpg"
    assert slots[0]["copy"] == "Copy A"


def test_single_campaign_two_ads():
    form = _form(
        country="MX",
        account_label="MX-acc-1",
        camp_count=1,
        **{
            "camp_0_targeting": "Broad",
            "camp_0_os": "All",
            "camp_0_age": "35",
            "camp_0_budget": "50",
            "camp_0_asset": "Amazon",
            "camp_0_name": "MX_Amazon_Test",
            "camp_0_ad_count": "2",
            "camp_0_ad_0_creative_type": "Image",
            "camp_0_ad_0_creative_file": "img1.jpg",
            "camp_0_ad_0_copy": "Copy A",
            "camp_0_ad_1_creative_type": "Video",
            "camp_0_ad_1_creative_file": "vid1.mp4",
            "camp_0_ad_1_copy": "Copy B",
        },
    )
    country, label, slots = _parse_build_upload_form(form)
    assert len(slots) == 2
    # Both slots share same campaign name
    assert slots[0]["name"] == slots[1]["name"] == "MX_Amazon_Test"
    assert slots[0]["creative_file"] == "img1.jpg"
    assert slots[1]["creative_file"] == "vid1.mp4"
    assert slots[0]["creative_type"] == "Image"
    assert slots[1]["creative_type"] == "Video"
    # Shared campaign-level fields
    assert slots[0]["targeting"] == slots[1]["targeting"] == "Broad"
    assert slots[0]["budget"] == slots[1]["budget"] == "50"


def test_two_campaigns_each_with_two_ads():
    form = _form(
        country="EC",
        account_label="EC-acc",
        camp_count=2,
        **{
            "camp_0_targeting": "Trading",
            "camp_0_os": "iOS",
            "camp_0_age": "30",
            "camp_0_budget": "40",
            "camp_0_asset": "eToro",
            "camp_0_name": "EC_eToro_Camp1",
            "camp_0_ad_count": "2",
            "camp_0_ad_0_creative_type": "Image",
            "camp_0_ad_0_creative_file": "a.jpg",
            "camp_0_ad_0_copy": "c1",
            "camp_0_ad_1_creative_type": "Image",
            "camp_0_ad_1_creative_file": "b.jpg",
            "camp_0_ad_1_copy": "c2",
            "camp_1_targeting": "Invest",
            "camp_1_os": "All",
            "camp_1_age": "40",
            "camp_1_budget": "60",
            "camp_1_asset": "eToro",
            "camp_1_name": "EC_eToro_Camp2",
            "camp_1_ad_count": "2",
            "camp_1_ad_0_creative_type": "Video",
            "camp_1_ad_0_creative_file": "v1.mp4",
            "camp_1_ad_0_copy": "d1",
            "camp_1_ad_1_creative_type": "Video",
            "camp_1_ad_1_creative_file": "v2.mp4",
            "camp_1_ad_1_copy": "d2",
        },
    )
    country, label, slots = _parse_build_upload_form(form)
    assert len(slots) == 4
    c1_slots = [s for s in slots if s["name"] == "EC_eToro_Camp1"]
    c2_slots = [s for s in slots if s["name"] == "EC_eToro_Camp2"]
    assert len(c1_slots) == 2
    assert len(c2_slots) == 2
    assert c1_slots[0]["creative_file"] == "a.jpg"
    assert c2_slots[1]["creative_file"] == "v2.mp4"


def test_autoname_uses_first_ad_type():
    form = _form(
        country="MX",
        account_label="acc",
        camp_count=1,
        **{
            "camp_0_targeting": "Broad",
            "camp_0_os": "All",
            "camp_0_age": "35",
            "camp_0_budget": "50",
            "camp_0_asset": "Amz",
            "camp_0_ad_count": "1",
            "camp_0_ad_0_creative_type": "Video",
            "camp_0_ad_0_creative_file": "vid.mp4",
            "camp_0_ad_0_copy": "",
        },
    )
    country, label, slots = _parse_build_upload_form(form)
    assert len(slots) == 1
    assert "Video" in slots[0]["name"]
    assert "MX" in slots[0]["name"]
    assert "Amz" in slots[0]["name"]
