"""ACL-2847: a profile that was already open and fell to the Facebook login page.

Fixtures only: no Meta call, no browser, no navigation. The tab address from the local debugging port is the evidence.
"""
from __future__ import annotations

import contextlib
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.analysis_agent import account_spend
from src.fb_api import account_blocks as blocks
from src.fb_api import connection, warm
from src.web import account_state as astate
from src.web import warm_api

LOGIN_URL = "https://www.facebook.com/login/?next=https%3A%2F%2Fadsmanager.facebook.com%2F"
ADS_URL = "https://adsmanager.facebook.com/adsmanager/manage/campaigns?act=1"
TEXT = "Logged out of Facebook. Open the profile, log in to Ads Manager, then publish again."
ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def rig(tmp_path, monkeypatch):
    monkeypatch.setattr(blocks, "_PATH_OVERRIDE", tmp_path / "holds.json")
    blocks._TIMED_META.clear()
    tabs: dict = {}
    current = {"label": ""}
    monkeypatch.setattr(account_spend, "_profile_key", lambda label, *a, **k: "key-" + label)
    monkeypatch.setattr(warm_api, "_profile_id", lambda label: "pid-" + label)
    monkeypatch.setattr(warm_api, "_entry", lambda label: SimpleNamespace(profile_id="pid-" + label))
    monkeypatch.setattr(warm_api, "_guard", lambda: None)
    reg = SimpleNamespace(session=lambda pid: SimpleNamespace(port=9222),
                          _scan=lambda pids: {p: ("port", 9222) for p in pids})
    monkeypatch.setattr(warm_api, "_registry", lambda: reg)
    # One fake port for every profile: the account being worked on decides which tabs it answers with.
    monkeypatch.setattr(warm, "local_page_targets", lambda port, *a, **k: list(tabs.get(current["label"], [])))
    monkeypatch.setattr(connection, "prepare_existing",
                        lambda reg_, entry, label: current.update(label=label) or True)
    import src.fb_ads_rpa.operations as ops

    class Lock:
        def holding(self, timeout=0):
            return contextlib.nullcontext()

    monkeypatch.setattr(ops, "profile_lock", lambda pid: Lock())
    tabs["__current__"] = current
    yield tabs
    blocks._TIMED_META.clear()


def _open(tabs, label, targets):
    tabs[label] = targets
    tabs["__current__"]["label"] = label


def test_an_already_open_profile_on_the_login_page_is_held_and_the_row_is_red(rig):
    _open(rig, "A", [("about:blank", ""), (LOGIN_URL, "Log in to Facebook")])
    assert warm_api.note_tab_lock("A", action="Check plan") is True
    held = blocks.hold_of("key-A")
    assert held["page"] == "login" and held["action"] == "Check plan"
    row = blocks.view({"A": "key-A"})["A"]
    assert row["person_lock"] and row["page"] == "login" and row["reason"] == "Logged out of Facebook."
    assert row["next"] == "Open the profile, log in to Ads Manager, then publish again."
    assert row["title"] == "Log in to Facebook"
    shown = astate._person_lock(row)
    assert shown["word"] == "Logged out of Facebook" and "Open the profile, log in to Ads Manager" in shown["tip"]


def test_only_the_account_on_the_login_page_is_held(rig):
    _open(rig, "A", [(LOGIN_URL, "")])
    warm_api.note_tab_lock("A")
    _open(rig, "B", [(ADS_URL, "Ads Manager")])
    assert warm_api.note_tab_lock("B") is False
    assert blocks.is_held("key-A") and not blocks.is_held("key-B")
    assert set(blocks.view({"A": "key-A", "B": "key-B"})) == {"A"}


def test_publish_refuses_at_once_with_the_one_text_and_nothing_is_created(rig):
    _open(rig, "A", [(LOGIN_URL, "")])
    data, code = warm_api.prepare_connection("A")
    assert code == 409 and data["ok"] is False and data["code"] == "account_blocked" and data["error"] == TEXT
    assert blocks.refusal("key-A", "A", "publish") == (TEXT, "account_blocked")
    assert blocks.refusal("key-A", "A", "check_plan") == (TEXT, "account_blocked")


