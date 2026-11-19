"""ACL-2653: a Facebook lock holds only its own profile; removing an account clears its machine stop.

Fixtures only: no Meta call, no browser. The tab address is the whole evidence of a lock.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
from datetime import date, datetime
from pathlib import Path

import pytest

from src.analysis_agent import account_refresh as ar
from src.analysis_agent import account_refresh_state as ars
from src.analysis_agent import account_spend
from src.analysis_agent import campaign_inventory as ci
from src.analysis_agent.account_refresh_machine import MachineStore, binding
from src.fb_api import account_blocks as blocks
from src.web import account_state as astate
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


def test_the_automatic_prune_drops_only_checkpoint_stops_never_other_kinds(dashboards):
    """state.json is shared by all dashboards: a key unknown to THIS config's map is not "removed"."""
    select, new, reads = dashboards
    q = new("one", ["A", "B"])
    select("one")
    root = q._machine.root
    foreign = "d" * 64                                         # a stop of another dashboard's account
    _plant_stop(root, foreign, kind="policy_block", until=NOW + 1800)
    other_cp = "e" * 64
    _plant_stop(root, other_cp, kind="checkpoint")             # a checkpoint stop of any key goes
    two = new("two", ["A"])                                    # a second config that does not map B
    st = two.state()
    stops = json.loads((root / "state.json").read_text())["stops"]
    assert foreign in stops and other_cp not in stops
    assert st["halt"] is not None and st["halt"]["key"] == foreign


def test_a_known_non_checkpoint_stop_stays(dashboards):
    select, new, reads = dashboards
    q = new("one")
    known = binding("A")
    _plant_stop(q._machine.root, known)
    assert q.state()["halt"]["key"] == known
    assert known in json.loads((q._machine.root / "state.json").read_text())["stops"]


def test_removal_keeps_the_stop_and_hold_of_a_binding_a_remaining_alias_shares(tmp_path, monkeypatch):
    monkeypatch.setattr(ars, "machine_root", lambda: tmp_path / "machine", raising=False)
    monkeypatch.setattr(account_spend, "_profile_key", lambda label, *a, **k: "prof-shared")
    monkeypatch.setattr(ar, "binding", lambda label: "f" * 64)
    _plant_stop(tmp_path / "machine", "f" * 64)
    blocks.note_person_lock("prof-shared", "ONE", "Retry")
    ar.forget_accounts({"ONE": ("f" * 64, "prof-shared")}, remaining=["ALIAS"])
    assert blocks.is_held("prof-shared")
    assert MachineStore(tmp_path / "machine").halt(now=NOW)["key"] == "f" * 64


def test_a_lease_timeout_still_clears_the_hold(tmp_path, monkeypatch):
    monkeypatch.setattr(ars, "machine_root", lambda: tmp_path / "machine", raising=False)
    blocks.note_person_lock("prof-x", "X", "Retry")

    class Busy:
        def lease(self, name):
            raise TimeoutError("read in progress")

    monkeypatch.setattr(ar, "MachineStore", lambda *a, **k: Busy())
    ar.forget_accounts({"X": ("b" * 64, "prof-x")}, remaining=[])
    assert not blocks.is_held("prof-x")


def test_the_remove_route_runs_the_cleanup_off_the_event_loop():
    src = (ROOT / "src/web/app.py").read_text(encoding="utf-8")
    assert "await run_in_threadpool(_refresh.forget_accounts" in src


def test_a_hold_keeps_automatic_reads_off_the_profile_within_one_config(dashboards, monkeypatch):
    select, new, reads = dashboards
    first = new("one", ["A", "B"])
    select("one")
    snap = ci.Snapshot(account="A", state="failed", stop="checkpoint", error="Synthetic checkpoint")
    ci.put_result(snap)
    first.record_result(snap, trigger="action")
    assert blocks.is_held(account_spend._profile_key("A"))
    assert first.request("A", "attention") == {"queued": False, "reason": "profile_held"}
    assert first.request("A", "schedule", window="08:00") == {"queued": False, "reason": "profile_held"}
    assert first.request("B", "attention")["queued"] is True
    first._drain_once()
    assert [a for _, a in reads] == ["B"]


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


# ───────── 3. the Facebook lock from the tab ─────────

