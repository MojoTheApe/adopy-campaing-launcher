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
    assert not any("url" in str(k).lower() for k in row)
    assert not any(isinstance(v, str) and ("://" in v or "act_" in v) for v in row.values())
    lease.release()


def test_reuse_and_account_switch_never_load_ads_manager(rig):
    rig.checkout().release()
    rig.checkout().release()
    rig.checkout(act="act_987654321").release()
    assert len(rig.tabs) == 2 and sum(t.navigations for t in rig.tabs) == 1
    assert rig.last_graph.acts[-1] == "act_987654321"


def test_the_recapture_bound_is_per_lease(rig):
    lease = rig.checkout()
    lease.graph.light_recaptures = 2                                     # this lease used up its re-captures
    lease.release()
    assert rig.checkout().graph.light_recaptures == 0


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
    # the fallback is for THAT lease only: the next lease tries the light mode again (one fresh start)
    LightGraph.handover_reason = ""
    lease2 = rig.checkout()
    assert len(rig.tabs) == 4 and capture.closed and not rig.tabs[3].closed
    assert rig.reg.session(PID).mode_kind == "light" and not rig.reg.session(PID).light_fallback
    assert lease2.tab is rig.tabs[3] and _light_rows(rig)[-1]["handover_ok"] is True
    lease2.release()


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
    monkeypatch.setenv("ACL_LIGHT_WORK_TAB", "0")
    r = Rig(tmp_path)
    r.warm_up()
    r.checkout().release()
    r.checkout().release()
    assert len(r.tabs) == 1 and r.tabs[0].navigations == 1
    assert OK_STATE["kind"] == "ads_manager"


def test_two_consecutive_fallbacks_keep_the_ads_manager_tab_for_later_leases(rig):
    LightGraph.handover_reason = "handover_timeout"
    rig.checkout().release()                                             # fallback 1
    rig.checkout().release()                                             # fallback 2 -> limit reached
    sess = rig.reg.session(PID)
    assert sess.light_fallbacks == 2
    row = _light_rows(rig)[-1]
    assert row["light_fallbacks"] == 2 and row["light_disabled_for_profile"] is True
    assert _light_rows(rig)[-2]["light_disabled_for_profile"] is False
    tabs_before = len(rig.tabs)
    kept = sess.tab
    lease = rig.checkout()                                               # no drop, no new light attempt
    assert len(rig.tabs) == tabs_before and lease.tab is kept and not kept.closed
    lease.release()
    assert len(_light_rows(rig)) == 2


def test_a_successful_light_lease_resets_the_fallback_count(rig):
    LightGraph.handover_reason = "handover_timeout"
    rig.checkout().release()
    assert rig.reg.session(PID).light_fallbacks == 1
    LightGraph.handover_reason = ""
    rig.checkout().release()
    assert rig.reg.session(PID).light_fallbacks == 0 and rig.reg.session(PID).mode_kind == "light"


def test_acl2533_photo_leases_run_on_the_ads_manager_tab_with_the_light_switch_on(rig):
    """A form-photo upload needs the Ads Manager page (fb_dtsg/lsd) and a fresh graph per Page."""
    rig.checkout().release()                                             # a normal read: the light tab
    sess = rig.reg.session(PID)
    assert sess.mode_kind == "light"
    graphs = []
    for _ in range(2):                                                   # two uploads (two Pages) in a row
        lease = rig.checkout(mode="photo")
        assert lease.tab is sess.tab and sess.mode_kind == "ads_manager"
        assert lease.tab.url != light_tab.LIGHT_URL and getattr(lease.tab, "light", None) is None
        graphs.append(lease.graph)
        lease.release()
    assert graphs[0] is not graphs[1]                                    # never the previous upload's graph
    lease = rig.checkout()                                               # a normal read: light again
    assert sess.mode_kind == "light" and lease.tab.light is not None
    lease.release()


def test_unset_flag_leases_in_light_mode(rig, monkeypatch):
    """ACL-2591: no variable at all = the light work tab."""
    monkeypatch.delenv("ACL_LIGHT_WORK_TAB", raising=False)
    lease = rig.checkout()
    assert rig.reg.session(PID).mode_kind == "light"
    assert getattr(lease.tab, "light", None) is not None
    assert not [r for r in rig.rows if r.get("fallback_reason") == "flag_off"]
    lease.release()


@pytest.mark.parametrize("value", ["0", "false", "no", "off"])
def test_explicit_off_values_use_ads_manager_tab_with_flag_off(rig, monkeypatch, value):
    monkeypatch.setenv("ACL_LIGHT_WORK_TAB", value)
    lease = rig.checkout()
    assert rig.reg.session(PID).mode_kind == "ads_manager"
    assert getattr(lease.tab, "light", None) is None
    assert any(r.get("fallback_reason") == "flag_off" and r.get("mode") == "ads_manager" for r in rig.rows)
    lease.release()
