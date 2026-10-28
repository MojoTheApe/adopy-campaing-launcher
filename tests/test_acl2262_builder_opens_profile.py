"""ACL-2262: the Builder opens an account's profile by itself; publish never stops on a closed profile."""
from types import SimpleNamespace

import pytest

from src.analysis_agent import account_spend_api as api
from src.fb_api import connection, warm
from tests.test_acl1812_warm_sessions import ACT, PID, Rig


@pytest.fixture(autouse=True)
def _owner_settings(monkeypatch, tmp_path):
    monkeypatch.setenv("ACL_PROFILE_RESTART_COOLDOWN_S", "0")      # owner decision: stays 0
    monkeypatch.setattr(connection, "settings", lambda: {"managed_sessions": True, "auto_reopen_manual": False})


def closed_profile_rig(monkeypatch, tmp_path, opened_log=None, pid=PID):
    """Warm registry with fakes for a CLOSED profile: Publish/Check opens it through `open_one`.

    Used by the publish-open tests (checkpoint / logged out / frozen tab / dry run) that used to rely on
    the cold path; the stops they verify now happen on the warm path.
    """
    monkeypatch.setenv("ACL_PROFILE_RESTART_COOLDOWN_S", "0")
    rig = Rig(tmp_path)
    rig.scan_result[pid] = ("none", None)

    def start(p, folder, instance):
        if opened_log is not None:
            opened_log.append((p, folder, instance))
        return Rig._start(rig, p, folder, instance)

    rig.reg._start_profile = start
    monkeypatch.setattr(warm, "_REGISTRY", rig.reg)
    return rig


def _entry():
    return SimpleNamespace(profile_id=PID, folder_id="F", multilogin_instance="A", ad_account_id=ACT)


def test_closed_profile_is_opened_by_prepare_without_any_button(tmp_path):
    rig = Rig(tmp_path)
    rig.scan_result[PID] = ("none", None)
    with connection.opening_closed_profile():
        assert connection.prepare_existing(rig.reg, _entry(), "Acc") is True
    assert len(rig.started) == 1 and rig.reg.is_warm(PID)
    lease = rig.checkout()                                  # the action itself now runs on the warm tab
    lease.release()


def test_registered_profile_whose_process_vanished_is_reopened_not_reported_closed(tmp_path):
    rig = Rig(tmp_path)
    rig.warm_up()
    rig.checkout().release()
    rig.scan_result[PID] = ("none", None)                   # closed by hand / crashed overnight
    rig.reg.session(PID).verified = False
    with connection.opening_closed_profile():
        assert connection.prepare_existing(rig.reg, _entry(), "Acc") is True
    assert len(rig.started) == 1
    rig.checkout().release()


def test_profile_used_by_a_running_action_is_never_restarted_under_it(tmp_path):
    rig = Rig(tmp_path)
    sess = rig.warm_up()
    sess.owner = 12345
    rig.scan_result[PID] = ("none", None)
    assert connection.prepare_existing(rig.reg, _entry(), "Acc") is True   # the running action owns it
    assert not rig.started and rig.reg.is_warm(PID)


def test_no_message_sends_the_person_to_a_button_or_a_three_minute_wait(tmp_path):
    rig = Rig(tmp_path)
    rig.warm_up()
    rig.checkout().release()
    rig.tabs[0].closed = True
    rig.scan_result[PID] = ("none", None)                   # closed right after the prepare check
    rig.reg.session(PID).verified = False
    with pytest.raises(warm.WarmError) as ei:
        rig.checkout()
    text = str(ei.value)
    assert ei.value.code == "profile_closed"
    assert "Open profiles for today" not in text and "minutes" not in text


def test_restart_of_the_server_readopts_a_profile_that_is_still_open(tmp_path):
    rig = Rig(tmp_path)
    rig.warm_up()
    rig.checkout().release()
    # A new dashboard process: the registry is rebuilt from the store, the profile is still open.
    again = Rig(tmp_path)
    again.scan_result[PID] = ("port", 9333)
    assert again.reg.is_warm(PID)
    assert connection.prepare_existing(again.reg, _entry(), "Acc") is True
    again.checkout().release()
    assert not again.started, "a profile that is still open must not be started again"


