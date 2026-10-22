"""ACL-2117: профиль не открывают заново, пока MLX досохраняет его после остановки.

Живые прогоны TEST9095zb 22.10: три раза «Ads Manager did not finish loading (no Graph
session)» после Check plan -> остановка профиля -> Publish через ~2 мин. Здесь только
подставные часы, подставной MLX и подставная сессия — ни MLX, ни Meta.
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from src.analysis_agent import account_spend as _as
from src.fb_api import launch
from src.fb_api import profile as prof
from src.fb_api import stop_log
from src.meta_campaign_agent.multilogin_launcher import StartedProfile

PID = "P1"


class _Clock:
    def __init__(self, now=1000.0):
        self.now = now
        self.slept: list[float] = []

    def wall(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


class _Launcher:
    def __init__(self):
        self.calls = 0

    def start_profile(self, folder_id, profile_id, *, headless=False):
        self.calls += 1
        return StartedProfile(profile_id, "F1", port="7001", newly_started=True)


def _reset():
    launch._COLD.clear()
    launch._COLD_PROFILE.clear()
    launch._READY.clear()
    launch._WAITED.clear()
    launch._REOPENED.clear()


@pytest.fixture
def world(monkeypatch, tmp_path):
    clock = _Clock()
    events: list[dict] = []
    log = stop_log.StopLog(tmp_path / "stops.json", wall=clock.wall)
    monkeypatch.setattr(stop_log, "_DEFAULT", log)
    monkeypatch.setattr(stop_log, "_sleep", clock.sleep)
    monkeypatch.setattr(launch, "_record", events.append)
    monkeypatch.setattr(launch, "_PACER", launch.StartPacer(gap=lambda: 0.0))
    monkeypatch.setattr(prof, "running_state", lambda l, p: ("not_running", None))
    monkeypatch.setenv(stop_log.RESTART_COOLDOWN_ENV, "180")
    monkeypatch.setenv(stop_log.RESTART_MAX_WAIT_ENV, "300")
    from src.fb_api import mlx_session
    monkeypatch.setattr(mlx_session, "note_failure", lambda *a, **k: None)
    _reset()
    yield SimpleNamespace(clock=clock, events=events, log=log)
    _reset()


def _open(launcher, probe=lambda pid: ("none", None), clock=None, **kw):
    return prof.open_profile(PID, "F1", "A", launcher_factory=lambda i: launcher, local_probe=probe,
                             sleep=(clock.sleep if clock else (lambda s: None)), **kw)


# --- item 2: reopen during sync waits ---------------------------------------------------

def test_reopen_during_sync_waits_for_the_cooldown_then_starts(world):
    world.log.record(PID)
    world.clock.now += 120                      # B88: reopened about 2 min after the stop
    notes: list[str] = []
    launcher = _Launcher()
    opened = _open(launcher, clock=world.clock, on_wait=notes.append)
    assert launcher.calls == 1 and opened.port == "7001"
    assert sum(world.clock.slept) >= 60         # the rest of the pause (180 - 120)
    assert world.clock.now - 1000.0 >= 180
    assert stop_log.SYNC_WAIT_MESSAGE in notes and notes[-1] == ""
    assert stop_log.SYNC_WAIT_MESSAGE == "Waiting for the profile to finish saving (cloud sync)"


def test_the_action_does_not_fail_inside_the_pause(world):
    world.log.record(PID)
    launcher = _Launcher()
    _open(launcher, clock=world.clock)          # used to raise restart_cooldown
    assert launcher.calls == 1


def test_a_wait_longer_than_the_cap_still_refuses(world, monkeypatch):
    monkeypatch.setenv(stop_log.RESTART_MAX_WAIT_ENV, "10")
    world.log.record(PID)
    launcher = _Launcher()
    with pytest.raises(prof.ProfileError) as err:
        _open(launcher, clock=world.clock)
    assert err.value.code == "restart_cooldown" and launcher.calls == 0 and world.clock.slept == []


def test_a_cancelled_wait_gives_the_refusal_back(world):
    world.log.record(PID)
    why = stop_log.wait_or_refusal(PID, cancelled=lambda: True)
    assert "wait about" in why


def test_mlx_remembering_the_old_port_is_asked_again_after_the_wait(world, monkeypatch):
    answers = iter([("running", "61000"), ("not_running", None)])
    monkeypatch.setattr(prof, "running_state", lambda l, p: next(answers))
    world.log.record(PID)
    launcher = _Launcher()
    opened = _open(launcher, clock=world.clock)
    assert launcher.calls == 1 and opened.started_now is True


# --- item 3: "closing" waits, it does not fail ------------------------------------------

def test_an_action_on_a_closing_profile_waits_then_runs(world):
    states = iter([("closing", 63405)] * 7 + [("none", None)])    # 14 s, more than the old 6 s
    launcher = _Launcher()
    opened = _open(launcher, probe=lambda pid: next(states), clock=world.clock)
    assert launcher.calls == 1 and opened.port == "7001"
    assert sum(world.clock.slept) == 14.0


def test_the_closing_wait_is_bounded_by_the_stop_rule(world):
    assert prof.CLOSING_WAIT_S == launch.STOP_GONE_WAIT_S
    with pytest.raises(prof.ProfileError) as err:
        _open(_Launcher(), probe=lambda pid: ("closing", 63405), clock=world.clock)
    assert err.value.code == "profile_closing"
    assert sum(world.clock.slept) <= launch.STOP_GONE_WAIT_S


# --- no change when the profile is open / was never stopped -----------------------------

def test_an_already_open_profile_is_attached_without_any_wait(world):
    world.log.record(PID)
    launcher = _Launcher()
    opened = _open(launcher, probe=lambda pid: ("port", 63405), clock=world.clock)
    assert opened.port == "63405" and launcher.calls == 0 and world.clock.slept == []


def test_a_profile_that_was_never_stopped_starts_at_once(world):
    launcher = _Launcher()
    _open(launcher, clock=world.clock)
    assert launcher.calls == 1 and world.clock.slept == []


# --- item 4: telemetry ------------------------------------------------------------------

def test_telemetry_links_stop_wait_reopen_and_the_graph_session(world):
    world.log.record(PID)
    world.clock.now += 120
    _open(_Launcher(), clock=world.clock)
    launch.note_graph_session(PID, False, account="KLD_B88", wall=world.clock.wall)
    by = {e["event"]: e for e in world.events}
    assert by["fb_api_profile_sync_wait"]["sync_status"] == "cooldown"
    assert by["fb_api_profile_sync_wait"]["waited_s"] >= 60
    reopen = by["fb_api_profile_reopen"]
    assert reopen["stop_at"] == 1000.0 and reopen["stop_age_s"] >= 180
    assert reopen["sync_status"] == "cooldown_waited"
    graph = by["fb_api_reopen_graph_session"]
    assert graph["has_graph_session"] is False and graph["account"] == "KLD_B88"
    launch.note_graph_session(PID, True)       # one event per reopen
    assert [e["event"] for e in world.events].count("fb_api_reopen_graph_session") == 1


def test_a_first_start_without_a_recorded_stop_writes_no_reopen_event(world):
    _open(_Launcher(), clock=world.clock)
    assert not [e for e in world.events if e["event"] == "fb_api_profile_reopen"]


def test_the_stop_event_carries_stop_and_exit_times(world):
    launch.record_profile_stop(account="A", profile_id=PID, stop_at=10.0, exit_at=25.5,
                               settled="ready", gone=True)
    (ev,) = world.events
    assert ev["event"] == "fb_api_profile_stop" and ev["stop_to_exit_s"] == 15.5 and ev["process_gone"] is True


# --- item 1: Check plan keeps the window for the Publish that follows -------------------

def _plan(monkeypatch, drafts):
    from src.analysis_agent import account_spend_api as _api
    from src.web import publish_api

    class _Pub:
        def __init__(self, **kw):
            pass

        def publish_draft(self, draft):
            return {"success": True}

    sess = SimpleNamespace(client=SimpleNamespace(allow_writes=False, writes=0, reset_budget=lambda n: None),
                           act="act_1", account="Acc", notes={}, keep_next=False, keep_grace_s=None)

    @contextmanager
    def session(account):
        yield sess

    monkeypatch.setattr(_api, "_session", session)
    monkeypatch.setattr(publish_api, "ApiPublisher", _Pub)
    monkeypatch.setattr(publish_api, "preflight_account", lambda c, a: {})
    monkeypatch.setattr(publish_api, "TargetingResolver", lambda c: object())
    publish_api._plan_in_read_session("Acc", drafts)
    return sess


def _draft(errors=()):
    return SimpleNamespace(errors=list(errors), warnings=[], name="C")


def test_check_plan_keeps_the_window_for_the_publish_that_follows(monkeypatch):
    sess = _plan(monkeypatch, [_draft()])
    assert sess.keep_next is True and sess.keep_grace_s == 600.0


def test_check_plan_keep_time_is_configurable_and_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("ACL_PLAN_KEEP_OPEN_S", "0")
    sess = _plan(monkeypatch, [_draft()])
    assert sess.keep_next is False and sess.keep_grace_s is None


def test_check_plan_with_only_broken_drafts_does_not_hold_the_window(monkeypatch):
    sess = _plan(monkeypatch, [_draft(["no video"])])
    assert sess.keep_next is False


def test_the_kept_window_closes_by_its_own_term_not_the_short_grace(monkeypatch):
    timers = []

    class _T:
        def __init__(self, interval, fn, args=()):
            timers.append(interval)
            self.daemon = False

        def start(self):
            pass

    monkeypatch.setattr(threading, "Timer", _T)
    opened = _as._ReadOpen("Acc", started=True, profile_id=PID)
    _as._finish_started(opened, keep_for_next=True, grace_s=600.0)
    _as._finish_started(opened, keep_for_next=True)
    assert timers == [600.0, _as.CLOSE_GRACE_S]
    _as._PENDING_CLOSE.clear()