def test_a_checkpoint_tab_url_sets_a_facebook_lock_hold_without_any_graph_call(monkeypatch):
    import socket

    def no_network(*a, **k):
        raise AssertionError("the lock is read from the tab address; nothing may be called")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    assert blocks.note_tab_url(KEY, LABEL, CHECKPOINT_URL, "Open profile") is True
    held = blocks.hold_of(KEY)
    assert held["kind"] == "checkpoint" and held["code"] is None and held["action"] == "Open profile"
    assert blocks.is_person_lock(held)
    # Another host and a normal Ads Manager tab are not locks.
    assert blocks.note_tab_url("k2", "X", "https://www.facebook.com/adsmanager/manage/campaigns?act=1") is False
    assert blocks.note_tab_url("k3", "X", "https://example.com/checkpoint/1") is False
    assert not blocks.is_held("k2") and not blocks.is_held("k3")


def test_the_lock_keeps_its_first_date_and_automatic_work_never_lifts_it(monkeypatch):
    blocks.note_tab_url(KEY, LABEL, CHECKPOINT_URL, "Open profile")
    first = blocks.hold_of(KEY)["at"]
    blocks.note_tab_url(KEY, LABEL, CHECKPOINT_URL, "a background read")
    assert blocks.hold_of(KEY)["at"] == first
    assert blocks.lift_auth_hold(KEY, "publish") is False       # only a code-31 hold is lifted by an edit
    monkeypatch.setattr(account_spend, "_profile_key", lambda label, *a, **k: KEY)
    assert ar._held_blocks(LABEL, "schedule:2026-11-16/08:00") and ar._held_blocks(LABEL, "attention")
    assert not ar._held_blocks(LABEL, "retry")                  # a person's Retry is the one way through


def test_open_profile_on_a_checkpoint_tab_records_the_lock_and_still_reads_the_status(monkeypatch):
    from src.web import warm_api

    monkeypatch.setattr(warm_api, "_profile_id", lambda label: "pid")
    monkeypatch.setattr(account_spend, "_profile_key", lambda label, *a, **k: KEY)
    assert warm_api.note_tab_lock(LABEL, urls=["about:blank", CHECKPOINT_URL]) is True
    assert blocks.is_held(KEY)
    queued = []
    monkeypatch.setattr(warm_api, "request_status_read", lambda label: queued.append(label) or "queued")
    monkeypatch.setattr(warm_api, "note_tab_lock", lambda label, **k: True)
    seen = {}
    monkeypatch.setattr(warm_api, "_set_result", lambda job, label, result: seen.update(result))
    warm_api._on_open_result("job", LABEL, {"ok": True})
    # The owner's rule: Open on an account checks it, even on a lock. Meta's refusal is then shown with its time.
    assert queued == [LABEL] and seen["status_read"] == "queued" and seen["locked"] is True


def test_the_row_is_red_facebook_locked_since_the_date(monkeypatch):
    blocks.note_person_lock(KEY, LABEL, "a background read")
    hold = blocks.view({LABEL: KEY})[LABEL]
    assert hold["person_lock"] is True
    state = astate.account_state({"account": LABEL, "status": {}, "has_data": False, "hold": hold})
    since = datetime.fromtimestamp(hold["at"]).strftime("%d.%m")
    assert state["key"] == "issue"
    assert state["status_line"].startswith(f"Facebook locked · since {since}")
    assert "locked" in state["status_tip"]
    # An ad account restriction keeps its own words.
    restricted = astate.account_state({"account": LABEL, "has_data": False, "hold": hold,
                                       "status": {"status": "restricted", "status_text": "Restricted", "at": "2026-11-15"}})
    assert restricted["key"] == "issue" and "locked" not in restricted["status_line"]


# ───────── 4. no green Active for a stale clear; no false "Status updated" ─────────

def test_a_stale_clear_is_not_shown_as_active_for_a_locked_profile():
    blocks.note_person_lock(KEY, LABEL, "a background read")
    hold = blocks.view({LABEL: KEY})[LABEL]
    today = date(2026, 11, 16)
    row = {"account": LABEL, "has_data": True, "spend": 120.0, "hold": hold,
           "status": {"status": "clear", "at": "2026-11-14 00:13", "source": "api"}}
    assert astate.account_state({**row, "hold": None}, today)["key"] == "active"      # the old behaviour, as the control
    locked = astate.account_state(row, today)
    assert locked["key"] == "issue" and locked["label"] != "Active"
    assert locked["status_line"].startswith("Facebook locked")


NODE = shutil.which("node")

