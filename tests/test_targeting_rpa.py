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
    args = mock_fill.call_args[0]
    assert args[2] == "Amazon"
    # suggestion click + no narrow_further (single group)
    assert "targeting_g1_t1:Amazon" in result.steps_completed


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
    assert "targeting_g1_t1:Amazon" in result.steps_completed
    assert "targeting_g1_t2:Online Shopping" in result.steps_completed
    assert "targeting_g1_t3:Finance" in result.steps_completed


# ── [AND] groups — Narrow further ────────────────────────────────────────────

def test_and_group_clicks_narrow_further():
    """[AND] marker triggers 'Narrow further' click before second group."""
    flow = _make_flow()
    result = RpaPublishResult(campaign_name="T")

    with patch("src.fb_ads_rpa.ads_manager_flow._fill_first") as mock_fill, \
         patch("src.fb_ads_rpa.ads_manager_flow._click_first") as mock_click, \
         patch("src.fb_ads_rpa.ads_manager_flow.pause_medium"), \
         patch("src.fb_ads_rpa.ads_manager_flow.pause_short"), \
         patch.object(flow, "_shot"):
        flow._fill_detailed_targeting(
            _ad_set("Amazon, Online Shopping [AND] Finance, Investing"),
            result,
        )

    terms_filled = [c[0][2] for c in mock_fill.call_args_list]
    assert terms_filled == ["Amazon", "Online Shopping", "Finance", "Investing"]

    # narrow_further button should have been clicked once (for the second group)
    narrow_clicks = [
        c for c in mock_click.call_args_list
        if "Narrow further" in str(c)
    ]
    assert len(narrow_clicks) == 1

    assert "targeting_g2_narrow_further" in result.steps_completed
    assert "targeting_g1_t1:Amazon" in result.steps_completed
    assert "targeting_g2_t1:Finance" in result.steps_completed


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

    assert "targeting_g1_t1:Amazon" in result.steps_completed
    assert any("targeting_g1_t2_failed" in s for s in result.steps_completed)
    assert "targeting_g1_t3:Finance" in result.steps_completed
