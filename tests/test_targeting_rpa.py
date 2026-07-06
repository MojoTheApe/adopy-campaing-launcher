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
        }
    }
    return flow


def _ad_set(targeting: str) -> AdSetDraft:
    return AdSetDraft(name="Test Ad Set", targeting_label=targeting)


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

def test_single_term_fills_and_selects():
    flow = _make_flow()
    result = RpaPublishResult(campaign_name="T")

    with patch("src.fb_ads_rpa.ads_manager_flow._fill_first") as mock_fill, \
         patch("src.fb_ads_rpa.ads_manager_flow._click_first") as mock_click, \
         patch("src.fb_ads_rpa.ads_manager_flow.pause_medium"), \
         patch("src.fb_ads_rpa.ads_manager_flow.pause_short"), \
         patch.object(flow, "_shot"):
        flow._fill_detailed_targeting(_ad_set("Amazon"), result)

    mock_fill.assert_called_once()
    # _fill_first(page, selectors, value, ...)  →  args[2] = value
    args = mock_fill.call_args[0]
    assert args[2] == "Amazon"
    mock_click.assert_called_once()
    assert "targeting_1:Amazon" in result.steps_completed


# ── multiple terms ───────────────────────────────────────────────────────────

def test_multiple_terms_each_filled():
    flow = _make_flow()
    result = RpaPublishResult(campaign_name="T")

    with patch("src.fb_ads_rpa.ads_manager_flow._fill_first") as mock_fill, \
         patch("src.fb_ads_rpa.ads_manager_flow._click_first"), \
         patch("src.fb_ads_rpa.ads_manager_flow.pause_medium"), \
         patch("src.fb_ads_rpa.ads_manager_flow.pause_short"), \
         patch.object(flow, "_shot"):
        flow._fill_detailed_targeting(_ad_set("Amazon, Online Shopping, Finance"), result)

    assert mock_fill.call_count == 3
    terms_filled = [c[0][2] for c in mock_fill.call_args_list]
    assert terms_filled == ["Amazon", "Online Shopping", "Finance"]
    assert "targeting_1:Amazon" in result.steps_completed
    assert "targeting_2:Online Shopping" in result.steps_completed
    assert "targeting_3:Finance" in result.steps_completed


# ── failed term: continues to next ───────────────────────────────────────────

def test_failed_term_continues():
    flow = _make_flow()
    result = RpaPublishResult(campaign_name="T")

    def fill_side_effect(page, sels, value, **kw):
        if value == "BadTerm":
            raise AdsManagerFlowError("not found")

    with patch("src.fb_ads_rpa.ads_manager_flow._fill_first", side_effect=fill_side_effect), \
         patch("src.fb_ads_rpa.ads_manager_flow._click_first"), \
         patch("src.fb_ads_rpa.ads_manager_flow.pause_medium"), \
         patch("src.fb_ads_rpa.ads_manager_flow.pause_short"), \
         patch.object(flow, "_shot"):
        flow._fill_detailed_targeting(_ad_set("Amazon, BadTerm, Finance"), result)

    assert "targeting_1:Amazon" in result.steps_completed
    assert any("targeting_2_failed" in s for s in result.steps_completed)
    assert "targeting_3:Finance" in result.steps_completed
