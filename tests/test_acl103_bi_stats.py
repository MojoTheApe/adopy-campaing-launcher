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
def _clean_env(monkeypatch):
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


def test_bi_failure_falls_back_to_workbook_with_note(monkeypatch, tmp_path):
    _enable_bi(monkeypatch)

    def boom(cfg=None):
        raise ConnectionError("timed out")

    monkeypatch.setattr(bi_stats, "fetch_raw", boom)
    book = _book(tmp_path / "stats.xlsx")

    df = morning_service._load_enriched_df(str(book))

    assert df.attrs["stats_source"] == "excel"
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