def test_restart_with_a_new_port_adopts_the_profile_instead_of_reporting_a_restart(tmp_path):
    rig = Rig(tmp_path)
    rig.warm_up(port=9333)
    again = Rig(tmp_path)
    again.scan_result[PID] = ("port", 9444)
    assert connection.prepare_existing(again.reg, _entry(), "Acc") is True
    assert again.reg.session(PID).port == 9444
    again.checkout().release()


def test_job_holding_its_own_slot_does_not_ask_the_queue_for_another(tmp_path, monkeypatch):
    rig = Rig(tmp_path)
    rig.scan_result[PID] = ("none", None)
    queue = rig.reg._slot_queue()
    queue.holds = lambda job_id, profile="": job_id == "job-1"
    rig.reg._slot_queue = lambda: queue
    from src.fb_ads_rpa import run_telemetry
    monkeypatch.setattr(run_telemetry, "current_job_id", lambda: "job-1")
    assert rig.reg.open_one("Acc", _entry())["ok"]
    assert not [c for c in rig.slot_calls if c[0] == "acquire"]


def _freeze(key, kind="page_unresponsive"):
    api._start_cooldown(key, SimpleNamespace(kind=kind, retry_after_s=None))


def test_frozen_tab_wait_is_lifted_once_per_episode_and_rearmed_when_the_replacement_freezes():
    api._COOLDOWN.clear(); api._FROZEN_RELEASED.clear()
    key = "k-B97"
    _freeze(key)
    assert api.frozen_tab_replaceable(key) is True
    assert api.release_frozen_tab_cooldown(key) is True and api._cooldown_state(key)[0] == 0
    _freeze(key)                                  # the replacement tab froze as well: full pause
    assert api._cooldown_state(key)[0] > 590
    assert api.frozen_tab_replaceable(key) is False and api.release_frozen_tab_cooldown(key) is False
    assert api._cooldown_state(key)[0] > 590, "the pause must stand for the next action"
    api.note_tab_recovered(key)                   # a tab answered: the next freeze is a new episode
    assert api.frozen_tab_replaceable(key) is True
    api._COOLDOWN.clear(); api._FROZEN_RELEASED.clear()


def test_other_waits_are_never_lifted():
    api._COOLDOWN.clear(); api._FROZEN_RELEASED.clear()
    _freeze("k", "checkpoint")
    assert api.frozen_tab_replaceable("k") is False and api.release_frozen_tab_cooldown("k") is False
    assert api._cooldown_state("k")[1] == "checkpoint"
    api._COOLDOWN.clear()


def test_frozen_tab_wait_stays_when_sessions_are_not_managed(monkeypatch):
    monkeypatch.setattr(connection, "settings", lambda: {"managed_sessions": False, "auto_reopen_manual": False})
    api._COOLDOWN.clear(); api._FROZEN_RELEASED.clear()
    _freeze("k")
    assert api.frozen_tab_replaceable("k") is False and api._cooldown_state("k")[0] > 0
    api._COOLDOWN.clear()


