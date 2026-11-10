"""ACL-2344: warm leases in the light mode (ACL_LIGHT_WORK_TAB=1). Fakes only: no Meta, no Multilogin, no CDP."""
from __future__ import annotations

import pytest

from src.fb_api import graph as g
from src.fb_api import launch
from src.fb_api import light_tab
from tests.test_acl1812_warm_sessions import ACT, OK_STATE, PID, FakeGraph, Rig


class FakeDoc:
    def __init__(self):
        self.intact = ""

    def document_intact(self, timeout=10.0):
        return self.intact


class LightGraph(FakeGraph):
    handover_reason = ""

    def __init__(self, tab):
        super().__init__(tab)
        self.acts = []
        self.received = 0
        self.retired = False
        self.light_recapture = None

    def bind_act(self, act):
        self.acts.append(act)

    def receive_from(self, src):
        self.received += 1
        return LightGraph.handover_reason

    def _eval_helper(self, tail, timeout=10.0):
        if tail == ".retire()":
            self.retired = True
        return True

    def live_state(self, timeout=10.0):
        st = super().live_state(timeout)
        return {**st, "light": getattr(self.tab, "light", None) is not None}


@pytest.fixture()
def rig(tmp_path, monkeypatch):
    r = Rig(tmp_path)
    r.rows = []
    r.light_fail = ""
    LightGraph.handover_reason = ""

    def fake_open(tab):
        if r.light_fail:
            return None, r.light_fail
        tab.light = FakeDoc()
        tab.url = light_tab.LIGHT_URL
        return tab.light, ""

    monkeypatch.setattr(light_tab, "open_light_document", fake_open)
    monkeypatch.setattr(launch, "_record", lambda row: r.rows.append(row))
    monkeypatch.setenv("ACL_LIGHT_WORK_TAB", "1")

    def make(tab):
        graph = LightGraph(tab)
        r.last_graph = graph
        return graph

    r.make = make
    r.warm_up()
    return r


def _light_rows(r):
    return [row for row in r.rows if row.get("phase") == "light_lease"]


def test_lease_start_hands_over_and_closes_the_heavy_tab(rig):
    lease = rig.checkout()
    capture, work = rig.tabs
    assert capture.navigations == 1 and work.navigations == 0      # one Ads Manager load, in the capture tab
    assert capture.closed and not work.closed
    sess = rig.reg.session(PID)
    assert sess.tab is work and sess.mode_kind == "light" and sess.target_id == work.target_id
    assert lease.tab is work and lease.graph.tab is work and lease.graph.acts == [ACT]
    assert lease.graph.light_recapture is not None
    assert work.target_id in rig.reg.known_targets()               # the restored-tab clean-up never closes it
    row = _light_rows(rig)[-1]
    assert row["mode"] == "light" and row["handover_ok"] and row["capture_tab_closed"]
    assert "url" not in repr(row).lower() or "light" in repr(row)
    lease.release()


def test_reuse_and_account_switch_never_load_ads_manager(rig):
    rig.checkout().release()
    rig.checkout().release()
    rig.checkout(act="act_987654321").release()
    assert len(rig.tabs) == 2 and sum(t.navigations for t in rig.tabs) == 1
    assert rig.last_graph.acts[-1] == "act_987654321"


def test_new_helper_mode_gets_the_token_from_the_same_tab(rig):
    rig.checkout(mode="read").release()
    lease = rig.checkout(mode="write")
    assert len(rig.tabs) == 2 and lease.graph.received == 1 and lease.graph.tab is rig.tabs[1]
    lease.release()


def test_handover_failure_falls_back_to_the_ads_manager_tab_and_keeps_it(rig):
    LightGraph.handover_reason = "handover_timeout"
    lease = rig.checkout()
    capture, work = rig.tabs
    assert work.closed and not capture.closed and lease.tab is capture
    sess = rig.reg.session(PID)
    assert sess.mode_kind == "ads_manager" and sess.light_fallback
    row = _light_rows(rig)[-1]
    assert row["handover_ok"] is False and row["fallback_reason"] == "handover_timeout"
    assert row["capture_tab_closed"] is False
    lease.release()
    rig.checkout().release()                                         # the fallback tab is kept: no new load
    assert len(rig.tabs) == 2 and capture.navigations == 1


def test_light_document_failure_falls_back(rig):
    rig.light_fail = "light_doc_wrong_origin"
    lease = rig.checkout()
    assert lease.tab is rig.tabs[0] and rig.tabs[1].closed
    assert _light_rows(rig)[-1]["fallback_reason"] == "light_doc_wrong_origin"
    lease.release()


def test_login_on_the_capture_tab_stops_and_opens_no_work_tab(rig):
    orig = LightGraph.wait_ready
    LightGraph.wait_ready = lambda self, timeout=90.0: {"kind": "login", "has_token": False}
    try:
        with pytest.raises(g.LoggedOut):
            rig.checkout()
    finally:
        LightGraph.wait_ready = orig
    assert len(rig.tabs) == 1 and rig.tabs[0].closed


def test_replaced_document_or_max_age_starts_a_fresh_light_tab(rig):
    rig.checkout().release()
    rig.tabs[1].light.intact = "light_doc_replaced"
    rig.checkout().release()
    assert len(rig.tabs) == 4 and rig.tabs[1].closed and rig.tabs[2].closed and not rig.tabs[3].closed
    rig.now[0] += 6 * 3600 + 1
    rig.checkout().release()
    assert len(rig.tabs) == 6 and rig.tabs[3].closed


def test_switch_off_drops_the_light_tab_and_runs_as_today(rig, monkeypatch):
    rig.checkout().release()
    monkeypatch.setenv("ACL_LIGHT_WORK_TAB", "0")
    lease = rig.checkout()
    assert rig.tabs[1].closed and lease.tab is rig.tabs[2] and len(rig.tabs) == 3
    assert rig.reg.session(PID).mode_kind == "ads_manager"
    lease.release()


def test_flag_off_opens_one_tab_only(tmp_path, monkeypatch):
    monkeypatch.delenv("ACL_LIGHT_WORK_TAB", raising=False)
    r = Rig(tmp_path)
    r.warm_up()
    r.checkout().release()
    r.checkout().release()
    assert len(r.tabs) == 1 and r.tabs[0].navigations == 1
    assert OK_STATE["kind"] == "ads_manager"
