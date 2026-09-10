# -*- coding: utf-8 -*-
"""acl-357: пострановая разбивка — всё, что останавливается до браузера."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.analysis_agent.country_split import (
    CountrySplit,
    money_and_currency,
    rows_from_cells,
)


# ── money_and_currency ─────────────────────────────────────────────────

def test_dollar_amount_parses_without_guessing_currency():
    # $ пишут за десяток валют — код не выдумывается.
    assert money_and_currency("$1,234.56") == (1234.56, "")


def test_three_letter_code_wins():
    assert money_and_currency("KWD 12.00") == (12.0, "KWD")


def test_euro_sign_maps_to_code():
    assert money_and_currency("€45.50") == (45.5, "EUR")


def test_campaign_count_is_not_money():
    # Ловушка из _first_money: «4» из «4 campaigns» деньгами не считается.
    assert money_and_currency("Results from 4 campaigns")[0] is None


def test_bare_decimal_counts_as_money():
    assert money_and_currency("31.47")[0] == 31.47


def test_empty_and_dash_are_not_money():
    assert money_and_currency("")[0] is None
    assert money_and_currency("—")[0] is None


# ── rows_from_cells ────────────────────────────────────────────────────

HEADER = ["Country", "Reach", "Impressions", "Amount spent", "Cost per result"]


def test_rows_extracted_by_header_words():
    matrix = [
        ["Untitled report"],
        HEADER,
        ["Kuwait", "2,788", "3,162", "$23.89", "$0.04"],
        ["Saudi Arabia", "5,998", "6,740", "KWD 21.61", "$0.45"],
        ["Total results 2/2 rows displayed", "8,845", "9,902", "$45.50", ""],
    ]
    rows, how = rows_from_cells(matrix)
    assert rows == [
        {"country": "Kuwait", "spend": 23.89, "currency": ""},
        {"country": "Saudi Arabia", "spend": 21.61, "currency": "KWD"},
    ]
    assert "header row 1" in how


def test_total_and_service_rows_are_skipped():
    matrix = [
        HEADER,
        ["Total results", "1", "2", "$9.99", ""],
        ["All", "1", "2", "$9.99", ""],
        ["Peru", "1", "2", "$9.99", ""],
    ]
    rows, _ = rows_from_cells(matrix)
    assert [r["country"] for r in rows] == ["Peru"]


def test_country_without_amount_is_skipped_not_zeroed():
    matrix = [HEADER, ["Chile", "0", "0", "—", "—"]]
    rows, _ = rows_from_cells(matrix)
    assert rows == []  # «—» — это не ноль, строки нет вовсе


def test_missing_header_is_a_named_failure():
    rows, how = rows_from_cells([["something"], ["else entirely"]])
    assert rows == []
    assert "not found" in how


def test_column_order_does_not_matter():
    matrix = [
        ["Amount spent", "Country"],
        ["$5.00", "Ecuador"],
    ]
    rows, _ = rows_from_cells(matrix)
    assert rows == [{"country": "Ecuador", "spend": 5.0, "currency": ""}]


def test_short_rows_do_not_crash():
    matrix = [HEADER, ["Kuwait"], []]
    rows, _ = rows_from_cells(matrix)
    assert rows == []


# ── CountrySplit shape ─────────────────────────────────────────────────

def test_split_to_dict_carries_reading_shape():
    s = CountrySplit(account="A", date_from="2026-09-01", date_to="2026-09-10")
    d = s.to_dict()
    # Форма как у SpendReading (acl-108b): кто зовёт, не отличает маршрут.
    for key in ("account", "date_from", "date_to", "source", "state", "how", "error"):
        assert key in d
    assert d["state"] == "failed" and d["rows"] == []
