"""Tests for Ads Manager create-wizard flow helpers."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from src.fb_ads_rpa.ads_manager_flow import (
    AdsManagerFlow,
    AdsManagerFlowError,
    _is_objective_modal_open,
)
from src.fb_ads_rpa.draft import CampaignDraft, RpaPublishResult


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
    loc.first.is_visible.return_value = False
    page.get_by_text.return_value = loc
    dlg = MagicMock()
    dlg.count.return_value = 0
    dlg.first.is_visible.return_value = False
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


def test_finish_publish_leaves_active_campaign_as_draft():
    flow = AdsManagerFlow.__new__(AdsManagerFlow)
    flow.page = MagicMock()
    flow.cfg = {"selectors": {"publish_button": ["text=Publish"]}}
    flow.action_timeout = 5000
    flow._shot = MagicMock(return_value="shot-99-draft")
    ranks = MagicMock()
    flow._strategy_ranks = ranks
    draft = CampaignDraft(
        name="Campaign",
        country="CL",
        account_label="Account",
        status="ACTIVE",
    )
    result = RpaPublishResult(campaign_name=draft.name)

    with patch("src.fb_ads_rpa.ads_manager_flow._click_first") as mock_click:
        flow._finish_publish(draft, result, dry_run=False)

    mock_click.assert_not_called()
    flow._shot.assert_called_once_with("99_draft", result)
    assert "finish_draft_no_publish" in result.steps_completed
    assert "clicked_publish" not in result.steps_completed
    ranks.log_outcome.assert_called_once_with(
        "finish_draft",
        "hand_off_draft",
        ok=True,
        screenshot="shot-99-draft",
    )


def test_select_conversion_location_does_not_mark_nav_gate_when_section_not_reached():
    flow = AdsManagerFlow.__new__(AdsManagerFlow)
    flow.page = MagicMock()
    flow.cfg = {"selectors": {}}
    flow._shot = MagicMock(return_value="shot")
    ranks = MagicMock()
    flow._strategy_ranks = ranks
    result = RpaPublishResult(campaign_name="Campaign")

    with (
        patch.object(flow, "_ensure_ad_set_level", return_value=True),
        patch("src.fb_ads_rpa.ads_manager_flow.close_open_dropdown"),
        patch("src.fb_ads_rpa.ads_manager_flow.wait_editor_saved"),
        patch("src.fb_ads_rpa.ads_manager_flow.scroll_to_section", return_value=False),
        patch("src.fb_ads_rpa.ads_manager_flow.is_website_conversion_selected", return_value=True),
    ):
        flow._select_conversion_location_website(result)

    ranks.mark_next_step_ok.assert_not_called()
    assert "conversion_location_website_already" in result.steps_completed


def test_conversion_location_ok_recognizes_teach_mode_success():
    flow = AdsManagerFlow.__new__(AdsManagerFlow)
    result = RpaPublishResult(campaign_name="Campaign")
    result.steps_completed.extend(
        [
            "teach_mode_website_waiting",
            "conversion_location_website_teach",
            "teach_website_captured",
        ]
    )

    assert flow._conversion_location_ok(result) is True


def test_conversion_location_log_skips_failure_after_teach_mode_success():
    flow = AdsManagerFlow.__new__(AdsManagerFlow)
    ranks = MagicMock()
    flow._strategy_ranks = ranks
    result = RpaPublishResult(campaign_name="Campaign")
    result.steps_completed.extend(
        [
            "teach_mode_website_waiting",
            "conversion_location_website_teach",
            "teach_website_captured",
        ]
    )

    flow._log_conversion_location_website_outcome(
        result,
        result.steps_completed,
        ok=False,
        screenshot="fail-shot",
    )

    ranks.log_outcome.assert_not_called()
