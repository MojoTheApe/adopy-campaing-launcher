"""ACL-1437: campaign problems reach the dashboard as a short structured list.

Every step the policy knows carries a plain title, one sentence of impact and
one "what to do" line; the job result carries them per problem next to the old
plain text (kept for older records and Sentry).
"""
from __future__ import annotations

import re

import pytest

from src.fb_ads_rpa import failure_policy as fp
from src.fb_ads_rpa.draft import RpaPublishResult

# Real failure strings from the 2026-10-01 live runs (rpa_telemetry.jsonl).
CONVERSION = "conversion_event"
AD_COPY = "ad_copy:1741 - Pro Bancolombia has no text"
SECOND_TEXT = "ad2_second_text:2046 - Story14 second primary text not added (add_control_not_found)"


@pytest.mark.parametrize("table,guide", [
    (fp.POLICY, fp.GUIDE),
    (fp.PER_AD_POLICY, fp.PER_AD_GUIDE),
])
def test_every_policy_step_has_title_impact_and_action(table, guide):
    missing = sorted(set(table) - set(guide))
    assert not missing, f"steps without title/impact/action: {missing}"
    extra = sorted(set(guide) - set(table))
    assert not extra, f"guide entries for steps the policy does not know: {extra}"
    for name, entry in guide.items():
        title, impact, action = entry
        for label, text in (("title", title), ("impact", impact), ("action", action)):
            assert text and text.strip(), f"{name}: empty {label}"
            assert not re.search("[А-Яа-яЁё]", text), f"{name}: {label} must be English"
        assert len(title) <= 60, f"{name}: title is not short"
        assert impact.endswith("."), f"{name}: impact is one sentence ending with a period"
        assert action.endswith("."), f"{name}: action is one sentence ending with a period"


def test_a_per_ad_step_names_the_ad():
    item = fp.problem(AD_COPY)
    assert item["step"] == "ad_copy"
    assert item["ad_index"] == 1
    assert item["ad_name"] == "1741 - Pro Bancolombia"
    assert item["severity"] == fp.BLOCKING
    assert item["title"] == "Ad text"
    assert item["note"] == "1741 - Pro Bancolombia has no text"
    assert item["raw"] == AD_COPY


def test_a_later_ad_carries_its_number_and_a_warning_stays_a_warning():
    item = fp.problem(SECOND_TEXT)
    assert item["ad_index"] == 2
    assert item["ad_name"] == "2046 - Story14"
    assert item["severity"] == fp.WARNING
    assert item["note"].endswith("(add_control_not_found)")


def test_a_campaign_level_step_has_no_ad():
    item = fp.problem(CONVERSION)
    assert item["ad_index"] is None and item["ad_name"] == ""
    assert item["title"] == "Conversion event — not verified"
    assert item["note"] == ""


def test_an_unknown_step_is_still_listed_as_blocking():
    item = fp.problem("brand_new_step:boom")
    assert item["severity"] == fp.BLOCKING
    assert "brand_new_step" in item["title"]
    assert item["impact"] and item["action"]


def test_problems_put_blocking_first_and_drop_exact_repeats():
    items = fp.problems([SECOND_TEXT, CONVERSION, AD_COPY, CONVERSION])
    assert [i["raw"] for i in items] == [CONVERSION, AD_COPY, SECOND_TEXT]


def test_job_result_carries_the_structured_list_next_to_the_old_text():
    from src.fb_ads_rpa.ads_manager_flow import apply_step_failures

    result = RpaPublishResult(campaign_name="CO_bnc_All_Video_FX_Y_35_01.10", success=True)
    apply_step_failures(result, [CONVERSION, AD_COPY, SECOND_TEXT])
    result.run_id = "cc8fcfa17ae9"
    job = result.to_job_dict()

    assert "DO NOT PUBLISH AS IS" in job["error"]          # old text kept
    assert job["blocking_failures"] == [CONVERSION, AD_COPY]
    assert job["warnings"] == [SECOND_TEXT]
    assert job["run_id"] == "cc8fcfa17ae9"
    steps = [(p["step"], p["severity"]) for p in job["problems"]]
    assert steps == [("conversion_event", "blocking"), ("ad_copy", "blocking"),
                     ("ad2_second_text", "warning")]
    for p in job["problems"]:
        assert set(p) >= {"step", "ad_index", "ad_name", "severity", "title",
                          "impact", "action", "note", "raw"}


def test_a_clean_result_has_an_empty_list():
    job = RpaPublishResult(campaign_name="x", success=True).to_job_dict()
    assert job["problems"] == []
    assert job["run_id"] is None