_NODE_SCRIPT = r"""
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const sb = { console, Promise, JSON, Object, String, Array, setTimeout, clearTimeout, encodeURIComponent,
  document: { readyState: 'loading', addEventListener() {}, createElement: () => ({ style: {}, appendChild() {} }),
              head: { appendChild() {} }, querySelectorAll: () => [], getElementById: () => null },
  window: {}, fetch: () => Promise.resolve({}) };
vm.createContext(sb);
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), sb);
const note = sb.window.aclWarmHome.statusNote;
const same = note('2026-11-14 00:13|', '2026-11-14 00:13|', {holds: {}, rows: {}}, 'B93');
assert.notStrictEqual(same.text, 'Status updated.');
assert.ok(/not read/i.test(same.text));
const failed = note('x', 'x', {holds: {}, rows: {B93: {state: 'failed', error: 'Meta said no'}}}, 'B93');
assert.ok(failed.bad && /Meta said no/.test(failed.text));
const moved = note('2026-11-14 00:13|', '2026-11-16 10:02|', {holds: {}, rows: {}}, 'B93');
assert.strictEqual(moved.text, 'Status updated.'); assert.ok(!moved.bad);
const lock = note('a', 'b', {locks: {B93: {person_lock: true, next: 'Solve it.', reason: 'r', seen: 1789000000}}, rows: {}}, 'B93');
assert.ok(lock.bad && /Facebook locked/.test(lock.text));
console.log('ok');
"""


