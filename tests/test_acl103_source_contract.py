"""ACL-103: реальные загрузчик/период/API без BI, профилей и пользовательских БД."""
from __future__ import annotations

import json
from datetime import datetime

import pandas as pd
import pytest
from openpyxl import Workbook

from src.analysis_agent import bi_stats, morning_service
from src.analysis_agent.stats_workbook import STATS_REQUIRED_COLUMNS
from src.web import app as web
from tests.test_acl103_bi_stats import CM_MAP, ROW_KNOWN, _raw


@pytest.fixture(autouse=True)
def isolated_source(monkeypatch, tmp_path):
    monkeypatch.setenv("ACL_CONFIG_DIR", str(tmp_path / "machine-config"))
    for name, value in {
        "BI_MYSQL_HOST": "bi.invalid", "BI_MYSQL_USER": "synthetic-reader",
        "BI_MYSQL_PASSWORD": "synthetic-password", "BI_CURRENCY": "USD",
        "BI_TIMEZONE": "UTC", "BI_DAY_BOUNDARY": "calendar_day",
        "STATS_CURRENCY": "USD", "STATS_TIMEZONE": "UTC",
        "STATS_DAY_BOUNDARY": "calendar_day",
        "BI_ARCHIVE_PATH": str(tmp_path / "archive.sqlite3"),
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("FB_STATS_SOURCE", raising=False)
    monkeypatch.setattr(bi_stats, "load_cm_map", lambda path=None: CM_MAP)
    monkeypatch.setattr(morning_service, "_fallback_accounts_from_csv", lambda cm: [])
    monkeypatch.setattr(morning_service, "fetch_account_rows", lambda **kw: [])
    monkeypatch.setitem(morning_service._WORKBOOK_CACHE, "key", None)
    # Не оставляем новый cached period report между реальными вызовами маршрута.
    monkeypatch.setattr(web, "_PERIOD_CACHE", {})


def history_frame(monkeypatch, tmp_path):
    path = tmp_path / "stats.xlsx"
    book = Workbook()
    sheet = book.active
    sheet.append(STATS_REQUIRED_COLUMNS + ["Date"])
    sheet.append(["Mexico", "FacebookDoc101", "MX_AMZ_All_Image_134_HV_40_22.10",
                  "Image 134", 1, 0, 7.0, "Evgeni Sedin", "Amazon", 0.0,
                  datetime(2026, 1, 5)])
    book.save(path)
    monkeypatch.setattr(bi_stats, "fetch_raw", lambda cfg=None: _raw([ROW_KNOWN]))
    report, frame, _ = morning_service.load_morning_report(
        excel_path=str(path), my_cm="Evgeni Sedin", skip_gsheet=True)
    return report, frame


def endpoint(monkeypatch, frame, readings, start="2026-10-18", end="2026-10-18"):
    monkeypatch.setattr(web, "get_cached_report", lambda *a, **kw: ({}, frame, []))
    monkeypatch.setattr(web, "_live_spend", lambda f="", t="": readings)
    return json.loads(web.api_source_vs_account.__wrapped__(date_from=start, date_to=end).body)


def test_selected_workbook_history_keeps_its_own_source(monkeypatch, tmp_path):
    report, frame = history_frame(monkeypatch, tmp_path)
    selected = web.report_for_period(report, frame, "2026-01-05", "2026-01-05")
    assert selected["source_info"]["source"] == "Stats workbook"
    body = endpoint(monkeypatch, frame, {}, "2026-01-05", "2026-01-05")
    assert body["source"]["source"] == "Stats workbook"
    assert body["rows"][0]["source_spend"] == 7.0


def test_mixed_year_period_names_both_original_sources(monkeypatch, tmp_path):
    report, frame = history_frame(monkeypatch, tmp_path)
    selected = web.report_for_period(report, frame, "2026-01-05", "2026-10-18")
    assert "BI" in selected["source_info"]["source"]
    assert "Stats workbook" in selected["source_info"]["source"]
    body = endpoint(monkeypatch, frame, {}, "2026-01-05", "2026-10-18")
    assert "Stats workbook" in body["source"]["source"]
    assert body["rows"][0]["source_spend"] == 19.5


def test_unknown_currency_does_not_create_a_financial_difference(monkeypatch):
    frame = bi_stats.to_workbook_frame(_raw([ROW_KNOWN]), CM_MAP)
    reading = {"state": "ok", "spend": 14.0, "currency": "", "timezone": "UTC",
               "day_boundary": "calendar_day"}
    row = endpoint(monkeypatch, frame, {"FacebookDoc101": reading})["rows"][0]
    assert (row["source_spend"], row["account_spend"]) == (12.5, 14.0)
    assert row["gap"] is None and row["differs"] is False


def test_different_day_boundaries_are_not_silently_compared(monkeypatch):
    frame = bi_stats.to_workbook_frame(_raw([ROW_KNOWN]), CM_MAP)
    reading = {"state": "ok", "spend": 14.0, "currency": "USD",
               "timezone": "America/New_York", "day_boundary": "calendar_day"}
    row = endpoint(monkeypatch, frame, {"FacebookDoc101": reading})["rows"][0]
    assert (row["source_spend"], row["account_spend"]) == (12.5, 14.0)
    assert row["gap"] is None and row["differs"] is False


def test_actual_api_reading_keeps_iso_currency_through_store_and_comparison(monkeypatch, tmp_path):
    from src.analysis_agent import account_spend as spend, account_spend_api as api
    from tests.test_acl1760_api_read import World, ACCOUNT, D

    world = World(monkeypatch, tmp_path)
    original = world.page.routes["/ACT||"]
    world.page.routes["/ACT||"] = {**original, "body": {**original["body"], "timezone_name": "UTC"}}
    reading = api.read_via_api(ACCOUNT, D, D)
    assert reading.state == "ok" and reading.currency == "$" and reading.spend == 113.87
    spend._put(reading)
    restored = spend.get_reading(ACCOUNT, D, D)
    raw = list(ROW_KNOWN)
    raw[1], raw[2] = pd.Timestamp(D).date(), ACCOUNT
    frame = bi_stats.to_workbook_frame(_raw([raw]), CM_MAP)
    body = endpoint(monkeypatch, frame, {ACCOUNT: restored.to_dict()}, D, D)
    row = body["rows"][0]
    assert row["currency_mismatch"] is False
    assert row["account_currency"] == "USD"
    assert row["gap"] == pytest.approx(101.37) and row["differs"] is True
    assert len(world.paths) == 7, "metadata must use the existing read, without another Graph call"


def test_legacy_archive_additive_migration_preserves_history_and_unknown_units(monkeypatch, tmp_path):
    import sqlite3
    from src.analysis_agent import bi_archive, bi_compare

    path = tmp_path / "archive.sqlite3"
    # Настоящая прежняя схема, без новых метаданных; только временный архив.
    with sqlite3.connect(path) as conn:
        conn.execute('''CREATE TABLE stats_rows ("Date" TEXT NOT NULL, "Country" TEXT,
            "Account Alias" TEXT, "Adset Name" TEXT, "Ad Title" TEXT, "Ad Asset" TEXT,
            "Variant" TEXT, "CM" TEXT, "# Leads" REAL, "# FTDs" REAL, "$ Spend" REAL,
            "$ Net" REAL, "CM_ID" TEXT, source TEXT NOT NULL)''')
        conn.execute('''INSERT INTO stats_rows ("Date", "Account Alias", "CM", "$ Spend", source)
            VALUES ('2026-01-05', 'FacebookDoc101', 'Evgeni Sedin', 7, 'workbook')''')
    monkeypatch.setattr(bi_stats, "fetch_raw", lambda cfg=None: _raw([ROW_KNOWN]))
    rows, _, _ = morning_service._read_stats_rows(str(tmp_path / "absent.xlsx"))
    historic = rows[rows.Date == pd.Timestamp("2026-01-05")]
    assert historic["$ Spend"].sum() == 7 and len(historic) == 1
    info = bi_compare.provenance(frame=historic)
    # ACL-2204: старая строка без валюты — доллары по умолчанию; время/границы дня остаются неизвестными.
    assert info["source"] == "Stats workbook" and info["currency_code"] == "USD" and info["currency_defaulted"]
    assert info["assumed"] and "currency: USD (default)" in info["note"] and "currency: unknown" not in info["note"]
    with sqlite3.connect(path) as conn:
        assert conn.execute('SELECT "Date", "$ Spend", source FROM stats_rows WHERE "Date"=?',
                            ("2026-01-05",)).fetchone() == ("2026-01-05", 7, "workbook")
    assert bi_archive.archive_days() == {"bi": 1, "workbook": 1}


def test_archive_keeps_original_unit_metadata_when_later_bi_window_changes(monkeypatch, tmp_path):
    from src.analysis_agent import bi_archive, bi_compare

    first = bi_stats.normalize(_raw([ROW_KNOWN]))
    bi_archive.ingest_bi_window(first)
    monkeypatch.setenv("BI_CURRENCY", "EUR")
    next_row = list(ROW_KNOWN)
    next_row[1] = datetime(2026, 10, 19).date()
    bi_archive.ingest_bi_window(bi_stats.normalize(_raw([next_row])))
    rows = bi_archive.read_archive()
    assert dict(zip(rows.Date.dt.strftime("%Y-%m-%d"), rows._stats_currency_code)) == {
        "2026-10-18": "USD", "2026-10-19": "EUR"}
    # Сохранённый источник не заменяется текущей глобальной настройкой EUR.
    monkeypatch.setattr(bi_stats, "fetch_raw", lambda cfg=None: (_ for _ in ()).throw(ConnectionError("offline")))
    combined, state, note = morning_service._read_stats_rows(str(tmp_path / "absent.xlsx"))
    assert state == "archive" and "BI unavailable" in note
    assert bi_compare.provenance(frame=combined)["currency_code"] == ""
    selected = bi_compare.period_rows(combined, "2026-10-18", "2026-10-18")
    assert bi_compare.provenance(frame=selected)["currency_code"] == "USD"


def test_archive_failure_fallback_still_keeps_both_real_row_sources(monkeypatch, tmp_path):
    from src.analysis_agent import bi_archive, bi_compare

    history_frame(monkeypatch, tmp_path)
    monkeypatch.setattr(bi_archive, "read_archive", lambda: (_ for _ in ()).throw(OSError("synthetic archive failure")))
    rows, state, note = morning_service._read_stats_rows(str(tmp_path / "stats.xlsx"))
    assert state == "bi" and "Stats archive unavailable" in note
    assert set(rows[bi_compare.SOURCE_COLUMN]) == {"bi", "workbook"}
    assert rows.groupby("Date")["$ Spend"].sum().to_dict() == {
        pd.Timestamp("2026-01-05"): 7.0, pd.Timestamp("2026-10-18"): 12.5}


@pytest.mark.parametrize("metadata_allowed", [True, False])
def test_warehouse_optional_metadata_is_select_only_and_never_guessed(monkeypatch, metadata_allowed):
    from src.analysis_agent import bi_compare

    calls = []
    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, sql, params=None):
            calls.append((sql, params))
            assert sql.startswith("SELECT ")
            self.metadata = "information_schema.COLUMNS" in sql
            if self.metadata and not metadata_allowed:
                raise PermissionError("synthetic metadata denial")
        def fetchall(self):
            if self.metadata:
                # Account metadata deliberately cannot relabel harmonised BI money.
                return [(v,) for v in ["STATS_CURRENCY", "DATE_TIMEZONE", "DAY_BOUNDARY", "ACCOUNT_CURRENCY", "SPEND_CURRENCY"]]
            extra = ["EUR", "Europe/Berlin", "calendar_day"] if metadata_allowed else []
            return [list(ROW_KNOWN) + extra]
    class Connection:
        closed = False
        def cursor(self): return Cursor()
        def close(self): self.closed = True
    conn = Connection()
    monkeypatch.setattr(bi_stats, "_connect", lambda cfg: conn)
    for key in ["BI_CURRENCY", "BI_TIMEZONE", "BI_DAY_BOUNDARY"]:
        monkeypatch.delenv(key, raising=False)
    raw = bi_stats.fetch_raw({"database": "BI_Export", "table": "UF.Export"})
    frame = bi_stats.to_workbook_frame(raw, CM_MAP)
    info = bi_compare.provenance(frame=frame)
    assert frame["$ Spend"].sum() == 12.5 and conn.closed
    assert len(calls) == 2 and calls[0][1] == ("BI_Export", "UF.Export")
    assert "ACCOUNT_CURRENCY" not in calls[1][0]
    assert "SPEND_CURRENCY" not in calls[1][0], "spend-only currency cannot label net revenue"
    if metadata_allowed:
        assert (info["currency_code"], info["timezone"], info["day_boundary"]) == ("EUR", "Europe/Berlin", "calendar_day")
    else:
        assert info["currency_code"] == "USD" and info["currency_defaulted"]
        assert info["timezone"] == "" and info["assumed"] and "currency: unknown" not in info["note"]


