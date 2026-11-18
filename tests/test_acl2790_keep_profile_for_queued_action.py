# -*- coding: utf-8 -*-
"""ACL-2790: профиль, поднятый чтением, не закрывается, пока на аккаунте стоит действие.

Очередь профилей и MLX подменены; проверяем только решение «закрыть / оставить».
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.analysis_agent import account_spend as sp  # noqa: E402
from src.fb_ads_rpa import browser as rpa_browser  # noqa: E402

PID = "11111111-2222-3333-4444-555555555555"
ACC = "Acc 1"


class Queue:
    def __init__(self):
        self.kept = []
        self.released = []

    def keep(self, slot_id, label="", kind="review"):
        self.kept.append((slot_id, kind))

    def release(self, slot_id):
        self.released.append(slot_id)

    def others_on(self, *a, **k):
        return False

    def grants(self, *a, **k):
        return 0


@pytest.fixture
def world(monkeypatch):
    q = Queue()
    stops = []
    monkeypatch.setattr(sp, "_slot_queue", lambda: q)
    monkeypatch.setattr(sp, "_record", lambda *a, **k: None)
    monkeypatch.setattr(sp, "_profile_key", lambda account, *a, **k: PID)
    monkeypatch.setattr(sp, "may_open_closed_profile", lambda: False)
    monkeypatch.setattr(rpa_browser, "stop_profile_on_request", lambda entry, account: stops.append(account))
    from src.fb_api import stop_log
    monkeypatch.setattr(stop_log, "record_stop", lambda pid: None)
    sp._QUEUED_ACTIONS.clear()
    yield q, stops
    sp._QUEUED_ACTIONS.clear()


def opened():
    return sp._ReadOpen(account=ACC, started=True, slot_id="read:1", profile_id=PID, entry=object(),
                        started_mono=0.0)


def test_queued_action_blocks_the_close(world):
    q, stops = world
    with sp.action_queued(ACC):
        o = opened()
        sp._finish_started(o)
    assert stops == []
    assert q.kept == [("read:1", "convenience")] and q.released == []
    assert o.started is False


def test_queued_action_blocks_the_deferred_close_too(world):
    q, stops = world
    o = opened()
    with sp.action_queued(ACC):
        sp._close_started(o)
    assert stops == [] and q.kept == [("read:1", "convenience")]


def test_idle_profile_is_still_closed(world):
    q, stops = world
    sp._finish_started(opened())
    assert stops == [ACC] and q.kept == []


def test_close_resumes_after_the_action_ends(world):
    _q, stops = world
    with sp.action_queued(ACC):
        assert sp.action_pending(PID)
    assert not sp.action_pending(PID)
    sp._finish_started(opened())
    assert stops == [ACC]


def test_grace_end_keeps_the_window_when_an_action_waits(world, monkeypatch):
    q, stops = world
    o = opened()
    sp._PENDING_CLOSE[PID] = o
    monkeypatch.setattr(sp, "profile_lock", lambda key: __import__("contextlib").nullcontext())
    with sp.action_queued(ACC):
        sp._close_if_still_pending(o)
    assert stops == [] and q.kept == [("read:1", "convenience")]


def test_counter_is_per_profile_and_nests(world):
    with sp.action_queued(ACC), sp.action_queued(ACC):
        assert sp.action_pending(PID)
    assert sp._QUEUED_ACTIONS == {}
    assert not sp.action_pending("")