@pytest.mark.skipif(not NODE, reason="node is not installed")
def test_status_updated_is_only_said_when_the_read_time_moved():
    out = subprocess.run([NODE, "-e", _NODE_SCRIPT, str(ROOT / "src/web/static/warm_sessions.js")],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0 and "ok" in out.stdout, out.stderr + out.stdout


# ───────── 5. a stuck read ends as failed ─────────

def test_a_read_stuck_in_updating_ends_as_failed_with_retry_text(env):
    q = _queue(env)
    q._rows["A"] = {"list_at": "2026-11-14 00:13:00", "state": "updating", "error": "", "since": NOW - ar.STUCK_UPDATING_S - 5}
    q._reading = ["A"]
    assert ar.STUCK_UPDATING_S == 1200.0
    state = q.state()
    assert state["rows"]["A"]["state"] == "failed" and state["rows"]["A"]["error"] == ar.STUCK_TEXT
    assert state["rows"]["A"]["list_at"] == "2026-11-14 00:13:00"      # the last good read time is kept
    assert state["running"] is None


def test_a_recent_or_unstamped_updating_row_is_left_alone_then_stamped(env):
    q = _queue(env)
    q._rows["A"] = {"state": "updating", "error": "", "since": NOW - 30}
    q._rows["B"] = {"state": "updating", "error": ""}                  # saved by an older build
    q._reading = ["B"]
    state = q.state()
    assert state["rows"]["A"]["state"] == "updating"
    assert state["rows"]["B"]["state"] == "updating" and state["rows"]["B"]["since"] == NOW
    env.clock.t = NOW + ar.STUCK_UPDATING_S + 1
    rows = q.state()["rows"]
    assert rows["B"]["state"] == "failed" and rows["A"]["state"] == "updating"   # only the running read expires


def test_a_queued_or_paused_row_is_never_expired_and_keeps_its_queue_entry(env):
    env.failures["A"] = ci.Snapshot(account="A", state="failed", error="call budget", error_code="call_budget")
    q = _queue(env)
    q.request("A", "attention")
    q._drain_once()
    q.request("B", "attention")
    q._rows["B"] = {"list_at": "", "state": "updating", "error": "", "since": NOW}
    env.clock.t = NOW + 3 * ar.STUCK_UPDATING_S
    state = q.state()
    assert state["rows"]["B"]["state"] == "updating"
    assert [item["account"] for item in state["queued"]] == ["B"]


def test_an_executing_read_stamps_its_start(env):
    q = _queue(env)
    q.request("A", "attention")
    seen = {}
    real = env.reader

    def reader(account, trigger):
        seen.update(q._rows[account])
        return real(account, trigger)

    q._reader = reader
    q._drain_once()
    assert seen["state"] == "updating" and seen["since"] == NOW


# ───────── 6. Open reads the account's status now ─────────

def test_open_reads_status_now_while_another_profile_is_checkpointed_and_the_queue_has_work(env):
    env.failures["F94"] = _checkpoint("F94")
    q = _queue(env)
    q.request("F94", "attention")
    q._drain_once()
    q.request("F92", "attention")                     # queue has work
    q.request("Doc280", "schedule", window="08:00")
    assert q.request("B93", "open", force=True) == {"queued": True, "reason": "queued"}
    q._drain_once()
    reads = [a for a, _ in env.read]
    assert reads[1] == "B93" and set(reads) == {"F94", "B93", "F92", "Doc280"}   # Open goes ahead of the waiting reads


def test_open_reads_even_through_a_hold_and_a_global_halt(env, monkeypatch):
    monkeypatch.setattr(ar, "_profile_held", lambda account: account == "B93")
    env.failures["A"] = ci.Snapshot(account="A", state="failed", error="call budget", error_code="call_budget")
    q = _queue(env)
    q.request("A", "attention")
    q._drain_once()
    assert q.state()["halt"] is not None
    assert q.request("B93", "attention") == {"queued": False, "reason": "profile_held"}   # background work still stops
    assert q.request("B93", "open", force=True) == {"queued": True, "reason": "queued"}
    q._drain_once()
    assert ("B93", "open") in env.read


def test_open_is_a_persons_action_that_may_probe_a_held_profile():
    with blocks.problem_action(blocks.action_for_trigger("open")):
        assert blocks.buyer_started()
        assert blocks.current_action() == "Open profile"
    with blocks.problem_action(blocks.action_for_trigger("attention")):
        assert not blocks.buyer_started()


def test_open_on_a_checkpointed_profile_records_the_refusal_and_its_read_time(env, monkeypatch):
    monkeypatch.setattr(account_spend, "_profile_key", lambda label, *a, **k: KEY)
    blocks.note_person_lock(KEY, LABEL, "a background read")
    first = blocks.hold_of(KEY)["at"]
    later = first + 7200
    monkeypatch.setattr(blocks.time, "time", lambda: later)
    env.failures[LABEL] = _checkpoint(LABEL)
    q = _queue(env)
    assert q.request(LABEL, "open", force=True)["queued"] is True
    q._drain_once()
    assert q.state()["halt"] is None and q.state()["rows"][LABEL]["state"] == "failed"
    hold = blocks.view({LABEL: KEY})[LABEL]
    assert hold["at"] == first and hold["seen"] == later          # since stays, the read time moves
    row = astate.account_state({"account": LABEL, "status": {}, "has_data": False, "hold": hold})
    assert row["key"] == "issue"
    assert row["status_line"].startswith("Facebook locked · since ") and " · read " in row["status_line"]
    assert datetime.fromtimestamp(later).strftime("%d.%m %H:%M") in row["status_line"]


# ───────── 7. review fixes ─────────

def test_a_lock_found_again_keeps_its_first_seen_date():
    from src.fb_api.graph import Checkpoint

    blocks.note_stop(KEY, LABEL, Checkpoint("c"), "a background read")
    first = blocks.hold_of(KEY)["at"]
    time.sleep(0.01)
    blocks.note_stop(KEY, LABEL, Checkpoint("c"), "Open profile")
    again = blocks.hold_of(KEY)
    assert again["at"] == first and again["seen"] > first and again["action"] == "Open profile"


def test_the_checkpoint_path_match_is_a_path_segment_not_a_substring():
    from src.fb_api.graph import classify_stop_url

    assert classify_stop_url("/checkpoint/1501092823525282/") == "checkpoint"
    assert classify_stop_url("/checkpoint") == "checkpoint"
    assert classify_stop_url("/adsmanager/not-a-checkpoint-page") == ""
    assert classify_stop_url("/groups/checkpoint-fans/") == ""


def test_a_persons_open_is_read_past_an_untimed_stop_end_to_end(tmp_path):
    from src.analysis_agent import campaign_inventory

    src = (ROOT / "src/analysis_agent/campaign_inventory.py").read_text(encoding="utf-8")
    assert '{"action", "retry", "open"}' in src
    machine = MachineStore(tmp_path / "machine")
    key = "a" * 64
    machine.publish(key, ci.Snapshot(account="A", state="failed", error="call budget", error_code="call_budget"), now=NOW)
    assert machine.halt(now=NOW)["until"] is None
    assert campaign_inventory is not None


def test_a_timed_stop_that_holds_back_a_persons_open_says_why_with_the_time(env):
    env.failures["A"] = ci.Snapshot(account="A", state="failed", error="Meta rate limit", stop="rate_limited")
    q = _queue(env)
    q.request("A", "attention")
    q._drain_once()
    halt = q.state()["halt"]
    assert halt and halt["until"] is not None
    paused = ci.Snapshot(account="B", state="failed", error="x")
    paused._machine_paused = {**halt, "generation": 1}
    with q._state_guard():
        q._settle("B", "open", paused, False)
    row = q.state()["rows"]["B"]
    assert row["state"] == "failed" and "Until " in row["error"] and halt["reason"] in row["error"]


# ───────── ACL-2841: login-page hold and Meta's own text ─────────

LOGIN_URL = "https://www.facebook.com/login/?next=https%3A%2F%2Fwww.facebook.com%2F"
RECOVER_URL = "https://www.facebook.com/recover/initiate"


@pytest.mark.parametrize("url", [LOGIN_URL, RECOVER_URL])
def test_a_login_or_recover_tab_records_a_hold_shown_red_as_logged_out(url, monkeypatch):
    import socket

    def no_network(*a, **k):
        raise AssertionError("nothing may be called")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    assert blocks.note_tab_url(KEY, LABEL, url, "Open profile") is True
    held = blocks.hold_of(KEY)
    assert blocks.is_person_lock(held) and held["page"] == "login"
    hold = blocks.view({LABEL: KEY})[LABEL]
    state = astate.account_state({"account": LABEL, "status": {}, "has_data": False, "hold": hold})
    assert state["key"] == "issue"
    assert state["status_line"].startswith("Logged out of Facebook")
    assert "logged out of Facebook" in state["status_tip"]


def test_the_row_shows_metas_title_and_falls_back_without_one():
    assert blocks.note_tab_url(KEY, LABEL, CHECKPOINT_URL, "Open profile", "  Your account has been\n locked  ") is True
    hold = blocks.view({LABEL: KEY})[LABEL]
    assert hold["title"] == "Your account has been locked"
    state = astate.account_state({"account": LABEL, "status": {}, "has_data": False, "hold": hold})
    assert 'Facebook says: "Your account has been locked".' in state["status_tip"]
    # No readable title: the current wording.
    blocks.note_tab_url("k-nt", "NT", CHECKPOINT_URL, "Open profile", "")
    plain = astate.account_state({"account": "NT", "status": {}, "has_data": False,
                                  "hold": blocks.view({"NT": "k-nt"})["NT"]})
    assert "Facebook says" not in plain["status_tip"] and "is locked" in plain["status_tip"]
    blocks.note_tab_url("k-lg", "LG", LOGIN_URL, "Open profile", "")
    login = astate.account_state({"account": "LG", "status": {}, "has_data": False,
                                  "hold": blocks.view({"LG": "k-lg"})["LG"]})
    assert "logged out of Facebook" in login["status_tip"]


def test_a_later_sight_updates_the_title_and_keeps_the_first_date():
    blocks.note_tab_url(KEY, LABEL, LOGIN_URL, "Open profile")
    first = blocks.hold_of(KEY)["at"]
    blocks.note_tab_url(KEY, LABEL, LOGIN_URL, "Open profile", "Log in to Facebook")
    held = blocks.hold_of(KEY)
    assert held["at"] == first and held["title"] == "Log in to Facebook" and held["page"] == "login"


def test_open_profile_passes_the_tab_title_and_local_targets_read_it(monkeypatch):
    from src.fb_api import warm
    from src.web import warm_api

    monkeypatch.setattr(warm_api, "_profile_id", lambda label: "pid")
    monkeypatch.setattr(account_spend, "_profile_key", lambda label, *a, **k: KEY)
    assert warm_api.note_tab_lock(LABEL, urls=[("about:blank", ""), (LOGIN_URL, "Log in to Facebook")]) is True
    assert blocks.hold_of(KEY)["title"] == "Log in to Facebook"

    class Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self):
            return json.dumps([{"type": "page", "url": LOGIN_URL, "title": "Log in to Facebook"},
                               {"type": "service_worker", "url": "x", "title": "y"}]).encode()

    import urllib.request
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", lambda self, req, timeout=None: Resp())
    assert warm.local_page_targets(9222) == [(LOGIN_URL, "Log in to Facebook")]
    assert warm.local_page_urls(9222) == [LOGIN_URL]
