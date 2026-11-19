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


# ───────────── round 2: the signal arrives after the stop; Publish again ─────────────

from src.web import publish_jobs as pj  # noqa: E402


@pytest.fixture()
def jobs(monkeypatch):
    """Two finished jobs of two accounts, stopped by a frozen tab with nothing stored (the G88 case)."""
    monkeypatch.setattr(pj, "_persist_job", lambda *a, **k: None)
    saved = dict(pj._jobs)
    pj._jobs.clear()

    def job(jid, account):
        stopped = _frozen()
        stopped.left = ["Left in Meta: a paused campaign."]
        return {"job_id": jid, "account_label": account, "status": "done", "finished_at": "2026-11-19T09:05:00",
                "status_revision": 0, "errors": [stopped.text()], "error_details": [{"failure_key": stopped.key}],
                "results": [
                    {"campaign_name": "A", "success": False, "error": stopped.text(), "failure": stopped.as_dict(),
                     "blocking_failures": ["not finished: " + stopped.text()]},
                    {"campaign_name": "B", "success": False, "not_started": True, "failure": None,
                     "error": "Not started: Graph API stopped earlier (page_unresponsive). Nothing was created for it."}]}
    pj._jobs["j-g88"], pj._jobs["j-other"] = job("j-g88", ACCOUNT), job("j-other", "Doc120")
    yield pj._jobs
    pj._jobs.clear()
    pj._jobs.update(saved)


def test_g88_sequence_inventory_signal_after_the_stop_updates_that_accounts_rows(store, jobs):
    assert stop_reason.explain(_frozen(), ACCOUNT).key == "tab_unresponsive"       # nothing stored at stop time
    # 11 minutes later campaign_inventory stores 3858385 through the status writer, which calls the listener
    pj.apply_account_signal(ACCOUNT, "restricted", api.AUTH_RESTRICTED_TEXT)      # what the listener runs
    job = jobs["j-g88"]
    first, second = job["results"]
    want = ("Meta asks to authenticate this account - open the profile in Multilogin, "
            "pass the check, then Publish again")
    assert first["failure"]["key"] == "auth_required" and first["error"].startswith(want)
    assert "Left in Meta: a paused campaign." in first["error"]                    # the kept part
    assert first["blocking_failures"] == ["not finished: " + first["error"]]
    assert second["error"] == f"Not started: {want}. Nothing was created for it."
    assert job["errors"][0].startswith(want)
    assert not WAIT.search(first["error"] + second["error"])


def test_the_status_writer_calls_the_listener(store, jobs, monkeypatch):
    seen = []
    monkeypatch.setattr(sp, "_STATUS_LISTENERS", [lambda a, s, t: seen.append((a, s)), pj.apply_account_signal])
    monkeypatch.setattr(sp, "_save_all", lambda data: None)
    monkeypatch.setattr(sp, "_merge_status_locked", lambda d, a, r: True)
    sp.put_status(ACCOUNT, "restricted", api.AUTH_RESTRICTED_TEXT)
    assert seen == [(ACCOUNT, "restricted")]
    assert jobs["j-g88"]["results"][0]["failure"]["key"] == "auth_required"
    assert jobs["j-other"]["results"][0]["failure"]["key"] == "tab_unresponsive"


def test_another_accounts_rows_are_unaffected(store, jobs):
    before = repr(jobs["j-other"])
    pj.apply_account_signal(ACCOUNT, "restricted", api.AUTH_RESTRICTED_TEXT)
    assert repr(jobs["j-other"]) == before


def test_a_clean_signal_does_not_erase_a_reason(store, jobs):
    pj.apply_account_signal(ACCOUNT, "restricted", api.AUTH_RESTRICTED_TEXT)
    shown = repr(jobs["j-g88"])
    assert pj.apply_account_signal(ACCOUNT, "clear", "") == 0
    assert repr(jobs["j-g88"]) == shown


def test_a_running_job_is_not_rewritten(store, jobs):
    jobs["j-g88"]["status"] = "running"
    assert pj.apply_account_signal(ACCOUNT, "restricted", api.AUTH_RESTRICTED_TEXT) == 0


def test_publish_again_on_a_3858385_account_shows_the_reason(tmp_path):
    """The account read the publish already makes answers 31/3858385: the rows say authenticate, not wait."""
    from tests.test_acl2595_auth_checkpoint import AuthMeta
    from tests.test_acl1761_api_create import _draft, _publisher

    pub = _publisher(AuthMeta())
    res = pub.publish_draft(_draft(tmp_path, ads=1))
    assert res["failure"]["key"] == "auth_required"
    assert res["error"].startswith("Meta asks to authenticate this account - open the profile in Multilogin")
    assert not WAIT.search(res["error"])


def test_publish_again_with_a_frozen_tab_and_a_stored_3858385_shows_the_reason(store):
    _put("restricted", api.AUTH_RESTRICTED_TEXT)
    out = stop_reason.explain(_frozen(), ACCOUNT)
    assert out.key == "auth_required" and "Publish again" in out.line()
    other = stop_reason.explain(_frozen(), "Doc120")
    assert other.key == "tab_unresponsive"
