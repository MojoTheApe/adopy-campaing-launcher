"""ACL-1853: чтение ждёт занятый профиль дольше общего WAIT_SECONDS.

17.10 на TEST9095 Check plan по B90 стоял за чтением того же профиля и через
120 с падал с «This profile is busy in another ACM operation».
"""
from __future__ import annotations

import threading

import pytest

from src.fb_ads_rpa import operations as ops
from src.fb_ads_rpa import profile_lease as pl

PID = "550e8400-e29b-41d4-a716-446655440000"


def test_a_read_waits_for_a_busy_profile_longer_than_the_old_limit(monkeypatch, tmp_path):
    monkeypatch.setattr(pl, "_lease_root", lambda: tmp_path)
    monkeypatch.setattr(pl, "WAIT_SECONDS", 0.2)   # прежний предел, сжатый для теста
    held, release = threading.Event(), threading.Event()

    def holder():
        with pl.profile_lock(PID):
            held.set()
            release.wait(5)

    t = threading.Thread(target=holder)
    t.start()
    assert held.wait(5)
    threading.Timer(0.6, release.set).start()   # дольше прежнего предела

    @ops.serialized_browser(key=lambda account: PID, reading=True)
    def read(account):
        return "read " + account

    assert read("B90") == "read B90"
    t.join(5)


def test_a_read_still_gives_up_after_its_own_limit(monkeypatch, tmp_path):
    monkeypatch.setattr(pl, "_lease_root", lambda: tmp_path)
    monkeypatch.setattr(ops, "READ_WAIT_SECONDS", 0.2)
    held, release = threading.Event(), threading.Event()

    def holder():
        with pl.profile_lock(PID):
            held.set()
            release.wait(5)

    t = threading.Thread(target=holder)
    t.start()
    assert held.wait(5)

    @ops.serialized_browser(key=lambda account: PID, reading=True)
    def read(account):
        return "read"

    with pytest.raises(pl.ProfileLeaseError):
        read("B90")
    release.set()
    t.join(5)
