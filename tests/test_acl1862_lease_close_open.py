"""ACL-1862 (PR1): «Close profiles» и «Open profiles» идут под замком физического профиля.

Только подделки (MLX-запуск/остановка, сканер процессов); держатель замка — другой поток
или настоящий дочерний процесс. Живых аккаунтов и Multilogin нет.
"""
from __future__ import annotations

import threading
from contextlib import contextmanager

import pytest

from src.fb_ads_rpa import profile_lease as pl
from src.fb_ads_rpa.operations import profile_lock
from src.fb_api import warm
from tests.test_acl1812_warm_sessions import PID, Rig, entry
from tests.test_profile_process_lease import process_factory  # noqa: F401 — фикстура


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


@pytest.fixture
def journal(monkeypatch):
    events: list[dict] = []
    from src.fb_api import telemetry

    monkeypatch.setattr(telemetry, "record", lambda event, path=None: events.append(event))
    return events


class _OtherThreadHolder:
    """Держит замок профиля в другом потоке — для этого процесса это «чужая операция»."""

    def __enter__(self):
        self._took, self._free = threading.Event(), threading.Event()

        def hold():
            with profile_lock(PID):
                self._took.set()
                self._free.wait(20)

        self._t = threading.Thread(target=hold, daemon=True)
        self._t.start()
        assert self._took.wait(5)
        return self

    def __exit__(self, *_):
        self._free.set()
        self._t.join(5)


def _warm_ours(rig):
    rig.warm_up()
    rig.reg.register(PID, account="Acc", port=9333, dashboard_started=True)


def test_close_one_busy_lease_calls_nothing(rig, journal):
    _warm_ours(rig)
    with _OtherThreadHolder():
        res = rig.reg.close_one("Acc", entry())
    assert res["ok"] is False and res["code"] == "busy"
    assert res["message"] == ("Not closed: another ACM operation is using this profile. "
                              "Try again when it ends. Nothing was changed.")
    assert rig.stopped == []
    assert rig.reg.session(PID) is not None, "registry untouched"
    assert rig.stops.last_stop(PID) is None, "no stop_log entry"
    assert journal == [{"event": "profile_lease_refused", "code": "busy"}], "no profile id in the journal"


def test_close_one_works_and_releases_the_lease(rig):
    _warm_ours(rig)
    assert rig.reg.close_one("Acc", entry())["code"] == "closed"
    assert profile_lock(PID).acquire(blocking=False)   # свободен после закрытия
    profile_lock(PID).release()


def test_open_one_fresh_start_busy_lease_starts_nothing(rig, journal):
    with _OtherThreadHolder():
        res = rig.reg.open_one("Acc", entry())
    assert res["ok"] is False and res["code"] == "profile_lease"
    assert res["message"] == "Not started: another ACM operation is using the Acc profile. Nothing was started."
    assert rig.started == [] and rig.reg.session(PID) is None
    assert rig.reg._starting.get(PID) is None, "start turn given back"
    assert journal and journal[0]["code"] == "profile_lease"


def test_open_one_fresh_start_releases_lease_after_register(rig):
    res = rig.reg.open_one("Acc", entry())
    assert res["code"] == "started" and rig.reg.session(PID) is not None
    assert profile_lock(PID).acquire(blocking=False)
    profile_lock(PID).release()


def test_open_one_start_failure_releases_lease(rig):
    rig.start_error = RuntimeError("boom")
    assert rig.reg.open_one("Acc", entry())["code"] == "start_failed"
    assert profile_lock(PID).acquire(blocking=False)
    profile_lock(PID).release()


def test_open_one_gone_profile_does_not_restart_cooldown_while_busy(rig):
    """G5: профиль в реестре, процесса нет, замок у другого — паузу не трогаем."""
    rig.warm_up()
    rig.scan_result[PID] = ("none", None)
    with _OtherThreadHolder():
        res = rig.reg.open_one("Acc", entry())
    assert res["code"] == "profile_lease"
    assert rig.reg.session(PID) is not None, "registry untouched"
    assert rig.started == []


def test_open_one_does_not_hold_the_lease_during_the_memory_wait(rig, monkeypatch):
    """Замок берётся после ожидания памяти: читатель не стоит из-за него."""
    seen = []

    def memory(cancelled):
        seen.append(profile_lock(PID).held_by_current_thread())
        return ""

    monkeypatch.setattr(rig.reg, "_wait_for_memory", memory)
    rig.reg.open_one("Acc", entry())
    assert seen == [False]


def test_lease_folder_unreachable_is_a_normal_refusal(rig, monkeypatch):
    @contextmanager
    def denied(*_a, **_k):
        raise PermissionError("denied")
        yield

    monkeypatch.setattr(pl, "machine_file_lock", denied)
    res = rig.reg.open_one("Acc", entry())
    assert res["code"] == "profile_lease"
    assert res["message"] == ("Cannot acquire the profile lease; check access to the user lease directory. "
                              "Nothing was changed.")
    assert rig.started == []


def test_try_lease_is_reentrant_for_the_owner_thread():
    with profile_lock(PID):
        with warm.try_lease(PID, busy_message="x", busy_code="busy") as refusal:
            assert refusal is None
