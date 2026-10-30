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


def test_first_call_after_profile_open_waits_even_on_a_quiet_machine(tmp_path):
    rig = _rig(tmp_path, 12.0)
    graph = _graph(rig, writes=False, freeze_until=30.0)
    graph._ready_for_call()
    assert rig.now[0] - T0 >= 30.0


def test_an_old_lease_on_a_quiet_machine_still_stops_at_once_when_nothing_is_read(tmp_path):
    rig = _rig(tmp_path, 12.0)
    graph = _graph(rig, writes=False, freeze_until=300.0)
    rig.now[0] += warm.FRESH_SESSION_S + 1
    start = rig.now[0]
    with pytest.raises(PageUnresponsive) as stop:
        graph._ready_for_call()
    assert rig.now[0] - start < 1.0 and stop.value.tab_load["waited_s"] == 0.0


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
    rig.now[0] += warm.FRESH_SESSION_S + 1            # not the first-call grace: the stall path alone
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
    rig.now[0] += warm.FRESH_SESSION_S + 1
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
