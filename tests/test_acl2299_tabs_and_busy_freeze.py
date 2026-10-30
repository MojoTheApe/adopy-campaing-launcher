"""ACL-2299: restored Ads Manager tabs are closed at a robot start (A); a tab that freezes in the middle of a job
is waited for on a busy machine (B) and in a Check plan read (C).

Fakes only: no Meta, no Multilogin, no real CPU, memory or clock.
"""
import pytest

from src.fb_api import warm
from src.fb_api.cdp import PageTab
from src.fb_api.graph import PageUnresponsive
from src.fb_api.token_capture import InPageGraph
from tests.test_acl1812_warm_sessions import PID, Rig
from tests.test_acl2217_owned_context import Browser, page

AM = "https://adsmanager.facebook.com/adsmanager/manage/campaigns?act=111"
BM = "https://business.facebook.com/adsmanager/reporting?act=222"


def _row(target, url, context="profile"):
    return {**page(context, target=target), "url": url}


def _open(browser):
    return PageTab.open("http://127.0.0.1:1", connect=lambda *_a, **_kw: browser)


# ---- A: restored tabs ----

def test_restored_ads_manager_tabs_are_closed_before_the_first_call_and_ours_is_kept(monkeypatch):
    from src.fb_api import launch
    events = []
    monkeypatch.setattr(launch, "_record", events.append)
    pages = [_row("r1", AM), _row("r2", BM), _row("r3", AM + "&x=1"), _row("r4", AM)]
    browser = Browser(pages, expected="profile")
    tab = _open(browser)
    try:
        assert sorted(browser.closed_targets) == ["r1", "r2", "r3"], "one restored page always stays"
        assert "owned" not in browser.closed_targets, "the tab we drive is never closed"
        assert (tab.page_count_before, tab.page_count_after, tab.closed_restored) == (4, 1, 3)
        ctx = [e for e in events if e.get("event") == "fb_api_tab_context"]
        assert ctx[0]["page_count"] == 4
        assert ctx[-1]["page_count_before"] == 4 and ctx[-1]["page_count_after"] == 1
    finally:
        tab.close()


def test_owners_other_tabs_are_untouched():
    pages = [_row("mail", "https://mail.example.com/inbox"), _row("doc", "https://docs.example.com/d/1"),
             _row("fbhome", "https://www.facebook.com/"), _row("blank", "about:blank"),
             _row("nourl", ""), _row("am", AM),
             _row("other_ctx", AM, context="another-context"),
             {**page("profile", target="sw", kind="service_worker"), "url": AM}]
    browser = Browser(pages, expected="profile")
    # a second named context makes the profile ambiguous - keep only the profile's own pages for this case
    browser.pages = [p for p in pages if p["targetId"] != "other_ctx"]
    tab = _open(browser)
    try:
        assert browser.closed_targets == ["am"]
        assert (tab.page_count_before, tab.page_count_after) == (6, 5)
    finally:
        tab.close()


def test_cleanup_failure_never_fails_the_open():
    class Flaky(Browser):
        def call(self, method, params=None, **kw):
            if method == "Target.closeTarget" and (params or {}).get("targetId") == "r1":
                raise RuntimeError("closed already")
            return super().call(method, params, **kw)

    browser = Flaky([_row("r1", AM), _row("r2", AM)], expected="profile")
    tab = _open(browser)
    try:
        assert tab.closed_restored == 1 and browser.closed_targets == ["r2"]
    finally:
        tab.close()


# ---- B / C: a freeze in the middle of a job ----

def _graph(rig, *, writes, freeze_until):
    """InPageGraph whose readiness fails with a frozen tab until the fake clock passes `freeze_until`."""
    graph = object.__new__(InPageGraph)
    graph.allow_writes = writes
    graph._sleep = rig._sleep_fake
    graph.calls = 0

    def ready_once(timeout=10.0):
        graph.calls += 1
        if rig.now[0] - 1_800_000_000.0 < freeze_until:
            raise PageUnresponsive("the Ads Manager tab stopped answering (now at adsmanager.facebook.com/adsmanager/x)")

    graph._ready_once = ready_once
    rig.warm_up()
    graph.patience = rig.reg.patience_for(rig.reg.session(PID))
    return graph