def test_old_api_display_sign_does_not_fabricate_an_iso_code(monkeypatch, tmp_path):
    from src.analysis_agent import account_spend as spend

    monkeypatch.setattr(spend, "_store_path", lambda: tmp_path / "readings.json")
    reading = spend.SpendReading(account="FacebookDoc101", date_from="2026-10-18",
        date_to="2026-10-18", source="api", state="ok", spend=14.0, currency="$")
    spend._put(reading)
    restored = spend.get_reading(reading.account, reading.date_from, reading.date_to)
    assert restored.currency == "$" and restored.currency_code == ""
    frame = bi_stats.to_workbook_frame(_raw([ROW_KNOWN]), CM_MAP)
    row = endpoint(monkeypatch, frame, {reading.account: restored.to_dict()})["rows"][0]
    assert row["comparison_state"] == "unknown_currency" and not row["currency_mismatch"]
    assert row["account_spend"] == 14.0 and row["gap"] is None


def test_matching_invalid_timezone_text_does_not_become_a_known_day_basis(monkeypatch):
    monkeypatch.setenv("BI_TIMEZONE", "Europe/Not_A_Timezone")
    frame = bi_stats.to_workbook_frame(_raw([ROW_KNOWN]), CM_MAP)
    reading = {"state": "ok", "spend": 14.0, "currency": "USD",
               "timezone": "Europe/Not_A_Timezone", "day_boundary": "calendar_day"}
    row = endpoint(monkeypatch, frame, {"FacebookDoc101": reading})["rows"][0]
    assert row["comparison_state"] == "unknown_day_boundary" and row["gap"] is None


