"""ACL-2653: a Facebook lock holds only its own profile; removing an account clears its machine stop.

Fixtures only: no Meta call, no browser. The tab address is the whole evidence of a lock.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.analysis_agent import account_refresh as ar
from src.analysis_agent import account_refresh_state as ars
from src.analysis_agent import account_spend
from src.analysis_agent import campaign_inventory as ci
from src.analysis_agent.account_refresh_machine import MachineStore, binding
from src.fb_api import account_blocks as blocks
from tests.test_acl2063_machine_queue import dashboards  # noqa: F401  (fixture)
from tests.test_acl2063_refresh_queue import NOW, _queue, env  # noqa: F401  (fixture)

ROOT = Path(__file__).resolve().parent.parent
KEY = "prof-b93"
LABEL = "KLD_BFL_B93"
CHECKPOINT_URL = "https://www.facebook.com/checkpoint/1501092823525282/?next=https%3A%2F%2Fwww.facebook.com%2F"


@pytest.fixture(autouse=True)
def _holds(tmp_path, monkeypatch):
    monkeypatch.setattr(blocks, "_PATH_OVERRIDE", tmp_path / "holds.json")
    blocks._TIMED_META.clear()
    yield
    blocks._TIMED_META.clear()


def _checkpoint(account: str) -> ci.Snapshot:
    return ci.Snapshot(account=account, state="failed", error="The profile is on a Facebook checkpoint", stop="checkpoint")


def _plant_stop(root, key: str, kind: str = "call_budget", until=None) -> None:
    with MachineStore(root).transaction() as state:
        state["revision"] += 1
        state["stops"][key] = {"kind": kind, "reason": "synthetic", "until": until, "key": key,
                               "generation": state["revision"]}


# ───────── 1. a checkpoint holds one profile ─────────

def test_another_profiles_checkpoint_does_not_block_reads_of_the_others(env):
    env.failures["F94"] = _checkpoint("F94")
    q = _queue(env)
    q.request("F94", "attention")
    q._drain_once()
    assert q.state()["halt"] is None
    # Open status reads and ordinary reads of other accounts go on; none of them needs a bypass.
    assert q.request("B93", "open", force=True) == {"queued": True, "reason": "queued"}
    assert q.request("F92", "schedule", window="08:00") == {"queued": True, "reason": "queued"}
    q._drain_once()
    assert sorted(a for a, _ in env.read) == ["B93", "F92", "F94"]


def test_a_checkpoint_publishes_no_machine_stop(tmp_path):
    machine = MachineStore(tmp_path / "machine")
    key = "a" * 64
    machine.publish(key, _checkpoint("F94"), now=NOW)
    assert machine.halt(now=NOW) is None


def test_genuinely_global_stops_still_halt_everything(env):
    env.failures["A"] = ci.Snapshot(account="A", state="failed", error="call budget", error_code="call_budget")
    q = _queue(env)
    q.request("A", "attention")
    q._drain_once()
    assert q.state()["halt"] is not None and q.state()["halt"]["until"] is None
    assert q.request("B", "attention") == {"queued": True, "reason": "halted"}


# ───────── 2. removal / orphan stops ─────────

def test_removing_an_account_clears_its_machine_stop_and_hold(tmp_path, monkeypatch):
    monkeypatch.setattr(ars, "machine_root", lambda: tmp_path / "machine", raising=False)
    key = "b" * 64
    _plant_stop(tmp_path / "machine", key)
    other = "c" * 64
    _plant_stop(tmp_path / "machine", other, kind="rate_limited", until=NOW + 600)
    blocks.note_person_lock("prof-f94", "KLD_BFL_F94", "a background read")
    assert blocks.is_held("prof-f94")
    ar.forget_accounts({"KLD_BFL_F94": (key, "prof-f94")}, remaining=[])
    assert not blocks.is_held("prof-f94")
    stops = MachineStore(tmp_path / "machine").halt(now=NOW)
    assert stops and stops["key"] == other          # only the removed account's stop went


def test_a_shared_profile_keeps_its_hold_while_another_label_uses_it(tmp_path, monkeypatch):
    monkeypatch.setattr(ars, "machine_root", lambda: tmp_path / "machine", raising=False)
    monkeypatch.setattr(account_spend, "_profile_key", lambda label, *a, **k: "prof-shared")
    blocks.note_person_lock("prof-shared", "ONE", "Retry")
    ar.forget_accounts({"ONE": ("", "prof-shared")}, remaining=["TWO"])
    assert blocks.is_held("prof-shared")


def test_an_orphan_machine_stop_is_dropped_and_a_known_one_stays(dashboards):
    select, new, reads = dashboards
    q = new("one")
    root = q._machine.root
    known = binding("A")
    orphan = "d" * 64
    _plant_stop(root, orphan)
    # The stop of a removed account (no map row has its key) never lifts by itself: the queue drops it.
    assert q.state()["halt"] is None
    assert orphan not in json.loads((root / "state.json").read_text())["stops"]
    _plant_stop(root, known)
    assert q.state()["halt"]["key"] == known                  # a known account's stop is untouched
    assert known in json.loads((root / "state.json").read_text())["stops"]


def test_old_machine_wide_checkpoint_stop_of_a_known_account_becomes_its_hold(dashboards, monkeypatch):
    select, new, reads = dashboards
    q = new("one")
    monkeypatch.setattr(account_spend, "_profile_key", lambda label, *a, **k: KEY)
    _plant_stop(q._machine.root, binding("A"), kind="checkpoint")
    assert q.state()["halt"] is None
    assert json.loads((q._machine.root / "state.json").read_text())["stops"] == {}
    assert blocks.is_held(KEY)                                # the profile itself still waits for a person


def test_an_empty_account_map_never_drops_stops(dashboards):
    select, new, reads = dashboards
    q = new("one")
    known = binding("A")
    _plant_stop(q._machine.root, known)
    q._accounts = lambda: []
    assert q.state()["halt"]["key"] == known