def _rig(tmp_path, cpu):
    rig = Rig(tmp_path)
    rig.reg._cpu = lambda: cpu
    return rig


def test_mid_job_freeze_under_busy_cpu_waits_out_the_patient_limit_and_continues(tmp_path):
    rig = _rig(tmp_path, 96.0)
    graph = _graph(rig, writes=True, freeze_until=300.0)
    graph._ready_for_call()                        # a write: nothing of it was sent yet, so waiting is safe
    assert graph.calls > 10 and rig.now[0] - 1_800_000_000.0 >= 300.0


def test_busy_cpu_does_not_wait_longer_than_the_patient_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("ACL_PATIENT_READY_S", "120")
    rig = _rig(tmp_path, 96.0)
    graph = _graph(rig, writes=True, freeze_until=10_000.0)
    with pytest.raises(PageUnresponsive) as stop:
        graph._ready_for_call()
    waited = rig.now[0] - 1_800_000_000.0
    assert 120.0 <= waited < 140.0
    load = stop.value.tab_load
    assert load["cpu_pct"] == 96.0 and load["waited_s"] >= 120.0 and load["busy"] is True


def test_idle_machine_waits_only_the_short_limit_then_keeps_the_2262_stop(tmp_path):
    # ACL-2316: was "no wait on an idle machine"; a publish tab now waits ACL_READ_IDLE_WAIT_S (75 s) first
    rig = _rig(tmp_path, 12.0)
    graph = _graph(rig, writes=True, freeze_until=300.0)
    with pytest.raises(PageUnresponsive) as stop:
        graph._ready_for_call()
    assert graph.calls > 1 and 75.0 <= rig.now[0] - 1_800_000_000.0 < 90.0
    assert stop.value.tab_load["cpu_pct"] == 12.0 and stop.value.tab_load["busy"] is False


def test_the_wait_ends_when_the_load_is_gone(tmp_path):
    rig = _rig(tmp_path, 96.0)
    graph = _graph(rig, writes=True, freeze_until=10_000.0)
    load = {"cpu": 96.0}
    rig.reg._cpu = lambda: load["cpu"]
    real_sleep = graph._sleep

    def sleep(s):
        real_sleep(s)
        if rig.now[0] - 1_800_000_000.0 >= 40:
            load["cpu"] = 10.0

    graph._sleep = sleep
    with pytest.raises(PageUnresponsive):
        graph._ready_for_call()
    # ACL-2341: the load is gone at 40 s, but a wait never ends before the 75 s idle wait of the same call
    assert 75.0 <= rig.now[0] - 1_800_000_000.0 < 90.0


def test_a_read_that_timed_out_is_asked_again_on_a_busy_machine(tmp_path):
    rig = _rig(tmp_path, 96.0)
    graph = _graph(rig, writes=False, freeze_until=0.0)
    asked = []

    def read_once(spec, timeout):
        asked.append(rig.now[0])
        if len(asked) < 4:
            raise PageUnresponsive("silent")
        return {"data": []}

    graph._read_once = read_once
    assert graph._patiently(lambda: graph._read_once({}, 15.0)) == {"data": []}
    assert len(asked) == 4


def test_nested_waits_share_one_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("ACL_PATIENT_READY_S", "60")
    rig = _rig(tmp_path, 96.0)
    graph = _graph(rig, writes=False, freeze_until=10_000.0)

    def read_once(spec, timeout):
        graph._ready_for_call()          # nested inside the read's wait

    graph._read_once = read_once
    with pytest.raises(PageUnresponsive):
        graph._patiently(lambda: graph._read_once({}, 15.0))
    assert rig.now[0] - 1_800_000_000.0 < 90.0, "60 s limit, not 120 s"


