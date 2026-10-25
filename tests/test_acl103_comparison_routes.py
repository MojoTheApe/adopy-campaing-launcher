"""Real report/route/store boundaries, using temporary synthetic history only."""
from __future__ import annotations

import json

import pytest
from starlette.requests import Request

from src.analysis_agent import account_spend, bi_stats
from src.web import account_links, app as web
from tests.test_acl103_saved_source import SavedPanel
from tests.test_acl103_source_contract import history_frame, isolated_source  # noqa: F401


@pytest.fixture
def comparison_routes(monkeypatch, tmp_path):
    missing = str(tmp_path / "no-map.csv")
    monkeypatch.setattr(account_links, "get_ad_account_map_path", lambda: missing)
    monkeypatch.setattr(web, "get_ad_account_map_path", lambda: missing)
    monkeypatch.setattr(web, "mlx_instance_labels", lambda: {})
    monkeypatch.setattr(account_spend, "_UNSAVED", {})
    monkeypatch.setattr(account_spend, "_RUNNING", set())
    report, frame = history_frame(monkeypatch, tmp_path)
    monkeypatch.setattr(web, "get_cached_report", lambda *a, **k: (report, frame, []))
    forbidden = []
    def no_live(*a, **k):
        forbidden.append((a, k))
        pytest.fail("comparison routes may not start a new external read")
    for owner, name in [(bi_stats, "fetch_raw"), (bi_stats, "refresh_revision"),
                        (account_spend, "start_read")]:
        monkeypatch.setattr(owner, name, no_live)

    def save(label, day, amount):
        account_spend._put(account_spend.SpendReading(
            account=label, date_from=day, date_to=day, state="ok", source="api",
            spend=amount, currency="USD", currency_code="USD", timezone="UTC",
            day_boundary="calendar_day", finished_at="2026-10-18 14:20:00"))

    def setup(mapped=False):
        label = "KLD_BFL_X84" if mapped else "FacebookDoc101"
        if mapped:
            frame["Account Alias"] = "FacebookMKTOBFLX84"
        account_links.link(label, "country", "Mexico")
        save(label, "2026-01-05", 14.0)
        save(label, "2026-10-18", 14.0)
        return label

    def get(start, end, span=""):
        query = f"date_from={start}&date_to={end}&span={span}"
        request = Request({"type": "http", "method": "GET", "path": "/account-map",
                           "query_string": query.encode(), "headers": [],
                           "server": ("testserver", 80), "scheme": "http"})
        response = web.account_map_fragment.__wrapped__(
            request, date_from=start, date_to=end, span=span)
        api = json.loads(web.api_source_vs_account.__wrapped__(date_from=start, date_to=end).body)
        assert response.status_code == 200 and not forbidden
        return response.context["amap"], SavedPanel(response.body.decode()), api
    return setup, get, save


@pytest.mark.parametrize("start,end,amount,source,label", [
    ("", "2026-01-05", 7.0, "Stats workbook", "Through 2026-01-05"),
    ("2026-01-05", "", 19.5, "BI + Stats workbook", "From 2026-01-05 onward"),
])
def test_each_open_bound_matches_primary_rows_without_borrowing_a_single_day(comparison_routes, start, end, amount, source, label):
    setup, get, _save = comparison_routes
    account = setup()
    amap, panel, api = get(start, end)
    primary = next(r for c in amap["countries"] for r in c["rows"] if r["account"] == account)
    saved = amap["saved_source_check"]["rows"][0]
    row = api["rows"][0]
    assert primary["spend"] == saved["source_spend"] == row["source_spend"] == amount
    assert saved["source"]["source"] == row["source"]["source"] == source
    assert (amap["saved_source_check"]["date_from"], amap["saved_source_check"]["date_to"]) == (start, end)
    assert (api["date_from"], api["date_to"]) == (start, end)
    assert label in panel.content()
    for value in [saved, row]:
        assert value["account_spend"] is None and value["gap"] is None
        assert value["comparison_state"] == "missing_account"
    assert "USD 14.00" not in panel.content()


def test_closed_single_day_control_keeps_the_exact_saved_reading(comparison_routes):
    setup, get, _save = comparison_routes
    setup()
    amap, panel, api = get("2026-01-05", "2026-01-05")
    saved = amap["saved_source_check"]["rows"][0]
    for row in [saved, api["rows"][0]]:
        assert (row["source_spend"], row["account_spend"], row["gap"]) == (7.0, 14.0, 7.0)
        assert row["comparison_state"] == "comparable"
    assert "USD 7.00" in panel.content() and "USD 14.00" in panel.content()


def test_maximum_keeps_all_source_dates_and_no_fabricated_account_period(comparison_routes):
    setup, get, _save = comparison_routes
    setup()
    amap, panel, _api = get("", "", "all")
    saved = amap["saved_source_check"]["rows"][0]
    assert saved["source_spend"] == 19.5 and saved["account_spend"] is None and saved["gap"] is None
    assert amap["saved_source_check"]["date_from"] == amap["saved_source_check"]["date_to"] == ""
    assert "All dates" in panel.content()


def test_endpoint_resolves_one_known_canonical_source_alias_and_account_label(comparison_routes):
    setup, get, _save = comparison_routes
    label = setup(mapped=True)
    _amap, _panel, api = get("2026-10-18", "2026-10-18")
    assert len(api["rows"]) == 1, "one naming-rule identity must not split into two missing rows"
    row = api["rows"][0]
    assert row["account"] == "FacebookMKTOBFLX84" and row["reading_account"] == label
    assert (row["source_spend"], row["account_spend"], row["gap"]) == (12.5, 14.0, 1.5)


def test_endpoint_refuses_ambiguous_canonical_readings_without_losing_values(comparison_routes):
    setup, get, save = comparison_routes
    setup(mapped=True)
    save("FacebookMKTOBFLX84", "2026-10-18", 99.0)
    _amap, _panel, api = get("2026-10-18", "2026-10-18")
    assert sorted(r["account_spend"] for r in api["rows"] if r["account_spend"] is not None) == [14.0, 99.0]
    assert [r["source_spend"] for r in api["rows"] if r["source_spend"] is not None] == [12.5]
    assert all(r["comparison_state"] == "ambiguous_account_identity" and r["gap"] is None and not r["differs"] for r in api["rows"])
