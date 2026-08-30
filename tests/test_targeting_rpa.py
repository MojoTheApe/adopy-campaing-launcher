"""Tests for detailed targeting RPA step."""
from __future__ import annotations

from unittest.mock import MagicMock, call, patch

import pytest

from src.fb_ads_rpa.ads_manager_flow import AdsManagerFlow, AdsManagerFlowError
from src.fb_ads_rpa.draft import AdSetDraft, RpaPublishResult


def _make_flow(selectors_cfg: dict | None = None):
    """Build an AdsManagerFlow with a mock page and optional selector override."""
    flow = AdsManagerFlow.__new__(AdsManagerFlow)
    flow.page = MagicMock()
    flow.action_timeout = 5000
    flow.cfg = selectors_cfg or {
        "selectors": {
            "detailed_targeting_input": ["input[placeholder*='Add interests']"],
            "targeting_suggestion_item": ["ul[role='listbox'] li:first-child"],
            "narrow_further_button": ["text=Narrow further"],
        }
    }
    return flow


def _ad_set(targeting: str) -> AdSetDraft:
    return AdSetDraft(name="Test Ad Set", targeting_label=targeting)


# ── how these tests changed ───────────────────────────────────────────────────
#
# They used to patch `_fill_first` and assert it was called once per interest.
# The step no longer works that way: Ads Manager accepts the whole
# comma-separated group pasted into the Detailed Targeting field and surfaces
# every match at once, which is faster and steadier than typing term by term,
# so `_fill_first` is now only a fallback. Mocking it meant asserting HOW the
# step is implemented, and the tests broke on a deliberate improvement while
# saying nothing about whether targeting still ends up correct.
#
# What matters and is asserted below: what text reaches the field, that `|`,
# `\` and `[AND]` all split into AND-groups, that a second group clicks Narrow
# further, and that one failed group does not abort the rest.

import contextlib


@contextlib.contextmanager
def _typed(flow):
    """Capture the strings the step sends to the Detailed Targeting field."""
    sent: list[str] = []

    def _fake_fill(page, text):
        sent.append(text)
        return True

    with patch("src.fb_ads_rpa.ads_manager_flow.fill_targeting_interest", _fake_fill),          patch("src.fb_ads_rpa.ads_manager_flow.open_detailed_targeting_field", return_value=True),          patch("src.fb_ads_rpa.ads_manager_flow.pick_first_suggestion", return_value=True),          patch("src.fb_ads_rpa.ads_manager_flow._click_first") as click,          patch("src.fb_ads_rpa.ads_manager_flow.pause_medium"),          patch("src.fb_ads_rpa.ads_manager_flow.pause_short"),          patch.object(flow, "_shot"):
        yield sent, click



# ── empty targeting ──────────────────────────────────────────────────────────

def test_empty_targeting_skipped():
    flow = _make_flow()
    result = RpaPublishResult(campaign_name="T")
    with patch.object(flow, "_shot"):
        flow._fill_detailed_targeting(_ad_set(""), result)
    assert "targeting_empty_skipped" in result.steps_completed
    flow.page.assert_not_called()


def test_whitespace_targeting_skipped():
    flow = _make_flow()
    result = RpaPublishResult(campaign_name="T")
    with patch.object(flow, "_shot"):
        flow._fill_detailed_targeting(_ad_set("  ,  "), result)
    assert "targeting_empty_skipped" in result.steps_completed


# ── single term ──────────────────────────────────────────────────────────────


# ── multiple terms ───────────────────────────────────────────────────────────


# ── [AND] groups — Narrow further ────────────────────────────────────────────




def test_no_and_no_narrow_further():
    """Without [AND], narrow_further is never clicked."""
    flow = _make_flow()
    result = RpaPublishResult(campaign_name="T")

    with patch("src.fb_ads_rpa.ads_manager_flow._fill_first"), \
         patch("src.fb_ads_rpa.ads_manager_flow._click_first") as mock_click, \
         patch("src.fb_ads_rpa.ads_manager_flow.pause_medium"), \
         patch("src.fb_ads_rpa.ads_manager_flow.pause_short"), \
         patch.object(flow, "_shot"):
        flow._fill_detailed_targeting(_ad_set("Amazon, Finance, Investing"), result)

    narrow_clicks = [c for c in mock_click.call_args_list if "Narrow further" in str(c)]
    assert len(narrow_clicks) == 0


# ── failed term: continues to next ───────────────────────────────────────────


# ── what reaches the targeting field ─────────────────────────────────────────

def test_single_group_is_pasted_whole():
    flow = _make_flow()
    result = RpaPublishResult(campaign_name="T")
    with _typed(flow) as (sent, _click):
        flow._fill_detailed_targeting(_ad_set("Amazon, Online Shopping"), result)

    assert sent == ["Amazon, Online Shopping"], "группа должна уходить одной строкой"
    assert any(s.startswith("targeting_g1_dom_interest") for s in result.steps_completed)


@pytest.mark.parametrize(
    "written",
    ["Amazon, Online Shopping [AND] Finance",
     "Amazon, Online Shopping | Finance",
     r"Amazon, Online Shopping\Finance"],
)
def test_all_three_and_separators_split_into_two_groups(written):
    """`[AND]`, `|` and `\` are the same instruction written three ways.

    The sheet is filled by hand, so all three occur; treating one of them as
    part of an interest name would send the campaign at a different audience
    than the one that was asked for.
    """
    flow = _make_flow()
    result = RpaPublishResult(campaign_name="T")
    with _typed(flow) as (sent, _click):
        flow._fill_detailed_targeting(_ad_set(written), result)

    assert len(sent) == 2, f"{written!r} не распалось на две группы: {sent}"
    assert "Finance" in sent[1]
    assert "targeting_g2_narrow_further" in result.steps_completed


def test_single_group_never_narrows():
    flow = _make_flow()
    result = RpaPublishResult(campaign_name="T")
    with _typed(flow) as (_sent, _click):
        flow._fill_detailed_targeting(_ad_set("Amazon, Finance, Investing"), result)

    assert not [s for s in result.steps_completed if "narrow_further" in s]


def test_one_failing_group_does_not_abort_the_rest():
    """A group that will not paste must not cost us the other one."""
    flow = _make_flow()
    result = RpaPublishResult(campaign_name="T")
    calls: list[str] = []

    def _fill_first_fails(page, text):
        calls.append(text)
        return len(calls) > 1  # первая группа не вставилась

    with patch("src.fb_ads_rpa.ads_manager_flow.fill_targeting_interest", _fill_first_fails),          patch("src.fb_ads_rpa.ads_manager_flow.open_detailed_targeting_field", return_value=True),          patch("src.fb_ads_rpa.ads_manager_flow.pick_first_suggestion", return_value=True),          patch("src.fb_ads_rpa.ads_manager_flow._click_first"),          patch("src.fb_ads_rpa.ads_manager_flow._fill_first"),          patch("src.fb_ads_rpa.ads_manager_flow.pause_medium"),          patch("src.fb_ads_rpa.ads_manager_flow.pause_short"),          patch.object(flow, "_shot"):
        flow._fill_detailed_targeting(_ad_set("Amazon [AND] Finance"), result)

    assert "Finance" in calls, "после неудачи первой группы вторая не была даже испробована"