@pytest.mark.parametrize("currency,label", [("EUR", "EUR 13"), ("", "currency unknown 13")])
def test_source_metric_template_does_not_invent_usd(currency, label):
    rendered = web.templates.env.from_string(
        '{% import "_metrics.html" as mx with context %}'
        '{{ mx.cells(12.5, 5, 1) }} {{ 12.5 | fmt_usd }}'
    ).render(report={"source_info": {"currency_code": currency}})
    assert label in rendered
    assert "$" not in rendered


@pytest.mark.parametrize("currency,label", [("EUR", "EUR 13"), ("", "$13")])
def test_actual_tree_source_money_keeps_known_units_and_defaults_to_usd(monkeypatch, currency, label):
    from src.analysis_agent import bi_compare
    from tests.test_tree_attribution import _book
    frame = bi_compare.annotate_source(_book(), "bi")
    frame[bi_compare.METADATA_COLUMNS["currency_code"]] = currency
    monkeypatch.setattr(web, "get_cached_report", lambda: ({}, frame, []))
    monkeypatch.setattr(web, "workbook_alias_for", lambda *args: "FacebookX")
    body = json.loads(web.account_attribution("FacebookX", "Kuwait", "2026-09-02", "2026-09-04").body)
    assert body["source"]["currency_code"] == (currency or "USD")
    assert body["campaigns"]["KW_camp_A"]["spend"] == label.replace("13", "190")
    assert "currency unknown" not in json.dumps(body["campaigns"])
    assert ("$" in json.dumps(body["campaigns"])) == (not currency)


