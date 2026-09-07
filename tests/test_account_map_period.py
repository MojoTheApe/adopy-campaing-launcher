"""Период карты аккаунтов: умолчание — месяц по текущее число, не «всё»."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.web import app as web_app  # noqa: E402


def test_month_to_date_from_the_last_day_in_the_workbook():
    dates = ["2026-08-30", "2026-08-31", "2026-09-01", "2026-09-06"]
    assert web_app._month_to_date(dates) == ("2026-09-01", "2026-09-06")


def test_month_to_date_on_an_empty_workbook_is_blank():
    assert web_app._month_to_date([]) == ("", "")


def test_month_to_date_when_the_last_day_is_the_first():
    assert web_app._month_to_date(["2026-09-01"]) == ("2026-09-01", "2026-09-01")


def _view(**kw):
    import pandas as pd

    df = pd.DataFrame(
        {"Date": ["2026-08-15", "2026-09-01", "2026-09-06"], "Country": ["KW"] * 3}
    )
    return web_app._build_account_map_view({"countries": []}, df, **kw)


def test_nothing_chosen_means_month_to_date():
    v = _view()
    assert (v["date_from"], v["date_to"]) == ("2026-09-01", "2026-09-06")


def test_all_dates_has_to_be_asked_for():
    v = _view(span="all")
    assert (v["date_from"], v["date_to"]) == ("", "")
    assert v["span"] == "all"


def test_an_explicit_period_wins_over_the_default():
    v = _view(date_from="2026-08-01", date_to="2026-08-31")
    assert (v["date_from"], v["date_to"]) == ("2026-08-01", "2026-08-31")


def test_resolve_period_prefers_what_was_chosen():
    assert web_app.resolve_period(None, "2026-08-01", "2026-08-31", "") == (
        "2026-08-01",
        "2026-08-31",
    )


def test_resolve_period_all_means_no_bounds():
    assert web_app.resolve_period(None, "2026-08-01", "", "all") == ("", "")


def test_resolve_period_falls_back_to_month_to_date():
    import pandas as pd

    df = pd.DataFrame({"Date": ["2026-08-15", "2026-09-01", "2026-09-06"]})
    assert web_app.resolve_period(df, "", "", "") == ("2026-09-01", "2026-09-06")


def test_report_for_period_without_a_period_returns_the_same_object():
    import pandas as pd

    report = {"countries": [], "source_excel": "x.xlsx"}
    df = pd.DataFrame({"Date": ["2026-09-01"]})
    assert web_app.report_for_period(report, df, "", "") is report


def test_report_for_period_without_a_dataframe_returns_the_same_object():
    report = {"countries": []}
    assert web_app.report_for_period(report, None, "2026-09-01", "2026-09-06") is report
