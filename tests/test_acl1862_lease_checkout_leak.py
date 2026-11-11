"""ACL-1862 (part 2): G1 - `checkout` закрыт без замка; G4 - проверка утечки замка в конце задания.

Только подделки (MLX, сканер, вкладки); замок держит поток этого процесса. Настоящий второй
процесс - в tests/test_acl1862_lease_windows.py. Живых аккаунтов и Multilogin нет.
"""
from __future__ import annotations

import threading

import pytest

from src.fb_ads_rpa import profile_lease as pl
from src.fb_ads_rpa.operations import profile_lock, serialized_browser
from src.fb_api import warm
from tests.test_acl1812_warm_sessions import PID, Rig

OTHER = "6ba7b810-9dad-11d1-80b4-00c04fd430c8"


@pytest.fixture
def rig(tmp_path, strict_checkout_lease):
    return Rig(tmp_path)


@pytest.fixture
def journal(monkeypatch):
    events: list[dict] = []
    from src.fb_api import telemetry

    monkeypatch.setattr(telemetry, "record", lambda event, path=None: events.append(event))
    return events


def _free_in_other_thread(pid=PID) -> bool:
    out: list[bool] = []

    def probe():
        lease = profile_lock(pid)
        ok = lease.acquire(blocking=False)
        if ok:
            lease.release()
        out.append(ok)

    t = threading.Thread(target=probe)
    t.start()
    t.join(5)
    return out[0]


# ───────────────────────── G1: checkout fails closed ─────────────────────────

def test_checkout_without_the_lease_is_refused_and_touches_nothing(rig, journal):
    rig.warm_up()
    with pytest.raises(warm.WarmError) as err:
        rig.checkout()
    assert err.value.code == "profile_lease"
    assert rig.tabs == [] and rig.tab_urls == []
    assert {"event": "profile_lease_refused", "code": "profile_lease", "path": "checkout"} in journal


def test_checkout_with_the_lease_works_and_alias_spelling_converges(rig):
    rig.warm_up()
    with profile_lock("{" + PID.upper() + "}"):
        rig.checkout().release()


def test_lease_of_another_profile_does_not_open_checkout(rig):
    rig.warm_up()
    with profile_lock(OTHER), pytest.raises(warm.WarmError) as err:
        rig.checkout()
    assert err.value.code == "profile_lease"


def test_lease_held_by_another_thread_does_not_open_checkout(rig):
    rig.warm_up()
    taken, free = threading.Event(), threading.Event()

    def hold():
        with profile_lock(PID):
            taken.set()
            free.wait(10)

    t = threading.Thread(target=hold)
    t.start()
    assert taken.wait(5)
    try:
        with pytest.raises(warm.WarmError) as err:
            rig.checkout()
        assert err.value.code == "profile_lease"
    finally:
        free.set()
        t.join(5)


def test_non_uuid_profile_id_is_refused_not_crashed(rig):
    with pytest.raises(warm.WarmError) as err:
        rig.reg.checkout("not-a-uuid", account="A", act="act_1", mode="read", make_graph=rig.make)
    assert err.value.code == "profile_lease"


def test_warm_session_takes_the_lease_for_a_caller_without_one(monkeypatch):
    from contextlib import contextmanager

    from src.analysis_agent import account_spend_api as api

    seen = []

    @contextmanager
    def fake_held(account, act, key, profile_id, commands):
        seen.append(profile_lock(profile_id).held_by_current_thread())
        yield "session"

    monkeypatch.setattr(api, "_warm_session_held", fake_held)
    with api._warm_session("Acc", "act_1", "k", PID, False) as sess:
        assert sess == "session"
    assert seen == [True]
    assert _free_in_other_thread()                    # и отпущен после сессии


def test_warm_session_nested_in_a_read_reenters_without_deadlock(monkeypatch):
    from contextlib import contextmanager

    from src.analysis_agent import account_spend_api as api

    @contextmanager
    def fake_held(*_a):
        yield 1

    monkeypatch.setattr(api, "_warm_session_held", fake_held)
    with profile_lock(PID):
        with api._warm_session("Acc", "act_1", "k", PID, False):
            pass
        assert profile_lock(PID).held_by_current_thread()
    assert _free_in_other_thread()


# ───────────────────────── G4: job-end assertion ─────────────────────────

def test_clean_operation_writes_no_leak_event(journal):
    with pl.leak_watch(PID, "read"):
        with profile_lock(PID):
            pass
    assert journal == []


def test_leaked_acquire_is_journaled_and_released(journal):
    with pl.leak_watch(PID, "publish"):
        profile_lock(PID).acquire()                   # взял и не отпустил
    assert journal == [{"event": "profile_lease_leaked", "path": "publish"}]
    assert not profile_lock(PID).held_by_current_thread()
    assert _free_in_other_thread()


def test_inner_operation_inside_an_outer_one_is_not_a_leak(journal):
    with profile_lock(PID):
        with pl.leak_watch(PID, "read"), profile_lock(PID):
            pass
        assert profile_lock(PID).held_by_current_thread()
    assert journal == []


def test_leak_does_not_hide_the_operation_exception(journal):
    with pytest.raises(KeyError):
        with pl.leak_watch(PID, "read"):
            profile_lock(PID).acquire()
            raise KeyError("boom")
    assert journal == [{"event": "profile_lease_leaked", "path": "read"}]
    assert _free_in_other_thread()


@pytest.mark.parametrize("reading,path", [(True, "read"), (False, "write")])
def test_serialized_browser_reports_a_leaking_operation(journal, reading, path):
    @serialized_browser(key=lambda profile: profile, reading=reading)
    def op(profile):
        profile_lock(profile).acquire()
        return "result"

    assert op(PID) == "result"
    assert {"event": "profile_lease_leaked", "path": path} in journal
    assert _free_in_other_thread()


def test_serialized_browser_clean_operation_is_silent(journal):
    @serialized_browser(key=lambda profile: profile, reading=True)
    def op(profile):
        return 1

    assert op(PID) == 1
    assert journal == []


def test_publish_job_scope_uses_the_same_leak_watch():
    from src.web import publish_jobs

    assert publish_jobs._leak_watch is pl.leak_watch