def test_template_units_are_request_local_and_account_display_is_separate():
    template = web.templates.env.from_string('{{ 12.5|fmt_usd }} / {{ 14|fmt_currency("$") }}')
    assert template.render(report={"source_info": {"currency_code": "EUR"}}) == "EUR 13 / $14.00"
    assert template.render(report={"source_info": {"currency_code": ""}}) == "currency unknown 13 / $14.00"
    assert template.render(report={}) == "$13 / $14.00"


def test_unknown_cm_coverage_is_visible_without_taking_other_teams(monkeypatch, tmp_path):
    from tests.test_acl103_bi_stats import ROW_FOREIGN
    monkeypatch.setattr(bi_stats, "fetch_raw", lambda cfg=None: _raw([ROW_KNOWN, ROW_FOREIGN]))
    report, frame, warnings = morning_service.load_morning_report(
        excel_path=str(tmp_path / "absent.xlsx"), my_cm="Evgeni Sedin", skip_gsheet=True)
    assert list(frame["Account Alias"]) == ["FacebookDoc101"]
    assert any("coverage is incomplete" in note for note in warnings)


@pytest.mark.parametrize("current,previous,label,change", [
    ("EUR", "EUR", "EUR 100", "+100%"),
    ("EUR", "USD", "$100", "—"),
    ("", "", "currency unknown 100", "—"),
])
def test_prebuilt_period_money_uses_earlier_unit_and_only_comparable_change(current, previous, label, change):
    from types import SimpleNamespace
    from src.web import period_compare
    info = lambda code: {"currency_code": code, "timezone": "UTC", "day_boundary": "calendar_day"}
    cells = period_compare.metric_compare({"spend": 200, "leads": 20, "deposits": 4},
                                          {"spend": 100, "leads": 10, "deposits": 2})
    template = web.templates.env.from_string('{% import "_metrics.html" as mx with context %}{{ mx.cmp_cell(cell) }}')
    context = {"report": {"source_info": info(current)},
               "compare": SimpleNamespace(_report={"source_info": info(previous)})}
    shown = template.render(cell=cells[0], **context)
    assert label in shown and f'>{change}</span>' in shown
    count = template.render(cell=cells[1], **context)
    assert ">10</span>" in count and ">+100%</span>" in count


