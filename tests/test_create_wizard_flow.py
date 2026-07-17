"""Tests for Ads Manager create-wizard flow helpers."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from src.fb_ads_rpa.ads_manager_flow import (
    AdsManagerFlow,
    AdsManagerFlowError,
    _is_objective_modal_open,
)
from src.fb_ads_rpa.draft import RpaPublishResult


def _mock_page_with_visible_text(text: str) -> MagicMock:
    page = MagicMock()
    loc = MagicMock()
    loc.count.return_value = 1
    loc.first.is_visible.return_value = True
    page.get_by_text.return_value = loc
    page.frames = []
    page.main_frame = page
    return page


def test_is_objective_modal_open_detects_heading():
    page = _mock_page_with_visible_text("Create new campaign")
    assert _is_objective_modal_open(page) is True


def test_is_objective_modal_open_false_when_hidden():
    page = MagicMock()
    loc = MagicMock()
    loc.count.return_value = 0
    page.get_by_text.return_value = loc
    dlg = MagicMock()
    dlg.count.return_value = 0
    page.get_by_role.return_value = dlg
    page.frames = []
    assert _is_objective_modal_open(page) is False


def test_start_create_wizard_skips_retry_when_modal_already_open():
    flow = AdsManagerFlow.__new__(AdsManagerFlow)
    flow.page = MagicMock()
    flow.nav_timeout = 5000
    flow.action_timeout = 5000
    flow.cfg = {
        "selectors": {
            "create_button": ["text=+ Create"],
            "create_campaign_item": ["text=Campaign"],
            "campaign_objective_leads": ["text=Leads"],
            "campaign_name_input": ["label=Campaign name"],
        }
    }
    flow._shot = MagicMock()
    result = RpaPublishResult(campaign_name="Test")

    def click_side_effect(page, selectors, **kwargs):
        if selectors == ["text=Campaign"]:
            raise AdsManagerFlowError("no menu")

    with (
        patch("src.fb_ads_rpa.ads_manager_flow._wait_for_ads_manager_ready"),
        patch("src.fb_ads_rpa.ads_manager_flow._click_first", side_effect=click_side_effect) as mock_click,
        patch("src.fb_ads_rpa.ads_manager_flow._is_objective_modal_open", side_effect=[True, True]),
        patch("src.fb_ads_rpa.ads_manager_flow._dismiss_ai_guided_setup_dialog", return_value=True),
        patch("src.fb_ads_rpa.ads_manager_flow._click_leads_grid_row", return_value=True),
        patch("src.fb_ads_rpa.ads_manager_flow._is_objective_picker_visible", side_effect=[True, False]),
        patch("src.fb_ads_rpa.ads_manager_flow._is_campaign_wizard_ready", return_value=False),
        patch("src.fb_ads_rpa.ads_manager_flow._select_leads_objective"),
        patch("src.fb_ads_rpa.ads_manager_flow._click_continue_when_enabled"),
        patch.object(flow, "_wait_for_campaign_name_field") as mock_wait,
        patch.object(flow, "_shot", return_value="/tmp/test.png"),
    ):
        def mark_ready(*args, **kwargs):
            result.steps_completed.append("wizard_form_ready")

        mock_wait.side_effect = mark_ready
        flow._start_create_wizard(result)

    assert mock_click.call_count >= 1
    assert mock_click.call_args_list[0][0][1] == ["text=+ Create"]
    assert "create_retry" not in result.steps_completed
    assert "objective_modal_open" in result.steps_completed
    assert "create_retry_skipped_modal_open" in result.steps_completed
    mock_wait.assert_called_once()
