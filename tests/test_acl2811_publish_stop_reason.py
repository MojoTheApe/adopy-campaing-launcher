"""ACL-2811: a stopped publish shows Meta's reason for the account and what to do, never "wait"."""
from __future__ import annotations

import re

import pytest

from src.analysis_agent import account_spend as sp
from src.analysis_agent import account_spend_api as api
from src.fb_api import error_catalog, plain_errors as pe, stop_reason
from src.fb_api import graph as g
from src.web import publish_api

ACCOUNT = "KLD_BFL_G88"
WAIT = re.compile(r"\bwait", re.I)


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(sp, "_load_all", lambda: dict(_DATA))
    _DATA.clear()
    return _DATA


_DATA: dict = {}


def _put(status, text, account=ACCOUNT):
    _DATA["status|" + account] = {"status": status, "status_text": text, "at": "2026-11-19T09:16:00"}


def _frozen():
    return pe.classify(publish_api.ApiPublishError("tab", code="page_unresponsive"))


def test_3858385_shows_meta_reason_and_action_on_the_account(store):
    _put("restricted", api.AUTH_RESTRICTED_TEXT)
    base = _frozen()
    base.left = ["Left in Meta: a paused campaign. Publishing again reuses it if its settings match; x."]
    out = stop_reason.explain(base, ACCOUNT)
    assert out.key == "auth_required"
    assert out.line() == ("Meta asks to authenticate this account - open the profile in Multilogin, "
                          "pass the check, then Publish again")
    assert out.left == base.left and out.text().endswith(base.left[0])   # "Left in Meta" part unchanged
    assert out.details["account_signal"]["status"] == "restricted"
    assert not WAIT.search(out.text())


def test_no_meta_reason_says_what_happened_and_publish_again_without_waiting(store):
    out = stop_reason.explain(_frozen(), ACCOUNT)          # nothing stored for the account
    assert out.line() == "The Ads Manager tab stopped answering - Publish again"
    _put("clear", "")
    assert stop_reason.explain(_frozen(), ACCOUNT).line() == out.line()
    assert not WAIT.search(out.text())


def test_signal_of_another_account_is_not_used(store):
    _put("restricted", api.AUTH_RESTRICTED_TEXT, account="OTHER")
    assert stop_reason.explain(_frozen(), ACCOUNT).key == "tab_unresponsive"


def test_a_meta_named_failure_keeps_its_own_text(store):
    _put("restricted", api.AUTH_RESTRICTED_TEXT)
    own = pe.classify(g.GraphError("x", code=1870226, subcode=1870226))
    assert stop_reason.explain(own, ACCOUNT) is own


def test_other_meta_status_names_the_reason(store):
    _put("disabled", "Ad account is disabled")
    out = stop_reason.explain(_frozen(), ACCOUNT)
    assert out.key == "account_blocked" and "Ad account is disabled" in out.line() and "Publish again" in out.line()


def test_unreadable_store_does_not_break_the_stop_text(monkeypatch):
    def boom():
        raise OSError("no file")
    monkeypatch.setattr(sp, "_load_all", boom)
    assert stop_reason.explain(_frozen(), ACCOUNT).key == "tab_unresponsive"


def test_second_campaign_of_the_account_shows_the_reason(store):
    _put("restricted", api.AUTH_RESTRICTED_TEXT)
    text = stop_reason.not_started_text(ACCOUNT, "page_unresponsive")
    assert text.startswith("Not started: Meta asks to authenticate this account") and "Nothing was created" in text
    _DATA.clear()
    assert stop_reason.not_started_text(ACCOUNT, "page_unresponsive") == (
        "Not started: Graph API stopped earlier (page_unresponsive). Nothing was created for it.")


def test_the_lookup_makes_no_meta_call(store, monkeypatch):
    import socket
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: pytest.fail("network"))
    _put("restricted", api.AUTH_RESTRICTED_TEXT)
    stop_reason.explain(_frozen(), ACCOUNT)


def test_no_buyer_facing_publish_text_says_wait():
    for key, plain in pe.TABLE.items():
        assert not WAIT.search(plain.next_step) and "minutes" not in plain.next_step, key
    for exc in (publish_api.ApiPublishError("x", code="page_unresponsive"), g.RateLimited("slow", retry_after_s=300),
                g.CallBudget("budget"), g.PolicyBlocked("blocked"), g.GraphError("t", code=2)):
        assert not WAIT.search(pe.classify(exc).text()), exc
    for entry in error_catalog.CATALOG.values():
        assert not WAIT.search(entry.action_en) and "minutes" not in entry.action_en, entry.action_en
