"""Период карты аккаунтов: умолчание — месяц по текущее число, не «всё»."""
from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.web import app as web_app  # noqa: E402


def _iso(d):
    return d.isoformat()


def _today():
    from datetime import date

    return date.today()


def test_month_to_date_runs_to_today_even_if_the_workbook_lags():
    """Книга отстала на два дня — период всё равно кончается сегодня.

    Раньше конец подрезался последним днём книги. Выглядело аккуратно, но
    «Yesterday» и «Today» становились невыбираемыми, и снять спенд с аккаунта
    за вчера было нечем — хотя аккаунт про вчера прекрасно знает.
    """
    t = _today()
    first = t.replace(day=1)
    dates = [_iso(first - timedelta(days=1)), _iso(first), _iso(t - timedelta(days=2))]
    assert web_app._month_to_date(dates) == (_iso(first), _iso(t))


def test_month_to_date_falls_back_when_this_month_is_missing():
    """Книга не знает текущий месяц вовсе — открываемся на её последнем.

    Иначе дешборд открылся бы пустым, и причина не была бы видна.
    """
    t = _today()
    prev_end = t.replace(day=1) - timedelta(days=1)
    prev_start = prev_end.replace(day=1)
    dates = [_iso(prev_start), _iso(prev_end)]
    assert web_app._month_to_date(dates) == (_iso(prev_start), _iso(prev_end))


def test_month_to_date_on_an_empty_workbook_is_blank():
    assert web_app._month_to_date([]) == ("", "")


def _view(**kw):
    import pandas as pd

    t = _today()
    first = t.replace(day=1)
    df = pd.DataFrame(
        {
            "Date": [
                _iso(first - timedelta(days=20)),
                _iso(first),
                _iso(t - timedelta(days=2)),
            ],
            "Country": ["KW"] * 3,
        }
    )
    return web_app._build_account_map_view({"countries": []}, df, **kw)


def test_nothing_chosen_means_month_to_date():
    t = _today()
    v = _view()
    assert (v["date_from"], v["date_to"]) == (_iso(t.replace(day=1)), _iso(t))


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

    t = _today()
    first = t.replace(day=1)
    df = pd.DataFrame(
        {"Date": [_iso(first - timedelta(days=20)), _iso(first), _iso(t - timedelta(days=2))]}
    )
    assert web_app.resolve_period(df, "", "", "") == (_iso(first), _iso(t))


def test_report_for_period_without_a_period_returns_the_same_object():
    import pandas as pd

    report = {"countries": [], "source_excel": "x.xlsx"}
    df = pd.DataFrame({"Date": ["2026-09-01"]})
    assert web_app.report_for_period(report, df, "", "") is report


def test_report_for_period_without_a_dataframe_returns_the_same_object():
    report = {"countries": []}
    assert web_app.report_for_period(report, None, "2026-09-01", "2026-09-06") is report
