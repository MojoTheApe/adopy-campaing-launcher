"""Regression scenarios from the September release review (D01-D04)."""
from copy import deepcopy
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit

import pandas as pd
import pytest

from src.analysis_agent import morning_service
from src.web import app, cache, goals


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("ACL_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("MY_CM_NAME", "Evgeni Sedin")
    source = tmp_path / "synthetic.xlsx"
    source.touch()
    monkeypatch.setenv("FB_STATS_EXCEL", str(source))
    monkeypatch.setattr(app, "_PERIOD_CACHE", {})
    monkeypatch.setattr(cache, "_CACHE", deepcopy(cache._CACHE))
    cache.cache_clear()
    monkeypatch.setattr(morning_service, "_fallback_accounts_from_csv", lambda cm: [])


def test_period_preserves_selected_buyer_and_goals(enriched_df, accounts_rows):
    base, _, _ = morning_service.load_morning_report(
        enriched_df=enriched_df, my_cm="Other CM", account_rows_cached=accounts_rows)
    goals.save("Other CM", [{"country": "Mexico", "ftd": 5}])
    scoped = app.report_for_period(base, enriched_df, "2026-04-01", "2026-04-30")
    assert scoped["my_cm"] == "Other CM"
    assert {c["country"]: c["my_spend"] for c in scoped["countries"]} == {
        c["country"]: c["my_spend"] for c in base["countries"]}
    goal_view = app._build_goals_view(scoped)
    assert goal_view["cm"] == "Other CM"
    assert goal_view["target"] == 5


@pytest.mark.parametrize("change", ["excel", "accounts"])
def test_rebuild_invalidates_period_with_same_row_count(monkeypatch, change):
    source = {"spend": 10, "account": "first"}
    def load_base(**kwargs):
        df = pd.DataFrame({"Date": ["2026-04-18"], "$ Spend": [source["spend"]]})
        return {"source_excel": "synthetic.xlsx", "my_cm": "Other CM",
                "_account_rows": [source["account"]]}, df, []
    def load_period(**kwargs):
        return {"spend": float(kwargs["enriched_df"]["$ Spend"].sum()),
                "accounts": kwargs["account_rows_cached"]}, None, []
    monkeypatch.setattr(cache, "load_morning_report", load_base)
    monkeypatch.setattr(morning_service, "load_morning_report", load_period)
    base, df, _ = cache.get_cached_report(my_cm="Other CM")
    first = app.report_for_period(base, df, "2026-04-01", "2026-04-30")
    assert app.report_for_period(base, df, "2026-04-01", "2026-04-30") is first
    source["spend" if change == "excel" else "account"] = 100 if change == "excel" else "second"
    cache.cache_clear(keep_excel_df=change == "accounts")
    base, df, _ = cache.get_cached_report(force_rebuild=True, my_cm="Other CM")
    second = app.report_for_period(base, df, "2026-04-01", "2026-04-30")
    assert second == {"spend": source["spend"], "accounts": [source["account"]],
                      "period_from": "2026-04-01", "period_to": "2026-04-30"}
    assert second is not first


def test_rebuild_error_never_returns_unfiltered_numbers(monkeypatch):
    def fail(**kwargs):
        raise ValueError("synthetic calculation failure")
    monkeypatch.setattr(morning_service, "load_morning_report", fail)
    with pytest.raises(RuntimeError, match="selected period"):
        app.report_for_period({"countries": [{"spend": 999}]},
                              pd.DataFrame({"Date": ["2026-04-18"]}),
                              "2026-04-18", "2026-04-18")


def test_account_drill_uses_resolved_workbook_identity(monkeypatch, enriched_df):
    from src.web import account_links
    from src.analysis_agent.section_filters import filter_account_rows
    frame = enriched_df.copy()
    frame["Account Alias"] = "FacebookMKTOBFLX82"
    account_links.link("KLD_BFL_X82", "country", "Mexico")
    view = app._build_account_map_view({"countries": []}, frame, "2026-04-01", "2026-04-30")
    country = view["countries"][0]
    assert country["rows"][0]["has_data"]
    html = app.templates.env.get_template("_account_map_country.html").render(
        mc=country, amap=view, mlx={"A": "Inhouse", "B": "KLD"})
    class Links(HTMLParser):
        def __init__(self):
            super().__init__()
            self.urls = []
        def handle_starttag(self, tag, attrs):
            self.urls += [v for k, v in attrs if k == "hx-get"]
    links = Links()
    links.feed(html)
    query = parse_qs(urlsplit(links.urls[0]).query)
    rows = filter_account_rows(frame, query["country"][0], query["account"][0])
    assert len(rows) == 9