def test_publish_open_on_a_frozen_profile_replaces_the_tab_once_then_keeps_the_pause(monkeypatch, tmp_path):
    from src.analysis_agent import account_spend as _as
    from src.fb_api.graph import PageUnresponsive
    from src.web import publish_api

    rig = Rig(tmp_path)
    rig.warm_up()
    monkeypatch.setattr(warm, "_REGISTRY", rig.reg)
    api._COOLDOWN.clear(); api._FROZEN_RELEASED.clear()
    monkeypatch.setattr(publish_api, "_resolve_entry", lambda label: _entry())
    hang = {"on": False}

    def make(tab, act, *, writes=True):
        graph = rig.make(tab)
        if hang["on"]:
            def wait(**kw): raise PageUnresponsive("frozen")
            graph.wait_ready = wait
        return graph

    monkeypatch.setattr(publish_api, "_make_graph", make)
    monkeypatch.setattr(publish_api, "preflight_account", lambda client, act: SimpleNamespace())
    monkeypatch.setattr(publish_api, "TargetingResolver", lambda client, **k: object())
    monkeypatch.setattr(publish_api, "GraphClient", lambda graph, **kw: SimpleNamespace(graph=graph, **kw))
    monkeypatch.setattr(_as, "_profile_key", lambda label, *a, **k: "k-" + PID)
    key = "k-" + PID
    # Episode 1: frozen, the replacement answers -> the action goes on, the next freeze is a new episode.
    _freeze(key)
    pub = publish_api.ApiPublisher(dry_run=True)
    pub.open("Acc")
    pub.finish_job("Acc", clean=True)
    assert api._cooldown_state(key)[0] == 0 and not api._FROZEN_RELEASED
    # Episode 2: frozen, the replacement freezes too -> full pause, which then blocks the next action.
    hang["on"] = True
    rig.reg._drop_tab(rig.reg.session(PID))      # the old tab is gone; every new one hangs
    _freeze(key)
    with pytest.raises(publish_api.ApiPublishError):
        publish_api.ApiPublisher(dry_run=True).open("Acc")
    tabs_after_first = len(rig.tabs)
    assert api._cooldown_state(key)[0] > 590
    with pytest.raises(publish_api.ApiPublishError) as ei:
        publish_api.ApiPublisher(dry_run=True).open("Acc")
    assert ei.value.code == "page_unresponsive" and len(rig.tabs) == tabs_after_first, "no new tab while paused"
    api._COOLDOWN.clear(); api._FROZEN_RELEASED.clear()


def test_a_frozen_pause_stands_when_the_profile_is_not_on_the_warm_path(monkeypatch, tmp_path):
    from src.analysis_agent import account_spend as _as
    from src.web import publish_api

    rig = Rig(tmp_path)
    rig.scan_result[PID] = ("none", None)
    monkeypatch.setattr(warm, "_REGISTRY", rig.reg)
    api._COOLDOWN.clear(); api._FROZEN_RELEASED.clear()
    monkeypatch.setattr(_as, "_profile_key", lambda label, *a, **k: "k-" + PID)
    monkeypatch.setattr(connection, "settings", lambda: {"managed_sessions": False, "auto_reopen_manual": False})
    monkeypatch.setattr(publish_api, "_resolve_entry", lambda label: _entry())
    monkeypatch.setattr(publish_api, "_open_profile", lambda *a: pytest.fail("a paused profile is not opened"))
    _freeze("k-" + PID)
    with pytest.raises(publish_api.ApiPublishError) as ei:
        publish_api.ApiPublisher(dry_run=True).open("Acc")
    assert ei.value.code == "page_unresponsive" and not rig.started
    api._COOLDOWN.clear(); api._FROZEN_RELEASED.clear()


# ---- owner rule: reads never open a closed profile ----

def test_a_read_of_a_closed_profile_does_not_open_it(tmp_path):
    rig = Rig(tmp_path)
    rig.scan_result[PID] = ("none", None)
    assert connection.prepare_existing(rig.reg, _entry(), "Acc") is False     # no flag: the cold read path decides
    assert not rig.started and not rig.reg.is_warm(PID)


def test_a_read_forgets_a_stale_record_but_does_not_start_the_profile(tmp_path):
    rig = Rig(tmp_path)
    rig.warm_up()
    rig.scan_result[PID] = ("none", None)
    assert connection.prepare_existing(rig.reg, _entry(), "Acc") is False
    assert not rig.started and rig.reg.session(PID) is None


def test_a_read_still_adopts_a_profile_that_is_already_open(tmp_path):
    rig = Rig(tmp_path)
    rig.scan_result[PID] = ("port", 9333)
    assert connection.prepare_existing(rig.reg, _entry(), "Acc") is True
    assert not rig.started and rig.reg.is_warm(PID)