def test_check_plan_read_and_a_publish_step_wait_a_short_time_on_an_idle_machine(tmp_path):
    rig = _rig(tmp_path, 12.0)
    graph = _graph(rig, writes=False, freeze_until=60.0)
    with warm.read_patience():
        graph._ready_for_call()
    assert 60.0 <= rig.now[0] - 1_800_000_000.0 < 75.0

    # B4: a real freeze on an idle machine is not waited for 8 minutes: the short limit, then the stop
    rig = _rig(tmp_path / "long", 12.0)
    graph = _graph(rig, writes=False, freeze_until=10_000.0)
    with warm.read_patience(), pytest.raises(PageUnresponsive):
        graph._ready_for_call()
    assert 75.0 <= rig.now[0] - 1_800_000_000.0 < 90.0

    rig = _rig(tmp_path / "w", 12.0)
    graph = _graph(rig, writes=True, freeze_until=200.0)
    with warm.read_patience(), pytest.raises(PageUnresponsive):
        graph._ready_for_call()
    assert 75.0 <= rig.now[0] - 1_800_000_000.0 < 90.0      # ACL-2316: a publish step waits the same short limit

    rig = _rig(tmp_path / "r", 12.0)
    graph = _graph(rig, writes=False, freeze_until=200.0)
    with pytest.raises(PageUnresponsive):          # not Check plan: background reads keep the pause
        graph._ready_for_call()


def test_check_plan_prepare_on_an_idle_machine_keeps_todays_short_limit(tmp_path):
    from src.fb_api import cdp
    from tests.test_acl1812_warm_sessions import ACT

    for read_flag, expect in ((True, False), (False, False)):
        rig = _rig(tmp_path / str(read_flag), 12.0)
        rig.warm_up()
        seen = {}
        real_make = rig.make

        def make(tab, real_make=real_make, seen=seen):
            graph = real_make(tab)

            def wait_ready(timeout=90.0):
                seen["patient"] = cdp.patient_active()
                return {"kind": "ads_manager", "has_token": True}

            graph.wait_ready = wait_ready
            return graph

        rig.make = make
        if read_flag:
            with warm.read_patience():
                rig.reg.checkout(PID, account="Acc", act=ACT, mode="read", make_graph=make).release()
        else:
            rig.reg.checkout(PID, account="Acc", act=ACT, mode="read", make_graph=make).release()
        assert seen["patient"] is expect


def test_a_long_wait_beats_the_silence_watcher_and_is_shown_then_cleared(tmp_path, monkeypatch):
    from src.fb_ads_rpa import run_telemetry as rt
    beats, shown = [], []
    monkeypatch.setattr(rt, "beat", lambda: beats.append(1))
    rig = _rig(tmp_path, 96.0)
    graph = _graph(rig, writes=True, freeze_until=200.0)
    with warm.job_wait(lambda: False, shown.append):
        graph._ready_for_call()
    assert len(beats) >= 30, "every look keeps the job alive: no step_stuck"
    assert shown[0]["hold"] == "machine_busy" and shown[0]["cpu_pct"] == 96.0
    assert shown[-1] is None


def test_stop_by_the_user_ends_the_wait(tmp_path):
    rig = _rig(tmp_path, 96.0)
    graph = _graph(rig, writes=True, freeze_until=10_000.0)
    stopped = {"on": False}
    real_sleep = graph._sleep

    def sleep(s):
        real_sleep(s)
        stopped["on"] = rig.now[0] - 1_800_000_000.0 > 30

    graph._sleep = sleep
    with warm.job_wait(lambda: stopped["on"], lambda view: None), pytest.raises(PageUnresponsive):
        graph._ready_for_call()
    assert rig.now[0] - 1_800_000_000.0 < 50.0


def test_page_count_is_in_the_stop_row(tmp_path):
    from src.web import publish_api

    rig = _rig(tmp_path, 96.0)
    graph = _graph(rig, writes=True, freeze_until=10_000.0)
    sess = rig.reg.session(PID)
    sess.tab = type("T", (), {"page_count_before": 6, "page_count_after": 1, "closed_restored": 5})()
    rig.reg.annotate_stop(PageUnresponsive("x"), sess)
    stop = PageUnresponsive("x")
    rig.reg.annotate_stop(stop, sess)
    row = publish_api._tab_load(stop)
    assert row["page_count_before"] == 6 and row["page_count_after"] == 1 and row["closed_restored_tabs"] == 5
    assert row["cpu_pct"] == 96.0 and "memory_pct" in row
    assert publish_api._tab_load(None) == {} and publish_api._tab_load(ValueError("x")) == {}


def test_a_graph_without_patience_behaves_as_before():
    graph = object.__new__(InPageGraph)
    graph.allow_writes = True

    def boom(timeout=10.0):
        raise PageUnresponsive("silent")

    graph._ready_once = boom
    with pytest.raises(PageUnresponsive):
        graph._ready_for_call()
