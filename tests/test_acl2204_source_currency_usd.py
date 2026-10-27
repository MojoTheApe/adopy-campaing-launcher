"""ACL-2204: BI и книга статистики без объявленной валюты показываются в долларах."""
import pandas as pd

from src.analysis_agent import bi_compare, money_format
from src.web import source_display

CUR = bi_compare.METADATA_COLUMNS["currency_code"]


def _frame(*currencies, source="bi"):
    frame = pd.DataFrame({"Account Alias": ["FacebookDoc101"] * len(currencies),
                          "$ Spend": [10.0] * len(currencies)})
    frame = bi_compare.annotate_source(frame, source)
    frame[CUR] = list(currencies)
    return frame


def test_undeclared_currency_is_usd_and_notice_does_not_flag_it():
    for src in ("bi", "workbook"):
        info = bi_compare.provenance(frame=_frame("", "", source=src))
        assert info["currency_code"] == "USD" and info["currency"] == "USD"
        assert info["currency_defaulted"] is True
        assert "currency: unknown" not in info["note"] and "currency: USD (default)" in info["note"]
        assert "time zone: Asia/Jerusalem (default)" in info["note"] and not info["assumed"]  # ACL-2248
        assert source_display.money(12266, info) == "$12,266"


def test_no_rows_and_missing_column_default_to_usd():
    assert bi_compare.provenance(frame=pd.DataFrame())["currency_code"] == "USD"
    bare = bi_compare.annotate_source(pd.DataFrame({"$ Spend": [1.0]}), "bi").drop(columns=[CUR], errors="ignore")
    assert bi_compare.provenance(frame=bare)["currency_code"] == "USD"


def test_explicit_declaration_wins_and_keeps_its_own_code():
    info = bi_compare.provenance(frame=_frame("EUR", "EUR"))
    assert info["currency_code"] == "EUR" and info["currency_defaulted"] is False
    assert "currency: EUR;" in info["note"]
    assert source_display.money(585, info) == "EUR 585"
    assert bi_compare.provenance(frame=_frame("USD", ""))["currency_code"] == "USD"


def test_different_currencies_are_never_merged_into_usd():
    info = bi_compare.provenance(frame=_frame("EUR", ""))
    assert info["currency_code"] == "" and info["currency"] == "unknown"
    assert "currency: unknown" in info["note"] and "different currencies" in info["note"]
    assert source_display.money(585, info) == "currency unknown 585"


def test_per_account_meta_read_with_empty_currency_keeps_its_wording():
    assert money_format.fmt_currency(44.17, "") == "currency unknown 44.17"
    assert money_format.fmt_summary_amount(44.17, "") == "44.17"
    source = {"FacebookDoc101": 10.0}
    reading = {"FacebookDoc101": {"state": "ok", "spend": 10.0, "currency": "", "timezone": "UTC",
                                  "day_boundary": "calendar_day"}}
    row = bi_compare.compare_spend(source, reading, source_currency="")[0]
    assert row["source_currency"] == "USD" and row["comparison_state"] == "unknown_currency"
    assert row["gap"] is None
    eur = {"FacebookDoc101": dict(reading["FacebookDoc101"], currency="EUR")}
    assert bi_compare.compare_spend(source, eur, source_currency="")[0]["comparison_state"] == "currency_mismatch"


def _mixed(bi, workbook):
    """Архив владельца: у строк BI валюта проставлена при импорте, у книги — пусто."""
    bi_rows = _frame(*bi, source="bi")
    book_rows = _frame(*workbook, source="workbook")
    return pd.concat([bi_rows, book_rows], ignore_index=True)


def test_bi_usd_plus_workbook_without_currency_is_usd():
    for empty in ("", None, float("nan")):
        info = bi_compare.provenance(frame=_mixed(["USD", "USD"], [empty, empty]))
        assert info["currency_code"] == "USD", empty
        assert info["currency_defaulted"] is False  # BI объявила USD явно
        assert "currency: USD;" in info["note"] and "USD (default)" not in info["note"]
        assert source_display.money(585, info) == "$585"


def test_usd_plus_eur_is_unknown_and_never_summed():
    for bi, book in ((["USD"], ["EUR"]), (["EUR"], [""]), (["EUR"], [None]), (["EUR"], [float("nan")])):
        info = bi_compare.provenance(frame=_mixed(bi, book))
        assert info["currency_code"] == "" and info["currency"] == "unknown", (bi, book)
        assert source_display.money(585, info) == "currency unknown 585"