def test_connection_prepare_route_opens_a_closed_profile(monkeypatch, tmp_path):
    from src.web import warm_api

    rig = Rig(tmp_path)
    rig.scan_result[PID] = ("none", None)
    monkeypatch.setattr(warm, "_REGISTRY", rig.reg)
    monkeypatch.setattr(warm_api, "_guard", lambda: None)
    monkeypatch.setattr(warm_api, "_entry", lambda label: _entry())
    monkeypatch.setattr(warm_api, "_registry", lambda: rig.reg)
    data, code = warm_api.prepare_connection("Acc")
    assert code == 200 and data["ok"] and len(rig.started) == 1


# ---- a profile that the previous job has just closed ----

def test_open_waits_until_the_profile_is_fully_closed_then_starts_once(tmp_path):
    rig = Rig(tmp_path)
    rig.scan_result[PID] = ("closing", None)
    looks = []

    def sleep(seconds):
        looks.append(seconds)
        if len(looks) == 3:
            rig.scan_result[PID] = ("none", None)       # Multilogin released the profile

    rig.reg._sleep = sleep
    result = rig.reg.open_one("Acc", _entry())
    assert result["ok"] and len(rig.started) == 1
    assert len(looks) == 3 and max(looks) <= warm.CLOSE_POLL_S, "a few local looks, no hammering"


def test_open_gives_a_short_clear_message_when_the_profile_stays_closing(tmp_path):
    rig = Rig(tmp_path)
    rig.scan_result[PID] = ("closing", None)
    rig.reg._sleep = lambda seconds: rig.now.__setitem__(0, rig.now[0] + seconds)
    result = rig.reg.open_one("Acc", _entry())
    assert not result["ok"] and result["code"] == "profile_closing" and not rig.started
    assert "still closing" in result["message"] and "minutes" not in result["message"]
    assert rig.now[0] - 1_800_000_000.0 <= warm.CLOSE_WAIT_S + warm.CLOSE_POLL_S, "the wait is bounded"


def test_a_profile_lock_error_from_multilogin_is_one_clear_message_not_a_retry_loop(tmp_path):
    rig = Rig(tmp_path)
    rig.scan_result[PID] = ("none", None)
    calls = []

    def start(pid, folder, instance):
        calls.append(pid)
        raise RuntimeError("Start profile failed HTTP 500: cannot lock profile")

    rig.reg._start_profile = start
    result = rig.reg.open_one("Acc", _entry())
    assert not result["ok"] and result["code"] == "profile_locked" and len(calls) == 1
    assert "Try again" in result["message"]


def test_prepare_with_the_flag_waits_for_a_closing_profile(tmp_path):
    rig = Rig(tmp_path)
    rig.scan_result[PID] = ("closing", None)
    rig.reg._sleep = lambda s: rig.scan_result.__setitem__(PID, ("none", None))
    with connection.opening_closed_profile():
        assert connection.prepare_existing(rig.reg, _entry(), "Acc") is True
    assert len(rig.started) == 1


def test_own_slot_must_belong_to_the_same_profile(tmp_path, monkeypatch):
    import threading

    from src.fb_ads_rpa import run_telemetry
    from src.web.profile_slots import ProfileSlots

    class Q:
        holds = ProfileSlots.holds

        def __init__(self):
            self._cond = threading.Condition()
            self._running = {"job-1": {"profile": "OTHER"}}
            self.acquired = []

        def acquire(self, slot_id, pid, **kw):
            self.acquired.append(pid)
            return {"shared": False}

        def release(self, slot_id): pass
        def others_on(self, *a, **k): return False

    q = Q()
    rig = Rig(tmp_path)
    rig.scan_result[PID] = ("none", None)
    rig.reg._slot_queue = lambda: q
    monkeypatch.setattr(run_telemetry, "current_job_id", lambda: "job-1")
    assert rig.reg.open_one("Acc", _entry())["ok"]
    assert q.acquired == [PID], "a slot held for another profile does not cover this one"


def test_messages_do_not_depend_on_the_button_or_a_three_minute_wait():
    from src.analysis_agent import account_spend as sp

    for text in (warm.HAND_MESSAGE, sp.HAND_OPEN_MESSAGE_API):
        assert "Open profiles for today" not in text and "3 minutes" not in text
    assert warm.HAND_MESSAGE == sp.HAND_OPEN_MESSAGE_API
