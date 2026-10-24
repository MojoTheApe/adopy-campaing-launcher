"""Offline replay of the real PageTab -> InPageGraph lost-session boundary."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.fb_api.cdp import CdpError, PageTab, SessionLostError
from src.fb_api.graph import Checkpoint, GraphClient, GraphStop, LoggedOut, PageFailed
from src.fb_api.token_capture import InPageGraph


INCIDENT = json.loads((Path(__file__).parent / "fixtures" / "ads_manager" /
                       "fb_api_session_lost_2026_10_12.json").read_text(encoding="utf-8"))
ACT = "act_1234567890"


class Browser:
    """Only the CDP wire is fake: real tab, helper lifecycle and client execute."""

    def __init__(self):
        self.calls = []
        self.session = INCIDENT["session_id"]
        self.detached = False
        self.present = True
        self.url = INCIDENT["url"]
        self.world = 0
        self.graph_calls = []
        self.fail_on = ""
        self.browser_error = None
        self.target_overrides = {}
        self.after_attach_url = None
        self.kind = "ads_manager"
        self.has_token = True

    def call(self, method, params=None, *, session_id="", timeout=10):
        params = params or {}
        self.calls.append((method, params, session_id))
        expression = params.get("expression", "")
        if self.fail_on and (method == self.fail_on or self.fail_on in expression):
            self.fail_on = ""
            self.detached = True
        if session_id and (self.detached or session_id != self.session):
            raise CdpError(INCIDENT["reconciliation_error"])
        if method == "Target.createTarget":
            return {"targetId": INCIDENT["target_id"]}
        if method == "Target.getTargetInfo":
            if self.browser_error:
                raise self.browser_error
            if not self.present:
                raise CdpError("No target with given id found")
            return {"targetInfo": {"targetId": INCIDENT["target_id"], "type": "page",
                                   "url": self.url, **self.target_overrides}}
        if method == "Target.attachToTarget":
            if self.detached:
                self.session = INCIDENT["recovered_session_id"]
                self.detached = False
                if self.after_attach_url:
                    self.url = self.after_attach_url
            return {"sessionId": self.session}
        if method == "Page.getFrameTree":
            return {"frameTree": {"frame": {"id": "frame", "url": self.url}}}
        if method == "Page.createIsolatedWorld":
            self.world += 1
            return {"executionContextId": self.world}
        if method == "Runtime.evaluate":
            value = None
            if ".state()" in expression or ".probe()" in expression:
                value = {"kind": self.kind, "has_token": self.has_token}
            elif ".call(" in expression:
                self.graph_calls.append(expression)
                value = {"status": 200, "body": {"data": [{"id": "reconciled"}]}}
            return {"result": {"value": value}}
        return {}

    def close(self):
        pass


def transport():
    browser = Browser()
    tab = PageTab.open("http://127.0.0.1:1", connect=lambda *a, **k: browser)
    graph = InPageGraph(tab, sleep=lambda _: None)
    graph.start(ACT)
    browser.detached = True
    return browser, tab, graph


def test_exact_incident_reconciliation_recovers_only_the_owned_target():
    browser, tab, graph = transport()
    client = GraphClient(graph, min_interval_s=0)
    assert client.get("/existing-video") == {"data": [{"id": "reconciled"}]}
    assert tab.target_id == INCIDENT["target_id"]
    assert tab.session_id == INCIDENT["recovered_session_id"]
    assert len(browser.graph_calls) == 1
    assert [p["targetId"] for m, p, _ in browser.calls if m == "Target.attachToTarget"] == [
        INCIDENT["target_id"], INCIDENT["target_id"]]
    assert sum(m == "Target.createTarget" for m, _, _ in browser.calls) == 1


def test_missing_owned_target_is_a_typed_stop_for_creator_readback():
    browser, tab, graph = transport()
    browser.present = False
    client = GraphClient(graph, min_interval_s=0)
    with pytest.raises(GraphStop):
        client.get("/existing-video")
    assert browser.graph_calls == []
    sent = len(browser.calls)
    with pytest.raises(GraphStop):
        client.get("/must-not-run")
    assert len(browser.calls) == sent


def test_fresh_helper_on_a_warm_owned_tab_recovers_without_another_navigation():
    browser, tab, first_graph = transport()
    # Warm reuse changes read/write helper mode on the same tab without start().
    graph = InPageGraph(tab, sleep=lambda _: None)
    assert GraphClient(graph, min_interval_s=0).get("/reconcile") == {
        "data": [{"id": "reconciled"}]}
    assert tab.session_id == INCIDENT["recovered_session_id"]
    assert sum(m == "Page.navigate" for m, _, _ in browser.calls) == 1
    assert len(browser.graph_calls) == 1


def test_fresh_helper_never_adopts_an_unbound_current_account_url():
    browser = Browser()  # Its current URL looks valid but ACL has not navigated it.
    tab = PageTab.open("http://127.0.0.1:1", connect=lambda *a, **k: browser)
    graph = InPageGraph(tab, sleep=lambda _: None)
    browser.detached = True
    with pytest.raises(PageFailed):
        graph.request("GET", "/reconcile")
    assert sum(m == "Target.attachToTarget" for m, _, _ in browser.calls) == 1
    assert browser.graph_calls == []


def test_fresh_helper_keeps_the_original_account_when_the_current_page_changes():
    browser, tab, first_graph = transport()
    browser.url = "https://adsmanager.facebook.com/adsmanager/manage/campaigns?act=1234567890123456"
    graph = InPageGraph(tab, sleep=lambda _: None)
    with pytest.raises(PageFailed):
        graph.request("GET", "/reconcile")
    assert sum(m == "Target.attachToTarget" for m, _, _ in browser.calls) == 1
    assert browser.graph_calls == []


def test_fresh_helper_keeps_identity_checks_after_an_earlier_recovery():
    browser, tab, graph = transport()
    assert graph.request("GET", "/first")["status"] == 200
    browser.url = "https://adsmanager.facebook.com/adsmanager/manage/campaigns?act=1234567890123456"
    replacement = InPageGraph(tab, sleep=lambda _: None)
    with pytest.raises(PageFailed):
        replacement.request("GET", "/must-not-run")
    assert len(browser.graph_calls) == 1


def test_failed_navigation_does_not_rebind_a_replacement_helper():
    browser, tab, graph = transport()
    browser.detached = False
    browser.fail_on = "Page.navigate"
    with pytest.raises(SessionLostError):
        graph.navigate("act_1234567890123456")
    replacement = InPageGraph(tab, sleep=lambda _: None)
    assert replacement.request("GET", "/original-account")["status"] == 200
    assert len(browser.graph_calls) == 1


@pytest.mark.parametrize("seam", ["Page.getFrameTree", "Page.createIsolatedWorld", "(() =>", ".state()"])
def test_detachment_during_local_setup_recovers_at_the_same_boundary(seam):
    browser, tab, graph = transport()
    browser.detached = False
    graph._reset_world()
    browser.fail_on = seam
    assert graph.request("GET", "/existing-video")["status"] == 200
    assert len(browser.graph_calls) == 1
    assert tab.session_id == INCIDENT["recovered_session_id"]


def test_sent_graph_call_is_not_replayed_but_later_read_can_recover():
    browser, tab, graph = transport()
    browser.detached = False
    browser.fail_on = ".call("
    assert graph.request("GET", "/uncertain") == {"error": "call_unknown"}
    assert tab.session_id == INCIDENT["session_id"]  # no implicit recovery while sending
    assert graph.request("GET", "/reconcile")["status"] == 200
    calls = [p["expression"] for m, p, _ in browser.calls
             if m == "Runtime.evaluate" and ".call(" in p["expression"]]
    assert len(calls) == 2
    assert '"path": "/uncertain"' in calls[0]
    assert '"path": "/reconcile"' in calls[1]


@pytest.mark.parametrize("url,error", [
    ("https://adsmanager.facebook.com/adsmanager/manage/campaigns?act=1234567890123456", PageFailed),
    ("https://example.com/adsmanager/manage/campaigns?act=1234567890", PageFailed),
    ("https://adsmanager.facebook.com/other?act=1234567890", PageFailed),
    ("https://adsmanager.facebook.com/adsmanager/manage/campaigns", PageFailed),
    ("https://www.facebook.com/login.php", LoggedOut),
    ("https://www.facebook.com/checkpoint/", Checkpoint),
])
def test_wrong_page_account_login_or_checkpoint_stops_before_reattach(url, error):
    browser, tab, graph = transport()
    browser.url = url
    with pytest.raises(error):
        graph.request("GET", "/reconcile")
    assert sum(m == "Target.attachToTarget" for m, _, _ in browser.calls) == 1
    assert browser.graph_calls == []


@pytest.mark.parametrize("overrides", [{"targetId": "human-target"}, {"type": "iframe"}, {"subtype": "prerender"}])
def test_other_or_unusable_target_cannot_be_attached(overrides):
    browser, tab, graph = transport()
    browser.target_overrides = overrides
    with pytest.raises(PageFailed):
        graph.request("GET", "/reconcile")
    assert sum(m == "Target.attachToTarget" for m, _, _ in browser.calls) == 1


def test_identity_is_checked_again_after_attach_and_before_later_reads():
    browser, tab, graph = transport()
    browser.after_attach_url = "https://www.facebook.com/login.php"
    with pytest.raises(LoggedOut):
        graph.request("GET", "/reconcile")
    assert browser.graph_calls == []

    browser, tab, graph = transport()
    assert graph.request("GET", "/first")["status"] == 200
    browser.url = "https://adsmanager.facebook.com/adsmanager/manage/campaigns?act=1234567890123456"
    with pytest.raises(PageFailed):
        graph.request("GET", "/second")
    assert len(browser.graph_calls) == 1


@pytest.mark.parametrize("error", [CdpError("CDP connection closed"), OSError("unavailable"), TimeoutError("timeout")])
def test_unavailable_browser_is_a_sticky_typed_stop_without_reconnection(error):
    browser, tab, graph = transport()
    browser.browser_error = error
    with pytest.raises(PageFailed):
        graph.request("GET", "/reconcile")
    calls = len(browser.calls)
    with pytest.raises(PageFailed):
        graph.state()
    assert len(browser.calls) == calls
    assert sum(m == "Target.createTarget" for m, _, _ in browser.calls) == 1


def test_repeat_detachment_stops_after_one_recovery_for_the_tab_lifetime():
    browser, tab, graph = transport()
    assert graph.request("GET", "/first")["status"] == 200
    browser.detached = True
    with pytest.raises(PageFailed):
        graph.request("GET", "/second")
    assert sum(m == "Target.attachToTarget" for m, _, _ in browser.calls) == 2
    assert len(browser.graph_calls) == 1


@pytest.mark.parametrize("kind,has_token,error", [
    ("login", True, LoggedOut), ("checkpoint", True, Checkpoint),
    ("ads_manager", False, PageFailed), ("other", True, PageFailed),
])
def test_recovery_does_not_bypass_page_state_or_missing_token(kind, has_token, error):
    browser, tab, graph = transport()
    browser.kind = kind
    browser.has_token = has_token
    with pytest.raises(error):
        graph.request("GET", "/reconcile")
    assert browser.graph_calls == []
    assert not any(m in ("Page.reload", "Network.enable") for m, _, _ in browser.calls)


def test_cancel_during_recovery_propagates_without_a_graph_call():
    browser, tab, graph = transport()
    browser.browser_error = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        graph.request("GET", "/reconcile")
    assert browser.graph_calls == []


def test_page_tab_never_replays_or_recovers_an_arbitrary_sent_call():
    browser, tab, graph = transport()
    before = len(browser.calls)
    with pytest.raises(SessionLostError):
        tab.evaluate_in(1, "arbitrary_side_effect()")
    assert len(browser.calls) == before + 1
    assert tab.session_id == INCIDENT["session_id"]
