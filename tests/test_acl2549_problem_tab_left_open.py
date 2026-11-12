"""ACL-2549 (part C of ACL-2091): a problem tab and window stay open at the end of a job.

Fakes only: no Meta, no Multilogin, no live CDP.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.fb_api import graph as g
from src.fb_api import warm
from src.fb_api.cdp import PageTab
from src.web import profile_slots as ps
from src.web import publish_api
from src.web import publish_jobs as pj
from tests.test_acl1812_warm_sessions import CHECKPOINT_URL, LOGIN_URL, PID, FakeTab, Rig  # noqa: F401
from tests.test_acl1760_api_read import ACCOUNT, D, world  # noqa: F401
from tests.test_acl2113_keep_profile_open import FakePublisher, _key, jobs  # noqa: F401


# -- PageTab.leave_open

class FakeCdp:
    def __init__(self):
        self.calls: list[str] = []
        self.closed = False

    def call(self, method, params=None, **kw):
        self.calls.append(method)
        return {}

    def close(self):
        self.closed = True


def test_leave_open_never_closes_the_target_and_only_ends_our_connection():
    cdp = FakeCdp()
    tab = PageTab(cdp, "TGT1", "S1")
    assert "TGT1" in PageTab._LIVE
    tab.leave_open()
    assert tab.closed is True
    assert "TGT1" not in PageTab._LIVE
    assert cdp.closed is True
    assert not any(m in ("Target.closeTarget", "Page.navigate", "Page.reload") for m in cdp.calls)
    tab.leave_open()          # idempotent
    tab.close()               # a closed tab does nothing more
    assert "Target.closeTarget" not in cdp.calls


# -- warm path

class LeaveTab(FakeTab):
    def __init__(self, n):
        super().__init__(n)
        self.left = False

    def leave_open(self):
        self.left = True


@pytest.fixture()
def rig(tmp_path):
    r = Rig(tmp_path)

    def tab(url):
        r.tab_urls.append(url)
        t = LeaveTab(len(r.tabs) + 1)
        r.tabs.append(t)
        return t

    r.reg._tab_factory = tab
    r.warm_up()
    return r


@pytest.mark.parametrize("url,exc,kind", [(CHECKPOINT_URL, g.Checkpoint, "checkpoint"),
                                          (LOGIN_URL, g.LoggedOut, "logged_out")])
def test_warm_page_stop_leaves_the_tab_open_and_protects_it(rig, url, exc, kind):
    rig.checkout().release()
    rig.tabs[0].url = url
    with pytest.raises(exc):
        rig.checkout()
    tab = rig.tabs[0]
    assert tab.left is True and tab.closed is False
    assert tab.target_id in rig.reg.known_targets(), "known_targets() keeps the clean-up away from it"
    assert rig.reg.session(PID).problem_kind == kind


def test_chip_says_security_check_for_a_checkpoint_and_logged_out_for_a_login(rig):
    rig.checkout().release()
    rig.tabs[0].url = CHECKPOINT_URL
    with pytest.raises(g.Checkpoint):
        rig.checkout()
    chip = rig.reg._state_one(PID, "port", 9333, busy=False)
    assert chip["state"] == warm.CHECKPOINT and chip["text"] == "Security check"
    rig.reg.session(PID).problem_kind = "logged_out"
    assert rig.reg._state_one(PID, "port", 9333, busy=False)["state"] == warm.LOGGED_OUT


def test_fresh_tab_on_a_login_page_is_left_open(rig):
    real_make = rig.make

    def make(tab):
        graph = real_make(tab)
        graph.fresh_state = {"kind": "login", "has_token": False}
        return graph

    rig.make = make
    with pytest.raises(g.LoggedOut):
        rig.checkout()
    assert rig.tabs[0].left is True and rig.tabs[0].closed is False


def test_policy_block_at_the_end_of_a_lease_leaves_the_tab_open(rig):
    lease = rig.checkout()
    tab = rig.tabs[0]
    lease.release(g.PolicyBlocked("blocked", code=368))
    assert tab.left is True and tab.closed is False
    assert tab.target_id in rig.reg.known_targets()
    assert rig.reg.session(PID).problem_kind == "policy_block"


def test_frozen_tab_is_still_dropped(rig):
    lease = rig.checkout()
    tab = rig.tabs[0]
    lease.release(g.PageUnresponsive("frozen"))
    assert tab.closed is True and tab.left is False


def test_token_only_logout_is_not_a_login_page_and_drops_the_tab(rig):
    lease = rig.checkout()
    tab = rig.tabs[0]
    lease.release(g.LoggedOut("token expired"))          # page_seen False
    assert tab.closed is True and tab.left is False


def test_next_good_checkout_closes_the_left_tab_through_stale_target_and_clears_the_kind(rig):
    rig.checkout().release()
    rig.tabs[0].url = CHECKPOINT_URL
    with pytest.raises(g.Checkpoint):
        rig.checkout()
    left = rig.tabs[0]
    rig.checkout().release()                              # the buyer solved it, a new tab is made
    assert len(rig.tabs) == 2
    assert rig.tabs[1].closed_targets == [left.target_id]
    assert rig.reg.session(PID).problem_kind == ""


def test_a_tab_without_leave_open_falls_back_to_the_old_close(tmp_path):
    r = Rig(tmp_path)
    r.warm_up()
    r.checkout().release()
    r.tabs[0].url = CHECKPOINT_URL
    with pytest.raises(g.Checkpoint):
        r.checkout()
    assert r.tabs[0].closed is True


# -- cold read and cold publish

@pytest.mark.parametrize("stop,kept", [(g.Checkpoint("c"), True), (g.LoggedOut("l", page_seen=True), True),
                                       (g.PolicyBlocked("p", code=368), True),
                                       (g.LoggedOut("token"), False), (g.PageUnresponsive("f"), False),
                                       (g.RateLimited("r"), False), (None, False)])
def test_keeps_tab_open_by_kind(stop, kept):
    assert g.keeps_tab_open(stop) is kept


@pytest.mark.parametrize("stop,leaves", [(g.Checkpoint("c"), True), (g.PolicyBlocked("p", code=368), True),
                                         (g.PageUnresponsive("f"), False)])
def test_cold_publish_close_leaves_or_closes(stop, leaves):
    tab = LeaveTab(1)
    me = SimpleNamespace(_lease=None, stop=stop, _tab=tab, account_label="A")
    publish_api.ApiPublisher.close(me)
    assert tab.left is leaves and tab.closed is (not leaves)


def test_cold_read_on_a_checkpoint_page_leaves_the_tab_open(world):
    from src.analysis_agent import account_spend_api as api

    world.page.state_value = {"kind": "checkpoint", "has_token": False}
    r = api.read_via_api(ACCOUNT, D, D)
    assert r.state == "failed"
    assert world.cdp.closed_targets == []
    assert "T-mine" not in world.cdp.closed_targets


def test_cold_read_with_a_login_page_leaves_the_tab_open(world):
    from src.analysis_agent import account_spend_api as api

    world.page.state_value = {"kind": "login", "has_token": False}
    api.read_via_api(ACCOUNT, D, D)
    assert "T-mine" not in world.cdp.closed_targets


# -- finish_job and the job end

@pytest.fixture
def real_publisher(monkeypatch):
    stops = []
    monkeypatch.setattr(publish_api, "_stop_profile", lambda e, label: stops.append(label))
    from src.fb_api import stop_log
    monkeypatch.setattr(stop_log, "record_stop", lambda pid: None)

    def make(stop, *, started=True):
        pub = publish_api.ApiPublisher()
        pub._opened = SimpleNamespace(started_now=started)
        pub._entry = SimpleNamespace(profile_id="")
        pub.stop = stop
        return pub

    make.stops = stops
    return make


def test_keep_switch_off_and_a_checkpoint_with_nothing_written_does_not_stop_the_profile(real_publisher, monkeypatch):
    monkeypatch.setenv("FB_RPA_KEEP_PROFILES_OPEN", "0")
    out = real_publisher(g.Checkpoint("c")).finish_job("A", clean=True)
    assert out == "kept_problem" and real_publisher.stops == []


def test_warm_job_that_did_not_start_the_profile_still_reports_kept_problem(real_publisher):
    assert real_publisher(g.PolicyBlocked("p", code=368), started=False).finish_job("A", clean=True) == "kept_problem"


def test_token_only_logout_is_not_kept_and_closes_as_before(real_publisher, monkeypatch):
    monkeypatch.setenv("FB_RPA_KEEP_PROFILES_OPEN", "0")
    out = real_publisher(g.LoggedOut("token")).finish_job("A", clean=True)
    assert out == "closed" and real_publisher.stops == ["A"]


def test_a_profile_closed_by_the_auto_resume_is_not_kept(real_publisher, monkeypatch):
    monkeypatch.setenv("FB_RPA_KEEP_PROFILES_OPEN", "0")
    out = real_publisher(g.PageUnresponsive("f")).finish_job("A", clean=True, problem_window_open=False)
    assert out == "closed"


class ProblemPublisher(FakePublisher):
    calls: list = []

    def __init__(self, **kw):
        super().__init__(**kw)
        self.stop = g.Checkpoint("the profile is on a Facebook checkpoint")

    def problem_kind(self):
        return "checkpoint"

    def finish_job(self, account_label, *, clean, other_job_waits=False, problem_window_open=True):
        ProblemPublisher.calls.append(problem_window_open)
        return "kept_problem" if problem_window_open else "closed"

    @property
    def started_profile(self):
        return False            # warm path: the job did not start the profile


def test_job_end_keeps_a_review_slot_and_explains_the_problem(jobs, monkeypatch):
    monkeypatch.setattr(publish_api, "ApiPublisher", ProblemPublisher)
    _, job = jobs("KLD_A", writes=0)
    assert job["review_window"] is True and job["kept_kind"] == ps.KEPT_REVIEW
    assert job["profile_problem"]["kind"] == "checkpoint"
    assert "security check" in job["profile_problem"]["reason"].lower()
    assert "Retry" in job["profile_problem"]["next"]
    assert job["profile_problem"]["reason"] in job["creative_notices"]
    assert pj.API_NO_WRITE_NOTICE not in job["creative_notices"]
    assert [(r["profile"], r["kept_kind"]) for r in pj._slots.snapshot()["running"]] == [
        (_key("KLD_A"), ps.KEPT_REVIEW)]


def test_job_end_after_an_auto_resume_that_closed_the_profile_has_no_kept_problem(jobs, monkeypatch):
    ProblemPublisher.calls = []

    class ResumedPublisher(ProblemPublisher):
        def publish_draft(self, draft):
            with pj._jobs_lock:
                for j in pj._jobs.values():
                    j["profile_closed"] = True         # as `_auto_resume` does after a wait_timeout
            return super().publish_draft(draft)

    monkeypatch.setattr(publish_api, "ApiPublisher", ResumedPublisher)
    _, job = jobs("KLD_A", writes=0)
    assert ProblemPublisher.calls == [False]
    assert "profile_problem" not in job


# -- ProfileSlots.resolve_kept

def test_resolve_kept_moves_review_to_convenience_by_profile_and_resets_the_idle_clock():
    now = [100.0]
    slots = ps.ProfileSlots(memory=lambda: ps.MemoryReading(64000, 60000), clock=lambda: now[0], tick_s=0.01,
                            settings=lambda: ps.load_settings({}))
    with slots._cond:
        slots._running["j1"] = {"profile": "P1", "kept": True, "kept_kind": ps.KEPT_REVIEW, "label": "A", "kept_at": 1.0}
        slots._running["j2"] = {"profile": "P2", "kept": True, "kept_kind": ps.KEPT_REVIEW, "label": "B", "kept_at": 1.0}
        slots._running["j3"] = {"profile": "P1", "kept": False}
    now[0] = 500.0
    assert slots.resolve_kept("p1") == 1
    assert slots._running["j1"]["kept_kind"] == ps.KEPT_CONVENIENCE and slots._running["j1"]["kept_at"] == 500.0
    assert slots._running["j2"]["kept_kind"] == ps.KEPT_REVIEW
    assert slots.resolve_kept("P1") == 0