@pytest.mark.parametrize("currency,expected", [("EUR", "EUR 13"), ("", "$13")])
def test_available_account_uses_its_own_activity_window_units(monkeypatch, tmp_path, currency, expected):
    from src.web import account_links
    monkeypatch.setenv("BI_CURRENCY", currency)
    mapped = tmp_path / "synthetic-account-map.csv"
    mapped.write_text("account_name,ad_account_id,profile_id\nFacebookDoc101,act_1,p-synthetic\n")
    missing = str(mapped)
    monkeypatch.setattr(account_links, "get_ad_account_map_path", lambda: missing)
    monkeypatch.setattr(web, "get_ad_account_map_path", lambda: missing)
    report, frame = history_frame(monkeypatch, tmp_path)
    january = web.report_for_period(report, frame, "2026-01-05", "2026-01-05")
    view = web._build_account_map_view(january, frame, "2026-01-05", "2026-01-05")
    assert view["source_info"]["currency_code"] == "USD"
    assert view["activity_window"]["to"] == "2026-10-18"
    assert view["available"][0]["spend"] == 12.5
    shown = web.templates.env.get_template("_account_map_available.html").render(amap=view)
    assert f'{expected} in {view["activity_window"]["days"]}d' in shown
    assert "currency unknown" not in shown
    assert ("$13" in shown) == (not currency)
