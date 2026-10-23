"""ACL-103: статистика из BI (MySQL) в схеме Stats-книги, с фолбэком на книгу."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest
from openpyxl import Workbook

from src.analysis_agent import bi_stats, morning_service
from src.analysis_agent.stats_workbook import STATS_REQUIRED_COLUMNS
from src.web import cache

RAW_COLS = ["CM_ID", *bi_stats.COLUMN_MAP]


def _raw(rows):
    return pd.DataFrame(rows, columns=RAW_COLS)


ROW_KNOWN = ["1106", date(2026, 10, 18), "FacebookDoc101", "MX_AMZ_All_Image_134_HV_40_22.10",
             "Image 134", "Amazon", "Mexico", "21701_es", 5, 1, Decimal("12.50"), Decimal("200.00")]
ROW_UNKNOWN_CM = ["Unknown", date(2026, 10, 18), "FacebookDoc101", "Unknown", "Unknown", "Unknown",
                  "Mexico", "Unknown", 2, 0, Decimal("3.00"), Decimal("0.00")]
ROW_FOREIGN = ["2101", date(2026, 10, 18), "Taboola_1", "Unknown", "Unknown", "Unknown",
               "Chile", "Unknown", 0, 0, Decimal("9.99"), None]

CM_MAP = {"1106": "Evgeni Sedin"}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    monkeypatch.setenv("BI_ARCHIVE_PATH", str(tmp_path / "archive.sqlite3"))
    monkeypatch.setitem(morning_service._WORKBOOK_CACHE, "key", None)
    for name in ("BI_MYSQL_HOST", "BI_MYSQL_USER", "BI_MYSQL_PASSWORD", "FB_STATS_SOURCE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setitem(bi_stats._REV, "value", None)
    monkeypatch.setitem(bi_stats._REV, "checked", 0.0)


def _enable_bi(monkeypatch):
    monkeypatch.setenv("BI_MYSQL_HOST", "bi.invalid")
    monkeypatch.setenv("BI_MYSQL_USER", "reader")
    monkeypatch.setenv("BI_MYSQL_PASSWORD", "x")


def test_rows_take_workbook_schema_and_types():
    df = bi_stats.to_workbook_frame(_raw([ROW_KNOWN]), CM_MAP)

    assert set(STATS_REQUIRED_COLUMNS) <= set(df.columns)
    row = df.iloc[0]
    assert row["CM"] == "Evgeni Sedin"
    assert row["Account Alias"] == "FacebookDoc101"
    assert row["Ad Asset"] == "Amazon"
    assert row["$ Spend"] == pytest.approx(12.5)
    assert row["# FTDs"] == 1
    assert row["Date"] == pd.Timestamp("2026-10-18")
    assert df["$ Spend"].dtype == "float64" and df["# Leads"].dtype == "int64"


def test_unknown_cm_inherits_account_manager_and_foreign_teams_are_dropped():
    raw = _raw([ROW_KNOWN, ROW_UNKNOWN_CM, ROW_FOREIGN])

    # Как в книге: только менеджеры из карты (плюс Unknown их аккаунтов).
    assert list(bi_stats.to_workbook_frame(raw, CM_MAP)["CM"]) == ["Evgeni Sedin", "Evgeni Sedin"]

    df = bi_stats.to_workbook_frame(raw, CM_MAP, keep_unmapped=True)
    assert list(df["CM"]) == ["Evgeni Sedin", "Evgeni Sedin", "CM 2101"]
    # Пустой NET — ноль, а не NaN: иначе итоги страны становятся NaN.
    assert df["$ Net"].iloc[2] == 0.0


def test_foreign_code_on_shared_account_is_not_ours():
    foreign_on_ours = ["2101", *ROW_KNOWN[1:]]
    df = bi_stats.to_workbook_frame(_raw([ROW_KNOWN, foreign_on_ours]), CM_MAP)
    assert list(df["CM"]) == ["Evgeni Sedin"]


def test_cm_map_file_is_codes_to_names(tmp_path: Path):
    path = tmp_path / "map.json"
    path.write_text('{"_comment": "x", "codes": {"1106": "Evgeni Sedin", "9": ""}}', encoding="utf-8")
    assert bi_stats.load_cm_map(path) == {"1106": "Evgeni Sedin"}
    assert bi_stats.load_cm_map(tmp_path / "missing.json") == {}
    # Карта в репо покрывает менеджеров команды.
    assert "Evgeni Sedin" in bi_stats.load_cm_map().values()


def test_bi_enabled_needs_host_user_password_and_respects_excel_override(monkeypatch):
    assert not bi_stats.bi_enabled()
    monkeypatch.setenv("BI_MYSQL_PASSWORD", "x")
    assert not bi_stats.bi_enabled()  # адрес и логин не зашиты в код
    _enable_bi(monkeypatch)
    assert bi_stats.bi_enabled()
    monkeypatch.setenv("FB_STATS_SOURCE", "excel")
    assert not bi_stats.bi_enabled()


def test_table_name_with_dot_is_one_identifier():
    ref = bi_stats._table_ref({"database": "BI_Export", "table": "UF.Export"})
    assert ref == "`BI_Export`.`UF.Export`"


def _book(path: Path) -> Path:
    wb = Workbook()
    ws = wb.active
    ws.append(STATS_REQUIRED_COLUMNS + ["Date"])
    ws.append(["Mexico", "FacebookDoc101", "MX_AMZ_All_Image_134_HV_40_22.10", "Image 134",
               1, 0, 4.0, "Evgeni Sedin", "Amazon", 0.0, datetime(2026, 10, 18)])
    wb.save(path)
    return path


def test_bi_is_read_first_when_enabled(monkeypatch, tmp_path):
    _enable_bi(monkeypatch)
    monkeypatch.setattr(bi_stats, "fetch_raw", lambda cfg=None: _raw([ROW_KNOWN]))
    monkeypatch.setattr(bi_stats, "load_cm_map", lambda path=None: CM_MAP)

    df = morning_service._load_enriched_df(str(tmp_path / "absent.xlsx"))

    assert df.attrs["stats_source"] == "bi"
    assert df.attrs["stats_note"] is None
    assert df["$ Spend"].sum() == pytest.approx(12.5)


def test_bi_failure_shows_saved_days_with_note(monkeypatch, tmp_path):
    _enable_bi(monkeypatch)

    def boom(cfg=None):
        raise ConnectionError("timed out")

    monkeypatch.setattr(bi_stats, "fetch_raw", boom)
    book = _book(tmp_path / "stats.xlsx")

    df = morning_service._load_enriched_df(str(book))

    # Архив пуст, книга импортирована в него — показаны её дни; Refresh повторит BI.
    assert df.attrs["stats_source"] == "archive"
    assert "BI unavailable" in df.attrs["stats_note"]
    assert df["$ Spend"].sum() == pytest.approx(4.0)


def test_bi_failure_without_workbook_is_an_error(monkeypatch, tmp_path):
    _enable_bi(monkeypatch)
    monkeypatch.setattr(bi_stats, "fetch_raw", lambda cfg=None: (_ for _ in ()).throw(OSError("down")))
    # FileNotFoundError: страницы показывают «нет данных», а не 500.
    with pytest.raises(FileNotFoundError, match="BI unavailable"):
        morning_service._load_enriched_df(str(tmp_path / "absent.xlsx"))


def test_cache_key_follows_bi_revision_without_network(monkeypatch, tmp_path):
    book = str(_book(tmp_path / "stats.xlsx"))
    excel_only = cache._current_key(book)
    assert excel_only[0] == book

    _enable_bi(monkeypatch)
    # Ревизия ещё не известна — отчёт «не готов», а не построен по книге.
    assert cache._current_key(book) is None

    calls = []
    monkeypatch.setattr(bi_stats, "fetch_revision", lambda cfg=None: calls.append(1) or "2026-10-19 11:11:22|12015")
    assert bi_stats.refresh_revision() == "2026-10-19 11:11:22|12015"
    assert bi_stats.refresh_revision() == "2026-10-19 11:11:22|12015"
    assert len(calls) == 1  # TTL: второй раз в сеть не ходим
    bi_stats.refresh_revision(force=True)
    assert len(calls) == 2

    assert cache._current_key(book) == ("bi", "2026-10-19 11:11:22|12015", excel_only)


def test_revision_survives_network_error(monkeypatch):
    def down(cfg=None):
        raise OSError("down")

    monkeypatch.setattr(bi_stats, "fetch_revision", down)
    assert bi_stats.refresh_revision() == "unavailable"
    monkeypatch.setitem(bi_stats._REV, "value", "r1")
    assert bi_stats.refresh_revision(force=True) == "r1"


def _book_two_days(path: Path) -> Path:
    wb = Workbook()
    ws = wb.active
    ws.append(STATS_REQUIRED_COLUMNS + ["Date"])
    for day, spend in ((datetime(2026, 1, 5), 7.0), (datetime(2026, 10, 18), 4.0)):
        ws.append(["Mexico", "FacebookDoc101", "MX_AMZ_All_Image_134_HV_40_22.10", "Image 134",
                   1, 0, spend, "Evgeni Sedin", "Amazon", 0.0, day])
    wb.save(path)
    return path


def test_workbook_supplies_history_for_days_bi_does_not_have(monkeypatch, tmp_path):
    _enable_bi(monkeypatch)
    monkeypatch.setattr(bi_stats, "fetch_raw", lambda cfg=None: _raw([ROW_KNOWN]))
    monkeypatch.setattr(bi_stats, "load_cm_map", lambda path=None: CM_MAP)
    book = _book_two_days(tmp_path / "stats.xlsx")

    df = morning_service._load_enriched_df(str(book))

    by_day = df.groupby("Date")["$ Spend"].sum().to_dict()
    # 05.01 — только в книге; 18.10 есть в BI, книжные 4.0 за этот день отброшены.
    assert by_day == {pd.Timestamp("2026-01-05"): 7.0, pd.Timestamp("2026-10-18"): 12.5}
    assert df.attrs["stats_source"] == "bi"


def test_workbook_is_read_once_per_file_version(monkeypatch, tmp_path):
    book = str(_book_two_days(tmp_path / "stats.xlsx"))
    calls = []
    real = morning_service.read_stats_workbook
    monkeypatch.setattr(morning_service, "read_stats_workbook", lambda p: calls.append(p) or real(p))
    monkeypatch.setitem(morning_service._WORKBOOK_CACHE, "key", None)

    morning_service._read_workbook_cached(book)
    morning_service._read_workbook_cached(book)
    assert len(calls) == 1


ROW_DAY = lambda day, spend, code="1106": [code, day, "FacebookDoc101", "MX_AMZ_All_Image_134_HV_40_22.10",  # noqa: E731
                                          "Image 134", "Amazon", "Mexico", "21701_es", 1, 0, Decimal(spend), Decimal("0")]


def test_archive_keeps_days_that_left_the_bi_window(monkeypatch, tmp_path):
    _enable_bi(monkeypatch)
    monkeypatch.setattr(bi_stats, "load_cm_map", lambda path=None: CM_MAP)
    windows = iter([
        _raw([ROW_DAY(date(2026, 10, 13), "5"), ROW_DAY(date(2026, 10, 14), "6")]),
        # Следующий день: 13.10 выпал из окна, 14.10 пересчитан, пришёл 15.10.
        _raw([ROW_DAY(date(2026, 10, 14), "9"), ROW_DAY(date(2026, 10, 15), "7")]),
    ])
    monkeypatch.setattr(bi_stats, "fetch_raw", lambda cfg=None: next(windows))
    absent = str(tmp_path / "absent.xlsx")

    morning_service._read_stats_rows(absent)
    rows, source, note = morning_service._read_stats_rows(absent)

    by_day = rows.groupby("Date")["$ Spend"].sum().to_dict()
    assert by_day == {pd.Timestamp("2026-10-13"): 5.0, pd.Timestamp("2026-10-14"): 9.0,
                      pd.Timestamp("2026-10-15"): 7.0}
    assert source == "bi" and note is None
    assert set(rows["CM"]) == {"Evgeni Sedin"}


def test_workbook_imported_once_and_never_over_bi_days(monkeypatch, tmp_path):
    from src.analysis_agent import bi_archive

    _enable_bi(monkeypatch)
    monkeypatch.setattr(bi_stats, "load_cm_map", lambda path=None: CM_MAP)
    monkeypatch.setattr(bi_stats, "fetch_raw", lambda cfg=None: _raw([ROW_KNOWN]))
    book = str(_book_two_days(tmp_path / "stats.xlsx"))
    reads = []
    real = morning_service.read_stats_workbook
    monkeypatch.setattr(morning_service, "read_stats_workbook", lambda p: reads.append(p) or real(p))

    rows, _, _ = morning_service._read_stats_rows(book)
    morning_service._read_stats_rows(book)

    assert len(reads) == 1  # та же версия файла — не перечитывается
    by_day = rows.groupby("Date")["$ Spend"].sum().to_dict()
    assert by_day == {pd.Timestamp("2026-01-05"): 7.0, pd.Timestamp("2026-10-18"): 12.5}
    assert bi_archive.archive_days() == {"bi": 1, "workbook": 1}


def test_cm_map_change_applies_to_archived_days(monkeypatch, tmp_path):
    _enable_bi(monkeypatch)
    monkeypatch.setattr(bi_stats, "fetch_raw", lambda cfg=None: _raw([ROW_DAY(date(2026, 10, 13), "5", "4006")]))
    absent = str(tmp_path / "absent.xlsx")
    monkeypatch.setattr(bi_stats, "load_cm_map", lambda path=None: {})
    rows, _, _ = morning_service._read_stats_rows(absent)
    assert rows.empty  # код ещё не в карте — строка не наша
    monkeypatch.setattr(bi_stats, "load_cm_map", lambda path=None: {"4006": "Yinon Avraham"})
    rows, _, _ = morning_service._read_stats_rows(absent)
    assert list(rows["CM"]) == ["Yinon Avraham"]


# --- Review of PR #1031, B1: BI host / login never reach logs, page, error.html or Sentry ---

BI_HOST = "bi-secret-host.example.invalid"
BI_USER = "bi_secret_reader"
BI_PASSWORD = "bi-secret-pass-77"
LEAKS = (BI_HOST, BI_USER, BI_PASSWORD)


class _FakeOperationalError(Exception):
    """Как pymysql.err.OperationalError: args[0] — числовой код, текст с адресом."""


def _enable_secret_bi(monkeypatch):
    monkeypatch.setenv("BI_MYSQL_HOST", BI_HOST)
    monkeypatch.setenv("BI_MYSQL_USER", BI_USER)
    monkeypatch.setenv("BI_MYSQL_PASSWORD", BI_PASSWORD)


def _failing_fetch(code, message):
    def boom(cfg=None):
        raise _FakeOperationalError(code, message)

    return boom


DRIVER_MESSAGES = [
    (2003, f"Can't connect to MySQL server on '{BI_HOST}' (timed out)"),
    (1045, f"Access denied for user '{BI_USER}'@'10.1.2.3' (using password: YES)"),
]


def _assert_clean(text):
    for secret in LEAKS:
        assert secret not in text


@pytest.mark.parametrize("code,message", DRIVER_MESSAGES)
def test_bi_failure_note_and_stderr_carry_no_host_or_login(monkeypatch, tmp_path, capsys, code, message):
    _enable_secret_bi(monkeypatch)
    monkeypatch.setattr(bi_stats, "fetch_raw", _failing_fetch(code, message))
    book = _book(tmp_path / "stats.xlsx")

    df = morning_service._load_enriched_df(str(book))
    # Второе чтение: архив уже есть, BI снова не отвечает — это ветка с печатью в stderr.
    rows, _, note = morning_service._read_stats_rows(str(book))

    captured = capsys.readouterr()
    for text in (df.attrs["stats_note"], note, captured.err, captured.out):
        _assert_clean(text or "")
    # Диагностика осталась: класс и числовой код, оператору хватает.
    assert f"_FakeOperationalError {code}" in df.attrs["stats_note"]
    assert "showing saved days" in df.attrs["stats_note"]


@pytest.mark.parametrize("code,message", DRIVER_MESSAGES)
def test_bi_failure_error_page_carries_no_host_or_login(monkeypatch, tmp_path, code, message):
    from starlette.requests import Request

    from src.web import app as web

    _enable_secret_bi(monkeypatch)
    monkeypatch.setattr(bi_stats, "fetch_raw", _failing_fetch(code, message))
    absent = str(tmp_path / "absent.xlsx")
    with pytest.raises(bi_stats.BIUnavailable) as raised:
        morning_service._load_enriched_df(absent)
    _assert_clean(str(raised.value))

    def unavailable(*args, **kwargs):
        raise raised.value

    monkeypatch.setattr(web, "get_cached_report", unavailable)
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": [],
                       "query_string": b"", "server": ("testserver", 80), "scheme": "http"})
    response = web._render_report(request)

    html = response.body.decode("utf-8")
    assert "No data file" in html
    _assert_clean(html)


def test_report_warning_for_failed_bi_has_no_host_or_login(monkeypatch, tmp_path):
    _enable_secret_bi(monkeypatch)
    monkeypatch.setattr(bi_stats, "fetch_raw", _failing_fetch(*DRIVER_MESSAGES[1]))
    df = morning_service._load_enriched_df(str(_book(tmp_path / "stats.xlsx")))
    # load_morning_report добавляет stats_note в warnings как есть (render: report.html).
    assert df.attrs["stats_note"]
    _assert_clean(df.attrs["stats_note"])


def test_describe_error_is_class_and_code_only():
    text = bi_stats.describe_error(_FakeOperationalError(*DRIVER_MESSAGES[0]))
    assert text == "_FakeOperationalError 2003"
    assert bi_stats.describe_error(OSError("host bi.x refused")) == "OSError"


def test_shared_scrubber_hides_configured_bi_values(monkeypatch):
    from src.fb_ads_rpa import run_telemetry

    _enable_secret_bi(monkeypatch)
    text = f"Can't connect to MySQL server on '{BI_HOST}'; user '{BI_USER.upper()}'@x pw {BI_PASSWORD}"
    out = run_telemetry.scrub(text)
    _assert_clean(out.lower())
    assert BI_USER.upper() not in out
    # без настроенного BI ничего лишнего не прячется
    monkeypatch.delenv("BI_MYSQL_HOST")
    assert run_telemetry.scrub(f"on '{BI_HOST}'") == f"on '{BI_HOST}'"


def test_sentry_event_with_bi_driver_error_has_no_host_or_login(monkeypatch):
    from src import acl_sentry

    _enable_secret_bi(monkeypatch)
    event = {
        "message": DRIVER_MESSAGES[0][1],
        "exception": {"values": [{"type": "OperationalError", "value": DRIVER_MESSAGES[1][1]}]},
        "breadcrumbs": {"values": [{"message": f"connect {BI_HOST} as {BI_USER}"}]},
    }
    sent = acl_sentry._before_send(event, {})
    assert sent is not None
    _assert_clean(repr(sent))


# --- ACL-103d/e/f: currency and day boundary, source versus account figures ---

def test_bi_figures_carry_an_explicit_assumed_currency_and_day_boundary(monkeypatch):
    from src.analysis_agent import bi_compare

    monkeypatch.delenv("BI_ASSUMPTIONS_CONFIRMED", raising=False)
    monkeypatch.delenv("BI_CURRENCY", raising=False)
    info = bi_compare.provenance("bi")
    assert info["currency"] == "USD" and info["assumed"] is True
    assert "assumption" in info["note"] and "day boundary" in info["note"]
    monkeypatch.setenv("BI_ASSUMPTIONS_CONFIRMED", "1")
    monkeypatch.setenv("BI_CURRENCY", "eur")
    confirmed = bi_compare.provenance("bi")
    assert confirmed["currency"] == "EUR" and confirmed["assumed"] is False
    assert "assumption" not in confirmed["note"]


def test_report_carries_source_info_and_warns_while_assumed(monkeypatch, tmp_path):
    _enable_bi(monkeypatch)
    monkeypatch.delenv("BI_ASSUMPTIONS_CONFIRMED", raising=False)
    monkeypatch.setattr(bi_stats, "fetch_raw", lambda cfg=None: _raw([ROW_KNOWN]))
    monkeypatch.setattr(bi_stats, "load_cm_map", lambda path=None: CM_MAP)
    report, _df, warnings = morning_service.load_morning_report(
        excel_path=str(tmp_path / "absent.xlsx"), skip_gsheet=True)
    assert report["source_info"]["source"] == "BI" and report["source_info"]["assumed"] is True
    assert any("assumption" in w for w in warnings)


def test_source_and_account_spend_are_shown_separately(monkeypatch):
    from src.analysis_agent import bi_compare

    monkeypatch.delenv("BI_CURRENCY", raising=False)
    df = bi_stats.to_workbook_frame(_raw([ROW_KNOWN, ROW_DAY(date(2026, 10, 19), "3")]), CM_MAP)
    source = bi_compare.source_spend_by_account(df, "2026-10-18", "2026-10-18")
    assert source == {"FacebookDoc101": 12.5}
    readings = {
        "FacebookDoc101": {"state": "ok", "spend": 14.0, "currency": "USD"},
        "Facebook_Eur": {"state": "ok", "spend": 5.0, "currency": "EUR"},
        "Facebook_Busy": {"state": "running", "spend": None, "currency": ""},
    }
    rows = {r["account"]: r for r in bi_compare.compare_spend({**source, "Facebook_Eur": 4.0, "Facebook_Busy": 1.0}, readings)}
    assert rows["FacebookDoc101"]["source_spend"] == 12.5
    assert rows["FacebookDoc101"]["account_spend"] == 14.0
    assert rows["FacebookDoc101"]["gap"] == 1.5 and rows["FacebookDoc101"]["differs"] is True
    assert rows["Facebook_Eur"]["currency_mismatch"] is True and rows["Facebook_Eur"]["gap"] is None
    assert rows["Facebook_Busy"]["account_spend"] is None and rows["Facebook_Busy"]["gap"] is None


def test_source_vs_account_endpoint(monkeypatch, tmp_path):
    from src.web import app as web

    df = bi_stats.to_workbook_frame(_raw([ROW_KNOWN]), CM_MAP)
    df.attrs["stats_source"] = "bi"
    monkeypatch.setattr(web, "get_cached_report", lambda *a, **k: ({}, df, []))
    monkeypatch.setattr(web, "_live_spend", lambda f="", t="": {
        "FacebookDoc101": {"state": "ok", "spend": 12.5, "currency": "USD"}})
    response = web.api_source_vs_account.__wrapped__(date_from="2026-10-18", date_to="2026-10-18") \
        if hasattr(web.api_source_vs_account, "__wrapped__") else None
    assert response is not None
    import json
    body = json.loads(response.body)
    assert body["source"]["source"] == "BI"
    row = body["rows"][0]
    assert (row["source_spend"], row["account_spend"], row["gap"], row["differs"]) == (12.5, 12.5, 0.0, False)


def test_mapping_document_names_every_column_and_the_assumption():
    doc = (Path(__file__).resolve().parents[1] / "docs" / "ACL-103-bi-field-mapping.md").read_text(encoding="utf-8")
    for bi_col, book_col in bi_stats.COLUMN_MAP.items():
        assert f"`{bi_col}`" in doc and book_col in doc
    assert "BI_ASSUMPTIONS_CONFIRMED" in doc and "Granularity" in doc
