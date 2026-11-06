"""Состояния аккаунтов Home: чистая функция, без обращений к Meta."""
from datetime import date

from src.web import account_state as A

TODAY = date(2026, 11, 4)


def row(status=None, spend=0, has_data=False, in_workbook=True, **kw):
    return {"status": status or {}, "spend": spend, "has_data": has_data,
            "in_workbook": in_workbook, **kw}


def test_disabled_is_dead_even_with_spend():
    s = A.account_state(row({"status": "disabled", "status_text": "x", "at": "2026-11-04T10:00:00"}, 40, True), TODAY)
    assert s["key"] == "dead"


def test_issue_statuses():
    for code in ("restricted", "blocked", "review"):
        assert A.account_state(row({"status": code}, 10, True), TODAY)["key"] == "issue"


def test_spend_is_active():
    assert A.account_state(row({"status": "clear"}, 5, True), TODAY)["key"] == "active"


def test_not_in_workbook_is_new():
    assert A.account_state(row(in_workbook=False), TODAY)["key"] == "new"


def test_no_spend_is_unknown():
    assert A.account_state(row(), TODAY)["key"] == "unknown"
    assert A.account_state({}, TODAY)["key"] == "unknown"


def test_stale_by_age_and_flag():
    old = A.account_state(row({"status": "restricted", "at": "2026-10-31T10:00:00"}), TODAY)
    assert old["stale"] and old["age_days"] == 4 and old["read_short"] == "31.10"
    fresh = A.account_state(row({"status": "restricted", "at": "2026-11-03T10:00:00"}), TODAY)
    assert not fresh["stale"]
    flagged = A.account_state(row({"status": "restricted", "at": "2026-11-04T10:00:00", "stale": "1"}), TODAY)
    assert flagged["stale"]


def test_unknown_reason_from_failed_read():
    s = A.account_state(row({"tried_at": "2026-10-31T09:00:00", "tried_error": "timeout", "stale": "1"}), TODAY)
    assert s["reason"] == "last read failed 31.10"


def test_elsewhere_reason():
    s = A.account_state(row(elsewhere={"Chile": 1}), TODAY)
    assert "spend filed under Chile" in s["reason"]


def test_short_reason():
    assert A.short_reason("restricted", "Ad account not in BM") == "not in BM"
    assert A.short_reason("restricted", "needs a video selfie") == "video selfie"
    assert A.short_reason("review", "") == "in review"
    assert A.short_reason("blocked", "") == "blocked"
    assert A.short_reason("restricted", "") == "restricted"
    assert A.short_reason("", "whatever") == "problem"


def test_counts_and_sort():
    rows = [
        {"account": "b", "spend": 5, "state": {"key": "unknown"}},
        {"account": "a", "spend": 1, "state": {"key": "active"}},
        {"account": "c", "spend": 9, "state": {"key": "active"}},
        {"account": "d", "spend": 0, "state": {"key": "issue"}},
    ]
    assert [r["account"] for r in A.sort_rows(rows)] == ["c", "a", "d", "b"]
    c = A.state_counts(r["state"] for r in rows)
    assert c == {"active": 2, "issue": 1, "dead": 0, "new": 0, "unknown": 1, "all": 4}


def test_empty_country_counts():
    assert A.state_counts([])["all"] == 0
