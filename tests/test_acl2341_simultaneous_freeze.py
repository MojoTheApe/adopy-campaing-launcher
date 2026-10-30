"""ACL-2341: the first call after a profile opened waits like any freeze, a flickering CPU reading does not cancel
the wait, and several tabs freezing together are one machine/network stall. Fakes only."""
import pytest

from src.fb_api import warm
from src.fb_api.graph import PageUnresponsive
from tests.test_acl1812_warm_sessions import PID
from tests.test_acl2299_tabs_and_busy_freeze import _graph, _rig

T0 = 1_800_000_000.0


def test_first_call_freeze_on_a_busy_machine_waits_within_the_budget(tmp_path):
    # the live B95 stop: a read-only first call, busy machine, waited_s 0.0 - the CPU reading flickered
    rig = _rig(tmp_path, 80.6)
    graph = _graph(rig, writes=False, freeze_until=40.0)
    # the reading flickers around the busy line: busy on every other 5 s slot of the clock
    rig.reg._cpu = lambda: 80.6 if int((rig.now[0] - T0) // 5) % 2 == 0 else 30.0
    graph._ready_for_call()
    assert graph.calls > 3 and rig.now[0] - T0 >= 40.0, "waited instead of stopping with waited_s 0"


def test_a_read_only_graph_on_a_quiet_machine_waits_the_short_limit_and_leaves_a_patience_row(tmp_path):
    # live Check plan on Doc120: 66 % CPU, writes=False, stopped after ~30 s with no tab_patience row
    rig = _rig(tmp_path, 66.0)
    graph = _graph(rig, writes=False, freeze_until=10_000.0)
    rows = []
    from src.fb_ads_rpa import run_telemetry
    orig = run_telemetry.record
    run_telemetry.record = lambda *a, **k: rows.append((a, k))
    try:
        with pytest.raises(PageUnresponsive) as stop:
            graph._ready_for_call()
    finally:
        run_telemetry.record = orig
    assert 75.0 <= rig.now[0] - T0 < 90.0
    assert stop.value.tab_load["waited_patiently"] and stop.value.tab_load["waited_s"] >= 75.0
    assert any(k.get("note") == "tab_patience" for _a, k in rows)


def _second_lease(rig, graph, freeze_until):
    """A second profile's lease on the same registry (the two Multilogin browsers of the live run)."""
    from tests.test_acl1812_warm_sessions import PID as _pid  # noqa: F401
    other = warm.WarmSession(profile_id="other-profile", started_at=rig.now[0])
    other.account = "OTHER"
    rig.reg._sessions["other-profile"] = other
    from src.fb_api.token_capture import InPageGraph
    g2 = object.__new__(InPageGraph)
    g2.allow_writes = True
    g2._sleep = graph._sleep
    g2.calls = 0

    def ready_once(timeout=10.0):
        g2.calls += 1
        if rig.now[0] - T0 < freeze_until:
            raise PageUnresponsive("the Ads Manager tab stopped answering")

    g2._ready_once = ready_once
    g2.patience = rig.reg.patience_for(other)
    return g2


def test_two_leases_freezing_within_seconds_wait_as_one_machine_stall(tmp_path):
    rig = _rig(tmp_path, 12.0)                       # quiet CPU: only the shared stall can explain a long wait
    first = _graph(rig, writes=True, freeze_until=200.0)
    second = _second_lease(rig, first, freeze_until=200.0)
    t1 = rig.now[0]
    rig.reg.note_freeze(PID)
    rig.reg.note_freeze("other-profile")
    assert rig.reg.shared_stall(PID) and rig.reg.shared_stall("other-profile")
    first.patience.begin(PageUnresponsive("x"), writes=True)      # the idle limit alone would be 75 s
    ep = first.patience.begin(PageUnresponsive("x"), writes=True)
    assert ep is not None and ep.shared, "the stall gets the full budget, not the 75 s idle wait"
    second._ready_for_call()                          # waits it out and continues
    assert rig.now[0] - t1 >= 200.0 - (t1 - T0) or rig.now[0] - T0 >= 200.0


def test_a_stall_ends_in_the_all_accounts_paused_text(tmp_path, monkeypatch):
    monkeypatch.setenv("ACL_PATIENT_READY_S", "60")
    rig = _rig(tmp_path, 12.0)
    first = _graph(rig, writes=True, freeze_until=10_000.0)
    second = _second_lease(rig, first, freeze_until=10_000.0)
    rig.reg.note_freeze("other-profile")
    with pytest.raises(PageUnresponsive) as stop:
        first._ready_for_call()
    assert "all accounts paused together" in str(stop.value)
    assert stop.value.tab_load["shared_stall"] is True
    assert second is not None


def test_one_lonely_freeze_is_not_a_shared_stall(tmp_path):
    rig = _rig(tmp_path, 12.0)
    _graph(rig, writes=True, freeze_until=0.0)
    rig.reg.note_freeze(PID)
    assert not rig.reg.shared_stall(PID)
    rig.reg.note_freeze("other-profile")
    rig.now[0] += warm.SHARED_STALL_WINDOW_S + 5
    rig.reg.note_freeze(PID)
    assert not rig.reg.shared_stall(PID), "freezes minutes apart are two freezes"


# ---- a tab whose document was replaced is re-attached, not reported as frozen ----

class _Tab:
    def __init__(self):
        self.worlds = 0
        self.stale_ctx = {1}
        self.calls = []

    def create_isolated_world(self, name):
        self.worlds += 1
        return self.worlds + 1

    def evaluate_in(self, ctx, expr, timeout=60.0):
        from src.fb_api.cdp import CdpTimeout
        self.calls.append(ctx)
        if ctx in self.stale_ctx:
            raise CdpTimeout("Runtime.evaluate")      # a replaced document: the old context never answers
        return {"data": []}

    def browser_url(self):
        return "https://adsmanager.facebook.com/adsmanager/manage/campaigns?act=1"


def _live_graph(tab):
    from src.fb_api.token_capture import InPageGraph
    g = object.__new__(InPageGraph)
    g.tab = tab
    g._ctx = 1
    g._injected = True
    g.world_name = "w"
    g.name = "h"
    g._sleep = lambda s: None
    g._read_sent = False
    g._ready_for_call = lambda **k: None
    g._ensure = lambda: g._ctx if g._ctx is not None else g._new_ctx()
    g._new_ctx = lambda: setattr(g, "_ctx", tab.create_isolated_world("w")) or g._ctx
    return g


def test_a_read_whose_context_was_replaced_reattaches_and_continues():
    tab = _Tab()
    g = _live_graph(tab)
    assert g._read_once({"method": "GET", "path": "x"}, 5.0) == {"data": []}
    assert tab.worlds >= 2, "re-attached in a new world"


def test_a_tab_that_stays_silent_in_a_fresh_world_too_is_still_unresponsive():
    tab = _Tab()
    tab.stale_ctx = {1, 2, 3, 4, 5}
    g = _live_graph(tab)
    with pytest.raises(PageUnresponsive):
        g._read_once({"method": "GET", "path": "x"}, 5.0)
