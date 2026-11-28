"""ACL-2649: a stale heavy status is not a fact; page text expires faster than the API; Open names why it did not read.

Offline, fixtures only: no Meta, no browser, no timers. The hold/Retry parts of the Issue are covered by
`test_acl2652_lift_auth_hold.py` and `test_acl2668_hold_retry.py` (already in main).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from src.analysis_agent import account_spend as sp
from src.analysis_agent import campaign_inventory as ci
from src.analysis_agent import status_resolve as R
from src.web import account_state as A
from src.web import warm_api

NOW = datetime(2026, 11, 20, 12, 0, 0)
TODAY = date(2026, 11, 20)      # account_state evaluates a past day at 12:00


def _at(hours_ago: float) -> str:
    return (NOW - timedelta(hours=hours_ago)).strftime("%Y-%m-%d %H:%M:%S")


def row(status, spend=0, **kw):
    return {"status": status, "spend": spend, "has_data": bool(spend), "in_workbook": True, "act_id": "act_1", **kw}


# ── resolve_status: heavy statuses age out ───────────────────────────────

@pytest.mark.parametrize("word", ["restricted", "disabled", "review", "blocked"])
def test_two_day_old_heavy_status_is_unknown_stale_and_keeps_its_word(word):
    r = R.resolve_status({"status": word, "at": _at(48), "source": "api"}, "2026-11-20 09:00:00", NOW)
    assert r["shown"] == "unknown" and not r["known"] and r["stale"] and r["last"] == word


def test_fresh_api_heavy_status_is_still_a_fact():
    r = R.resolve_status({"status": "restricted", "at": _at(23), "source": "api"}, "", NOW)
    assert r["known"] and r["shown"] == "restricted" and not r["stale"]
    after_fail = R.resolve_status({"status": "restricted", "at": _at(2), "source": "api"}, "2026-11-20 11:00:00", NOW)
    assert after_fail["known"] and after_fail["stale"]


def test_page_status_expires_faster_than_an_api_status():
    page = {"status": "restricted", "at": _at(8), "source": "page"}
    api = {**page, "source": "api"}
    assert not R.resolve_status(page, "", NOW)["known"]
    assert R.resolve_status(api, "", NOW)["known"]
    assert R.resolve_status({**page, "at": _at(5)}, "", NOW)["known"]
    assert R.PAGE_STATUS_FRESH_HOURS < R.STATUS_FRESH_HOURS


@pytest.mark.parametrize("at", ["", None, "garbage", "2026-13-45", 0])
def test_heavy_status_with_an_unreadable_time_is_not_a_fact(at):
    r = R.resolve_status({"status": "disabled", "at": at, "source": "api"}, "", NOW)
    assert r["shown"] == "unknown" and not r["known"]


def test_empty_and_missing_records_do_not_raise():
    for raw in ({}, None, {"status": ""}, {"status": None}, {"status": "restricted"}):
        assert R.resolve_status(raw, "", NOW)["shown"] == "unknown"


# ── should_replace: any newer API read replaces page text ────────────────

def test_a_newer_api_read_replaces_a_page_status_even_when_lighter_and_close_in_time():
    old = {"status": "restricted", "at": "2026-11-20 08:00:00", "source": "page"}
    for gap_min in (1, 5, 600):
        at = (datetime(2026, 11, 20, 8) + timedelta(minutes=gap_min)).strftime("%Y-%m-%d %H:%M:%S")
        assert R.should_replace(old, {"status": "clear", "at": at, "source": "api"}) is True


def test_an_older_api_read_does_not_replace_a_newer_page_status():
    old = {"status": "restricted", "at": "2026-11-20 08:00:00", "source": "page"}
    assert R.should_replace(old, {"status": "clear", "at": "2026-11-19 08:00:00", "source": "api"}) is False


def test_a_page_read_does_not_clear_a_heavy_api_status_inside_one_cycle():
    old = {"status": "disabled", "at": "2026-11-20 08:00:00", "source": "api"}
    assert R.should_replace(old, {"status": "clear", "at": "2026-11-20 08:03:00", "source": "page"}) is False


def test_unreadable_times_and_missing_sources_still_replace():
    assert R.should_replace({"status": "restricted", "at": "x"}, {"status": "clear", "at": "y", "source": "api"}) is True
    assert R.should_replace(None, {"status": "clear"}) is True
    assert R.should_replace({}, {"status": "clear", "source": "api"}) is True


@pytest.fixture
def store(monkeypatch):
    data: dict = {}
    monkeypatch.setattr(sp, "_load_all", lambda: data)
    monkeypatch.setattr(sp, "_save_all", lambda d: None)
    monkeypatch.setattr(ci, "get_attempt", lambda account: None)
    return data


def test_a_fresh_api_clear_clears_an_older_page_restricted_in_the_store(store):
    acc = "FacebookDocXXX - Example"
    sp.put_status(acc, "restricted", "page text", source="page", at="2026-11-20 08:00:00")
    assert sp.put_status(acc, "clear", "ok", source="api", at="2026-11-20 08:02:00", account_status=1)
    assert sp.get_status(acc)["status"] == "clear"


# ── Home state ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("failure", [
    {},
    {"stale": "1", "tried_at": "2026-11-20 09:00:00", "tried_error": "logged out"},
    {"stale": "1"},
])
def test_two_day_old_restricted_with_a_failed_or_skipped_read_is_not_issue(failure):
    st = {"status": "restricted", "status_text": "restricted", "at": _at(48), "source": "api", **failure}
    for spend in (0, 50):
        s = A.account_state(row(st, spend), TODAY)
        assert s["key"] == "unknown", (failure, spend)
        assert s["status_line"].startswith("unknown · last status restricted 18.11")
        assert s["status_line"].endswith("not refreshed")
        assert s["reason"].startswith("last status restricted 18.11") and s["stale"]
        assert not s["by_spend"] and s["short_reason"] == ""


def test_old_disabled_is_not_dead_and_fresh_disabled_is():
    assert A.account_state(row({"status": "disabled", "at": _at(72), "source": "api"}, 40), TODAY)["key"] == "unknown"
    assert A.account_state(row({"status": "disabled", "at": _at(3), "source": "api"}, 40), TODAY)["key"] == "dead"


def test_a_page_restricted_older_than_the_page_limit_is_not_issue_but_an_api_one_is():
    page = {"status": "restricted", "at": _at(8), "source": "page"}
    assert A.account_state(row(page), TODAY)["key"] == "unknown"
    assert A.account_state(row({**page, "source": "api"}), TODAY)["key"] == "issue"


def test_fresh_api_clear_is_active():
    s = A.account_state(row({"status": "clear", "at": _at(1), "source": "api"}), TODAY)
    assert s["key"] == "active" and not s["stale"]


def test_a_person_lock_still_wins_over_an_old_heavy_status():
    hold = {"person_lock": True, "at": NOW.timestamp(), "page": "", "title": ""}
    s = A.account_state(row({"status": "restricted", "at": _at(80), "source": "api"}, hold=hold), TODAY)
    assert s["key"] == "issue" and s["person_lock"] and s["status_line"].startswith("Facebook locked")


@pytest.mark.parametrize("st", [{}, {"status": ""}, {"status": "restricted"}, {"status": "restricted", "at": "junk"}])
def test_odd_records_never_raise_and_never_give_issue(st):
    assert A.account_state(row(st), TODAY)["key"] in ("unknown", "new")


def test_rejected_ads_never_change_the_account_state():
    base = row({"status": "clear", "at": _at(1), "source": "api"}, 20)
    noisy = {**base, "rejected": 7, "ads": [{"status": "DISAPPROVED"}], "campaigns": [{"effective_status": "WITH_ISSUES"}]}
    assert A.account_state(noisy, TODAY) == A.account_state(base, TODAY)
    no_status = row({}, 0)
    assert A.account_state({**no_status, "rejected": 3}, TODAY)["key"] == A.account_state(no_status, TODAY)["key"]
    assert A.account_state({**no_status, "rejected": 3}, TODAY)["key"] != "issue"


def test_rows_of_other_accounts_do_not_change():
    good = row({"status": "clear", "at": _at(1), "source": "api"}, 5)
    before = A.account_state(good, TODAY)
    A.account_state(row({"status": "restricted", "at": _at(48), "source": "api"}), TODAY)
    assert A.account_state(good, TODAY) == before


def test_status_tip_for_an_old_heavy_status_names_the_word_and_the_limit():
    s = A.account_state(row({"status": "restricted", "at": _at(48), "source": "api", "tried_error": "boom"}), TODAY)
    assert "restricted" in s["status_tip"] and "not shown as a current problem" in s["status_tip"] and "boom" in s["status_tip"]


# ── Open: the row says why a read did not start ──────────────────────────

def test_open_note_for_a_running_read():
    assert "already running" in warm_api.open_read_note({"queued": False, "reason": "running"})


def test_open_note_for_a_paused_queue_says_when_it_reads_again():
    until = datetime(2026, 11, 20, 14, 35).timestamp()
    assert "14:35" in warm_api.open_read_note({"queued": True, "reason": "halted", "until": until})
    assert "resume" in warm_api.open_read_note({"queued": True, "reason": "halted", "until": None})
    assert "resume" in warm_api.open_read_note({"queued": True, "reason": "halted", "until": "junk"})


def test_open_note_is_empty_for_a_normal_queue_and_names_skips():
    assert warm_api.open_read_note({"queued": True, "reason": "queued"}) == ""
    assert warm_api.open_read_note({}) == "" and warm_api.open_read_note(None) == ""
    assert "Retry on this profile" in warm_api.open_read_note({"queued": False, "reason": "profile_held"})
    assert warm_api.open_read_note({"queued": False, "reason": "something_new"}) == ""


def test_open_result_carries_the_note_and_requests_exactly_one_read(monkeypatch):
    from src.analysis_agent import account_refresh

    calls = []

    class _Queue:
        def request(self, label, trigger, **kw):
            calls.append((label, trigger, kw))
            return {"queued": False, "reason": "running"}

        def state(self):
            return {"halt": None}

    monkeypatch.setattr(account_refresh, "queue", lambda: _Queue())
    monkeypatch.setattr(warm_api, "note_tab_lock", lambda label, **k: False)
    seen = {}
    monkeypatch.setattr(warm_api, "_set_result", lambda job, label, result: seen.update(result))
    warm_api._on_open_result("job", "Acc", {"ok": True})
    assert calls == [("Acc", "open", {"force": True})]
    assert seen["status_read"] == "queued" and "already running" in seen["status_note"]


def test_open_result_for_a_paused_queue_reads_the_pause_time(monkeypatch):
    from src.analysis_agent import account_refresh

    until = datetime(2026, 11, 20, 16, 5).timestamp()

    class _Queue:
        def request(self, label, trigger, **kw):
            return {"queued": True, "reason": "halted"}

        def state(self):
            return {"halt": {"reason": "Multilogin is not running.", "until": until}}

    monkeypatch.setattr(account_refresh, "queue", lambda: _Queue())
    monkeypatch.setattr(warm_api, "note_tab_lock", lambda label, **k: False)
    seen = {}
    monkeypatch.setattr(warm_api, "_set_result", lambda job, label, result: seen.update(result))
    warm_api._on_open_result("job", "Acc", {"ok": True})
    assert seen["status_read"] == "queued" and "16:05" in seen["status_note"]


def test_open_result_for_a_failed_open_has_no_note_and_no_read(monkeypatch):
    seen = {}
    monkeypatch.setattr(warm_api, "_set_result", lambda job, label, result: seen.update(result))
    warm_api._on_open_result("job", "Acc", {"ok": False})
    assert seen["status_read"] == "n/a" and "status_note" not in seen


def test_the_home_script_shows_the_note_and_adds_no_timer():
    js = (Path(__file__).resolve().parent.parent / "src" / "web" / "static" / "warm_sessions.js").read_text(encoding="utf-8")
    assert "r.status_note" in js
