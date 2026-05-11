"""Unit tests for GSheet account status parsing and Morning Report section 6–7 rules."""
from __future__ import annotations

from src.analysis_agent.gsheet_accounts import (
    format_account_status_todo_cell,
    is_omitted_from_account_statuses_table,
    parse_account_rows,
    _select_rightmost_sheet,
)
from src.analysis_agent.morning_report import (
    _offer_abbr,
    format_open_recommendations_for_status_table,
)


def test_omitted_statuses_not_in_table() -> None:
    assert is_omitted_from_account_statuses_table("Account Disabled")
    assert is_omitted_from_account_statuses_table("account restricted — review")
    assert not is_omitted_from_account_statuses_table("Video selfie")
    assert not is_omitted_from_account_statuses_table("expand")


def test_format_todo_video_selfie_unchanged() -> None:
    assert (
        format_account_status_todo_cell("Video selfie", "should be ignored")
        == "Video selfie"
    )


def test_format_todo_appends_column_k() -> None:
    assert (
        format_account_status_todo_cell(
            "expand",
            "Banco + Trading_IOS + cr1; Banco + FX_IOS + cr2",
        )
        == "expand | Banco + Trading_IOS + cr1; Banco + FX_IOS + cr2"
    )


def test_rightmost_tab_uses_max_index_not_list_order() -> None:
    """API may return sheets[] in an order that does not match UI left-to-right."""
    sheets = [
        {"properties": {"title": "WrongIfLast", "index": 0}},
        {"properties": {"title": "Today", "index": 2}},
        {"properties": {"title": "Middle", "index": 1}},
    ]
    title, idx = _select_rightmost_sheet(sheets)
    assert title == "Today"
    assert idx == 2


def test_format_open_recs_status_table_matches_section9_shape() -> None:
    recs = [
        {
            "offer": "Amazon",
            "targeting": "Day",
            "creatives": [{"name": "ccblc - pro"}],
            "below_benchmark": False,
        },
        {
            "offer": "Amazon",
            "targeting": "PF/IOS/HV",
            "creatives": [{"name": "Vid 700 - pro"}],
            "below_benchmark": True,
        },
    ]
    s = format_open_recommendations_for_status_table(recs)
    assert s == "Amz/Day/ccblc - pro; Amz/PF/IOS/HV/Vid 700 - pro⚠"


def test_format_open_recs_truncates_long_labels() -> None:
    recs = [
        {
            "offer": "Trader AI",
            "targeting": "VeryLongTargetingNameHere",
            "creatives": [{"name": "x" * 40}],
            "below_benchmark": False,
        },
    ]
    s = format_open_recommendations_for_status_table(recs, target_max=10, creative_max=12)
    assert s == "Trl/VeryLongT…/xxxxxxxxxxx…"


def test_kuwait_placeholder_offer_uses_trolley_slug() -> None:
    assert _offer_abbr("-", country_name="Kuwait") == "Trl"


def test_parse_reads_column_k() -> None:
    raw = [
        ["Evgeni", "GT", "Facebook_x GT", "100", "", "", "", "", "Trl", "expand", "A + T + C; A2 + T2 + C2"],
    ]
    rows = parse_account_rows(raw)
    assert len(rows) == 1
    assert rows[0]["to_do"] == "expand"
    assert rows[0]["to_do_extra"] == "A + T + C; A2 + T2 + C2"
