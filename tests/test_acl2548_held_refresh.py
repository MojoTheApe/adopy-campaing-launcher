"""ACL-2548 (part B of ACL-2091): the background refresh queue does not touch a held profile.

Offline: the reader is a fake, nothing reaches Meta or a browser.
"""
from __future__ import annotations

import pytest

from src.analysis_agent import account_refresh as ar
from src.analysis_agent import campaign_inventory as ci
from tests.test_acl2063_refresh_queue import NOW, _failed, _iso, _queue, env  # noqa: F401  (fixture)

HELD = "HELD"


@pytest.fixture
def held(monkeypatch):
    held_accounts = {HELD}
    monkeypatch.setattr(ar, "_profile_held", lambda account: account in held_accounts)
    attempts = []
    monkeypatch.setattr(ci, "_put_result", lambda done: attempts.append(done.account), raising=False)
    return attempts


@pytest.mark.parametrize("trigger, force", [("schedule", False), ("attention", False), ("open", False),
                                            ("action", True), ("attention", True), ("schedule", True)])
def test_no_automatic_or_after_action_read_reaches_a_held_profile(env, held, trigger, force):
    env.stops = {"X": ("page_unresponsive", _iso(NOW - 30))}   # a stop that does not halt the queue
    before = dict(env.stops)
    q = _queue(env)
    result = q.request(HELD, trigger, force=force)
    q.request("OTHER", "attention")
    q._drain_once()
    assert result["queued"] is False
    assert [a for a, _ in env.read] == ["OTHER"]                  # no read call for the held profile, others read
    assert held == [] and HELD not in env.attempts                # no attempt row written
    assert env.stops == before and ci.latest_stops() == before    # latest_stops identical


def test_a_hold_that_appears_while_a_request_waits_drops_it_without_a_read(env, held, monkeypatch):
    held.clear()
    monkeypatch.setattr(ar, "_profile_held", lambda account: False)
    q = _queue(env)
    assert q.request(HELD, "action", force=True)["queued"] is True
    q.request("OTHER", "attention")
    monkeypatch.setattr(ar, "_profile_held", lambda account: account == HELD)
    q._drain_once()
    assert [a for a, _ in env.read] == ["OTHER"]
    assert HELD not in env.attempts


def test_skipping_a_held_profile_leaves_a_persisted_global_halt_in_place(env, held):
    env.stops = {HELD: ("checkpoint", _iso(NOW - 60))}
    q = _queue(env)
    halt = q._persisted_halt()
    assert halt and halt["kind"] == "checkpoint"
    q.request(HELD, "attention")
    q.request(HELD, "action", force=True)
    q._drain_once()
    assert env.read == [] and env.stops == {HELD: ("checkpoint", _iso(NOW - 60))}
    assert q._persisted_halt() == halt


def test_a_sweep_skips_a_held_profile_and_still_completes(env, held):
    q = _queue(env)
    q.begin_sweep("08:00", 2, accounts=[HELD, "OTHER"])
    q.request(HELD, "schedule", window="08:00")
    q.request("OTHER", "schedule", window="08:00")
    q._drain_once()
    assert [a for a, _ in env.read] == ["OTHER"]


def test_a_persons_retry_still_reaches_a_held_profile(env, held):
    q = _queue(env)
    assert q.request(HELD, "retry", force=True) == {"queued": True, "reason": "queued"}
    q._drain_once()
    assert env.read == [(HELD, "retry")]


@pytest.mark.parametrize("error", [
    "Facebook security check on B97, found during a background read. Solve the checkpoint in the open profile window, then press Retry.",
    "The B97 profile is on a Facebook checkpoint",
])
def test_account_blocked_code_is_not_a_global_stop_even_if_the_text_says_checkpoint(error):
    snap = _failed("B97", error, code="account_blocked")
    assert ar._global_stop(snap) is None and ar.classify(snap) == "account"
    # without the code the text fallback still works as before
    assert ar._global_stop(_failed("B97", error))[0] == ar._GLOBAL_STOPS["checkpoint"][0]


def test_a_refused_read_does_not_raise_a_global_halt(env, held):
    env.failures["OTHER"] = _failed("OTHER", "Facebook security check on OTHER. Solve the checkpoint, then press Retry.",
                                    code="account_blocked")
    q = _queue(env)
    q.request("OTHER", "attention")
    q._drain_once()
    assert q.state().get("halt") in (None, {}) or not q._halt
    assert q._halt is None
