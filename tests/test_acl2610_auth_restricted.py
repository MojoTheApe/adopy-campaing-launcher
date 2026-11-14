"""ACL-2610: кабинет с account_status = 1, но ограниченный проверкой Meta (Start authentication).

Офлайн, фейковый Graph. ТЕКСТ и КОД проблемы Meta НЕ проверены на живом образце: фразы подобраны
по баннеру; тест фиксирует поведение на синтетическом `issues_info`.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.analysis_agent import account_next_step as ns  # noqa: E402
from src.analysis_agent import account_spend as sp  # noqa: E402
from src.analysis_agent import account_spend_api as api  # noqa: E402
from src.analysis_agent import campaign_inventory as ci  # noqa: E402

ACC = "FacebookDocXXX - Example"
ACT = "act_1234567890"

AUTH_ISSUE = {"level": "CAMPAIGN", "error_code": 1234567,
              "error_summary": "We restricted your ad account",
              "error_message": "Your ads won't run until you authenticate your account for act_1234567890."}
OTHER_ISSUE = {"level": "AD_SET", "error_code": 2460003, "error_summary": "Custom audience blocked",
               "error_message": "The custom audience is not available."}


class FakeClient:
    def __init__(self, campaigns, status=1):
        self.acc = {"account_id": ACT[4:], "currency": "USD", "account_status": status, "disable_reason": 0}
        self.campaigns, self.calls, self.truncated, self.paths = campaigns, 0, [], []

    def account(self, act):
        self.calls += 1
        return self.acc

    def get_all(self, path, params, max_pages=5):
        self.calls += 1
        self.paths.append((path, params["fields"]))
        return self.campaigns if path.endswith("/campaigns") else []


@pytest.fixture
def store(monkeypatch):
    data: dict = {}
    monkeypatch.setattr(sp, "_load_all", lambda: data)
    monkeypatch.setattr(sp, "_save_all", lambda d: None)
    monkeypatch.setattr(ci, "get_attempt", lambda account: None)
    events: list = []
    monkeypatch.setattr(api._tm, "record", lambda e: events.append(e))
    return events


def _read(campaigns, status=1):
    client = FakeClient(campaigns, status)
    snap = ci.Snapshot(account=ACC)
    ci.read_into(snap, client, ACT, structure_only=True)
    ci.finalize(snap)
    return snap, client


def _camp(*issues):
    return {"id": "111", "name": "C1", "status": "ACTIVE", "effective_status": "ACTIVE", "issues_info": list(issues)}


def test_issues_info_is_requested_on_the_existing_lists_without_new_calls():
    snap, client = _read([_camp()])
    assert client.calls == 4                                   # кабинет + кампании + ad set + объявления, как раньше
    assert all("issues_info" in fields for _path, fields in client.paths)


def test_active_account_with_auth_issue_is_restricted_with_the_auth_step(store):
    snap, _ = _read([_camp(AUTH_ISSUE)])
    assert ci.record_status(snap) is True
    got = sp.get_status(ACC)
    assert got["status"] == "restricted" and got["source"] == "api"
    assert got["status_text"] == api.AUTH_RESTRICTED_TEXT
    assert "Start authentication" in got["next_step"] and "Open the profile" in got["next_step"]


def test_active_account_without_issues_stays_active(store):
    snap, _ = _read([_camp()])
    ci.record_status(snap)
    assert sp.get_status(ACC)["status"] == "clear"
    assert store == []                                          # пустые issues в телеметрию не идут


def test_unrelated_issue_does_not_mark_the_account_restricted(store):
    snap, _ = _read([_camp(OTHER_ISSUE)])
    ci.record_status(snap)
    assert sp.get_status(ACC)["status"] == "clear"


def test_disabled_account_keeps_its_own_status_even_with_an_auth_issue(store):
    snap, _ = _read([_camp(AUTH_ISSUE)], status=2)
    ci.record_status(snap)
    assert sp.get_status(ACC)["status"] == "disabled"


def test_issue_sample_is_logged_redacted(store):
    snap, _ = _read([_camp(AUTH_ISSUE, OTHER_ISSUE)])
    ci.record_status(snap)
    assert len(store) == 1 and store[0]["event"] == "fb_issues_info"
    text = repr(store[0])
    assert "act_1234567890" not in text and "1234567890" not in text
    assert "We restricted your ad account" in text and "2460003" in text


def test_status_only_clear_does_not_lift_the_auth_restriction(store):
    snap, _ = _read([_camp(AUTH_ISSUE)])
    ci.record_status(snap)
    only = ci.Snapshot(account=ACC, state="ok", account_status=1)
    only._status_only = True
    assert ci.record_status(only) is False
    assert sp.get_status(ACC)["status"] == "restricted"


def test_next_tree_read_without_the_issue_lifts_it(store, monkeypatch):
    snap, _ = _read([_camp(AUTH_ISSUE)])
    ci.record_status(snap)
    monkeypatch.setattr(sp, "_now", lambda: "2099-01-01 00:00:00")
    snap2, _ = _read([_camp()])
    assert ci.record_status(snap2) is True
    assert sp.get_status(ACC)["status"] == "clear"


def test_status_only_clear_is_stored_when_nothing_restricted_is_kept(store):
    only = ci.Snapshot(account=ACC, state="ok", account_status=1)
    only._status_only = True
    assert ci.record_status(only) is True
    assert sp.get_status(ACC)["status"] == "clear"


def test_next_step_for_plain_restricted_is_unchanged():
    assert ns.next_step("restricted", "Ad account is restricted: payment is unsettled").startswith("Check Billing")


# ── ошибка записи/чтения 31/3858385 (образец publish 13.11; тексты Meta из него) ──

WRITE_MSG = ("We think someone may have tried to access your account without permission. For your protection, "
             "you won't be able to create or modify ads until you authenticate your account in Ads Manager.")


def _auth_stop():
    from src.fb_api import graph as g
    return g.AuthRequired(f"Meta asks to authenticate this account (31/3858385): {WRITE_MSG}",
                          code=31, subcode=3858385)


def _no_hold(monkeypatch):
    monkeypatch.setattr(api._blocks, "note_stop", lambda *a, **k: False)


def test_write_stop_31_3858385_marks_the_account_restricted_without_requests(store, monkeypatch):
    _no_hold(monkeypatch)
    api._start_cooldown("profile-key", _auth_stop(), ACC)
    got = sp.get_status(ACC)
    assert got["status"] == "restricted" and got["source"] == "api"
    assert got["status_text"] == api.AUTH_RESTRICTED_TEXT and "Start authentication" in got["next_step"]


def test_plain_code_pair_without_the_class_also_counts(store, monkeypatch):
    from src.fb_api import graph as g
    _no_hold(monkeypatch)
    api._start_cooldown("profile-key", g.Checkpoint("x", code=31, subcode=3858385), ACC)
    assert sp.get_status(ACC)["status"] == "restricted"


def test_other_stops_do_not_touch_the_status(store, monkeypatch):
    from src.fb_api import graph as g
    _no_hold(monkeypatch)
    api._start_cooldown("profile-key", g.Checkpoint("x", code=1, subcode=2), ACC)
    api._start_cooldown("profile-key", g.RateLimited("x", code=17), ACC)
    assert not sp.get_status(ACC).get("status")


def test_write_error_phrases_match_the_matcher():
    for text in ("Please authenticate your account", WRITE_MSG):
        assert api.is_auth_restriction({"summary": text, "message": ""})
