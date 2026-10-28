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


def _entry():
    return SimpleNamespace(profile_id=PID, folder_id="F", multilogin_instance="A", ad_account_id=ACT)


def test_closed_profile_is_opened_by_prepare_without_any_button(tmp_path):
    rig = Rig(tmp_path)
    rig.scan_result[PID] = ("none", None)
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
    queue.holds = lambda job_id: job_id == "job-1"
    rig.reg._slot_queue = lambda: queue
    from src.fb_ads_rpa import run_telemetry
    monkeypatch.setattr(run_telemetry, "current_job_id", lambda: "job-1")
    assert rig.reg.open_one("Acc", _entry())["ok"]
    assert not [c for c in rig.slot_calls if c[0] == "acquire"]


def test_frozen_tab_wait_is_dropped_so_the_hung_tab_is_replaced_before_publish(monkeypatch):
    api._COOLDOWN.clear()
    key = "k-B97"
    api._start_cooldown(key, SimpleNamespace(kind="page_unresponsive", retry_after_s=None))
    assert api._cooldown_state(key)[1] == "page_unresponsive"
    assert api.release_frozen_tab_cooldown(key) is True
    assert api._cooldown_state(key)[0] == 0
    api._start_cooldown(key, SimpleNamespace(kind="checkpoint", retry_after_s=None))
    assert api.release_frozen_tab_cooldown(key) is False and api._cooldown_state(key)[1] == "checkpoint"
    api._COOLDOWN.clear()


def test_frozen_tab_wait_stays_when_sessions_are_not_managed(monkeypatch):
    monkeypatch.setattr(connection, "settings", lambda: {"managed_sessions": False, "auto_reopen_manual": False})
    api._COOLDOWN.clear()
    api._start_cooldown("k", SimpleNamespace(kind="page_unresponsive", retry_after_s=None))
    assert api.release_frozen_tab_cooldown("k") is False and api._cooldown_state("k")[0] > 0
    api._COOLDOWN.clear()


def test_publish_open_on_a_frozen_profile_replaces_the_tab_instead_of_waiting(monkeypatch, tmp_path):
    from src.web import publish_api

    rig = Rig(tmp_path)
    rig.warm_up()
    monkeypatch.setattr(warm, "_REGISTRY", rig.reg)
    api._COOLDOWN.clear()
    monkeypatch.setattr(publish_api, "_resolve_entry", lambda label: _entry())
    monkeypatch.setattr(publish_api, "_make_graph", lambda tab, act, *, writes=True: rig.make(tab))
    monkeypatch.setattr(publish_api, "preflight_account", lambda client, act: SimpleNamespace())
    monkeypatch.setattr(publish_api, "TargetingResolver", lambda client, **k: object())
    monkeypatch.setattr(publish_api, "GraphClient", lambda graph, **kw: SimpleNamespace(graph=graph, **kw))
    from src.analysis_agent import account_spend as _as
    monkeypatch.setattr(_as, "_profile_key", lambda label, *a, **k: "k-" + PID)
    api._start_cooldown("k-" + PID, SimpleNamespace(kind="page_unresponsive", retry_after_s=None))
    pub = publish_api.ApiPublisher(dry_run=True)
    pub.open("Acc")                                          # no ApiPublishError("... waiting about 10 min")
    pub.finish_job("Acc", clean=True)
    api._COOLDOWN.clear()