def test_another_account_in_the_same_run_goes_on(rig):
    _open(rig, "A", [(LOGIN_URL, "")])
    assert warm_api.prepare_connection("A")[1] == 409
    _open(rig, "B", [(ADS_URL, "Ads Manager")])
    data, code = warm_api.prepare_connection("B")
    assert code == 200 and data == {"ok": True, "account": "B"}


def test_check_plan_open_reads_the_tabs_and_stops_before_any_graph_call(rig, monkeypatch):
    from src.web import publish_api

    _open(rig, "A", [(LOGIN_URL, "Log in to Facebook")])
    monkeypatch.setattr(publish_api, "_resolve_entry",
                        lambda label: SimpleNamespace(ad_account_id="act_1234567890", profile_id="pid-A"))
    monkeypatch.setattr(publish_api, "_cooldown", lambda label: (0.0, ""))
    monkeypatch.setattr(publish_api, "_frozen_api", lambda label: (None, "key-A"))
    session = publish_api.ApiPublisher.__new__(publish_api.ApiPublisher)
    session.dry_run = True
    opened = []
    monkeypatch.setattr(session, "_open_warm", lambda *a: opened.append(a) or True, raising=False)
    with blocks.problem_action("check_plan"):
        with pytest.raises(publish_api.ApiPublishError) as err:
            session.open("A")
    assert str(err.value) == TEXT and err.value.code == "account_blocked" and not opened


def test_a_healthy_tab_after_login_ends_the_hold_and_open_does_too(rig):
    _open(rig, "A", [(LOGIN_URL, "")])
    warm_api.note_tab_lock("A")
    assert blocks.is_held("key-A")
    rig["A"] = [(ADS_URL, "Ads Manager")]           # the person logged in inside the profile
    data, code = warm_api.prepare_connection("A")
    assert code == 200 and not blocks.is_held("key-A")
    rig["A"] = [(LOGIN_URL, "")]
    warm_api.note_tab_lock("A")
    assert blocks.is_held("key-A")
    rig["A"] = [(ADS_URL, "")]
    warm_api.note_tab_lock("A")                      # Open profile on a healthy page
    assert not blocks.is_held("key-A")


def test_an_unreadable_profile_keeps_the_hold(rig):
    _open(rig, "A", [(LOGIN_URL, "")])
    warm_api.note_tab_lock("A")
    rig["A"] = []                                    # no answer from the debugging port is not "healthy"
    warm_api.note_tab_lock("A")
    assert blocks.is_held("key-A")


def test_a_graph_logout_with_page_evidence_shows_the_same_words(rig):
    from src.fb_api.graph import LoggedOut

    assert blocks.note_stop("key-G", "G", LoggedOut("the profile is logged out of Facebook", page_seen=True), "Check plan")
    row = blocks.view({"G": "key-G"})["G"]
    assert row["person_lock"] and row["page"] == "login"
    assert row["next"] == "Open the profile, log in to Ads Manager, then publish again."
    assert blocks.refusal("key-G", "G", "publish")[0] == TEXT


def test_a_token_only_logout_is_still_not_a_hold(rig):
    from src.fb_api.graph import LoggedOut

    assert blocks.note_stop("key-T", "T", LoggedOut("token gone"), "Publish") is False


def test_the_page_script_says_why_when_publish_cannot_go_on():
    html = (ROOT / "src/web/templates/report.html").read_text(encoding="utf-8")
    assert "refused.code = data.code" in html                       # the server's code reaches the Publish path
    assert "err.code === 'account_blocked'" in html                 # and its text is shown without a prefix
    assert "cbReviewFailed" in html                                 # a review that cannot open says so
    block = html[html.index("var run = cbPublishRun(model.opts.onProgress"):]
    assert re.search(r"if \(!run\) \{\s*//[^\n]*\n\s*aclAlert\(", block[:400])   # no silent return in Confirm
