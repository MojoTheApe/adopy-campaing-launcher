"""ACL-2063: очередь обновления аккаунтов — свежесть, приоритеты, остановки."""
from __future__ import annotations

import inspect
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.analysis_agent import account_refresh as ar  # noqa: E402
from src.analysis_agent import account_spend_api  # noqa: E402
from src.analysis_agent import campaign_inventory as ci  # noqa: E402

NOW = 1_800_000_000.0


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


class Clock:
    def __init__(self, t: float = NOW):
        self.t = t

    def __call__(self) -> float:
        return self.t


class Env:
    """Подмена хранилища снимков и читалки: что прочитали и в каком порядке."""

    def __init__(self, clock: Clock):
        self.clock = clock
        self.snaps: dict[str, ci.Snapshot] = {}
        self.failures: dict[str, ci.Snapshot] = {}
        self.read: list[tuple[str, str]] = []

    def reader(self, account: str, trigger: str) -> ci.Snapshot:
        self.read.append((account, trigger))
        if account in self.failures:
            return self.failures[account]
        snap = ci.Snapshot(account=account, state="ok", finished_at="2026-10-21 10:00:00",
                           finished_at_utc=_iso(self.clock()))
        self.snaps[account] = snap
        return snap


@pytest.fixture
def env(monkeypatch):
    clock = Clock()
    e = Env(clock)
    monkeypatch.setattr(ci, "get_snapshot", lambda account: e.snaps.get(account))
    e.telemetry = []
    monkeypatch.setattr(account_spend_api, "_telemetry_skip",
                        lambda *a, **k: e.telemetry.append((a, k)))
    return e


def _queue(env: Env) -> ar.RefreshQueue:
    return ar.RefreshQueue(reader=env.reader, clock=env.clock)


def _failed(account: str, error: str, code: str = "") -> ci.Snapshot:
    return ci.Snapshot(account=account, state="failed", error=error, error_code=code)


def test_fresh_account_skipped(env):
    env.snaps["A"] = ci.Snapshot(account="A", state="ok", finished_at_utc=_iso(NOW - 600))
    q = _queue(env)
    assert q.request("A", "attention") == {"queued": False, "reason": "fresh"}
    assert q.state()["queued"] == []
    assert env.telemetry == [(("A", "attention"), {})]


def test_old_or_failed_snapshot_is_not_fresh(env):
    env.snaps["A"] = ci.Snapshot(account="A", state="ok", finished_at_utc=_iso(NOW - ar.FRESH_S - 1))
    env.snaps["B"] = ci.Snapshot(account="B", state="failed", finished_at_utc=_iso(NOW - 10))
    assert not ar.is_fresh("A", now=NOW)
    assert not ar.is_fresh("B", now=NOW)
    assert not ar.is_fresh("nobody", now=NOW)


def test_priority_order(env):
    q = _queue(env)
    q.request("B", "schedule")
    q.request("C", "attention")
    q.request("D", "action")
    q._drain_once()
    assert [a for a, _ in env.read] == ["D", "C", "B"]


def test_duplicate_merged_and_upgraded(env):
    q = _queue(env)
    q.request("A", "schedule")
    assert q.request("A", "action") == {"queued": True, "reason": "queued"}
    assert q.state()["queued"] == [{"account": "A", "trigger": "action"}]
    q._drain_once()
    assert env.read == [("A", "action")]


def test_account_failure_continues(env):
    env.failures["A"] = _failed("A", "Proxy error ERR_INVALID_AUTH_CREDENTIALS")
    q = _queue(env)
    q.request("A", "attention")
    q.request("B", "schedule")
    q._drain_once()
    assert [a for a, _ in env.read] == ["A", "B"]
    st = q.state()
    assert st["rows"]["A"]["state"] == "failed"
    assert "ERR_INVALID_AUTH_CREDENTIALS" in st["rows"]["A"]["error"]
    assert st["rows"]["B"]["state"] == "ok"
    assert st["halt"] is None


def test_global_failure_halts(env):
    env.failures["A"] = _failed("A", "HTTPSConnectionPool(host='launcher.mlx.yt'): [WinError 10061] refused")
    q = _queue(env)
    q.request("A", "attention")
    q.request("B", "schedule")
    q._drain_once()
    assert [a for a, _ in env.read] == ["A"]
    halt = q.state()["halt"]
    assert "Multilogin" in halt["reason"]
    assert q.state()["queued"] == [{"account": "B", "trigger": "schedule"}]
    assert q.request("C", "attention") == {"queued": True, "reason": "halted"}


def test_needs_api_backend_halts(env):
    env.failures["A"] = _failed("A", "API backend is off", ci.NEEDS_API_CODE)
    q = _queue(env)
    q.request("A", "attention")
    q.request("B", "schedule")
    q._drain_once()
    assert [a for a, _ in env.read] == ["A"]
    assert q.state()["halt"] and "backend" in q.state()["halt"]["reason"].lower()
    assert ar.classify(env.failures["A"]) == "global"


def test_retry_clears_halt(env):
    env.failures["A"] = _failed("A", "[WinError 10061] launcher.mlx.yt")
    q = _queue(env)
    q.request("A", "attention")
    q._drain_once()
    assert q.state()["halt"]
    del env.failures["A"]
    assert q.request("A", "retry", force=True) == {"queued": True, "reason": "queued"}
    q._drain_once()
    assert q.state()["halt"] is None
    assert env.read[-1] == ("A", "retry")
    assert q.state()["rows"]["A"]["state"] == "ok"


def test_halt_lifts_after_cooldown(env):
    env.failures["A"] = _failed("A", "Meta asked to slow down for A; waiting before the next read (about 5 min). Nothing was called.")
    q = _queue(env)
    q.request("A", "attention")
    q.request("B", "schedule")
    q._drain_once()
    halt = q.state()["halt"]
    assert halt and halt["until"] is not None
    assert env.read == [("A", "attention")]
    env.clock.t = halt["until"] + 1
    q._drain_once()
    assert q.state()["halt"] is None
    assert env.read[-1] == ("B", "schedule")


def test_halt_without_cooldown_does_not_lift_by_time(env):
    env.failures["A"] = _failed("A", "[WinError 10061] launcher.mlx.yt")
    q = _queue(env)
    q.request("A", "attention")
    q._drain_once()
    env.clock.t += 10 * 3600
    assert q.state()["halt"] is not None


def test_classify_variants():
    assert ar.classify(ci.Snapshot(account="A", state="ok")) == "ok"
    assert ar.classify(_failed("A", "x", "call_budget")) == "global"
    assert ar.classify(_failed("A", "The A profile is on a Facebook checkpoint")) == "global"
    assert ar.classify(_failed("A", "Meta rate limit reached; the read was stopped.")) == "global"
    assert ar.classify(_failed("A", "x", "mlx_session_lost")) == "global"
    assert ar.classify(_failed("A", "no access to the ad account")) == "account"
    assert ar.classify(_failed("A", "logged out", "logged_out")) == "account"


def test_period_has_no_entry_point():
    for name, fn in inspect.getmembers(ar, inspect.isfunction):
        assert "date_from" not in inspect.signature(fn).parameters, name
    for name, fn in inspect.getmembers(ar.RefreshQueue, inspect.isfunction):
        assert "date_from" not in inspect.signature(fn).parameters, name


# ───────────────────────── ревью: остановки, паузы, поток ─────────────────────────

import threading  # noqa: E402

from src.fb_api.graph import Checkpoint  # noqa: E402


class StopClient:
    """Кампании читаются, на ad set Meta отвечает остановкой."""

    def __init__(self, exc):
        self.exc = exc
        self.calls = 0
        self.stopped = None
        self.truncated: list[str] = []

    def account(self, act):
        return {"id": act, "account_id": act[4:], "currency": "USD", "account_status": 1}

    def get_all(self, path, params=None, max_pages=5):
        if path.endswith("/adsets"):
            raise self.exc
        return [{"id": "1", "name": "N", "status": "ACTIVE", "effective_status": "ACTIVE"}]


def _stop_partial(account: str, kind: str) -> ci.Snapshot:
    return ci.Snapshot(account=account, state="partial", stop=kind, finished_at_utc=_iso(NOW),
                       steps={"campaigns": "ok", "adsets": "failed", "ads": "skipped"},
                       problems=["adsets were not read: stopped"])


def test_read_into_records_stop_kind():
    snap = ci.Snapshot(account="A", state="failed")
    ci.read_into(snap, StopClient(Checkpoint("checkpoint")), "act_1", structure_only=True)
    ci.finalize(snap)
    assert snap.state == "partial" and snap.stop == "checkpoint" and snap.error == ""
    assert "stop" in snap.to_dict()
    assert ci._snapshot_from({"account": "A", "state": "ok"}).stop == ""  # старые записи читаются


def test_partial_with_stop_halts_and_is_not_fresh(env):
    env.failures["A"] = _stop_partial("A", "checkpoint")
    q = _queue(env)
    q.request("A", "attention")
    q.request("B", "schedule")
    q._drain_once()
    assert [a for a, _ in env.read] == ["A"]
    st = q.state()
    assert st["halt"] and st["halt"]["until"] is None  # чекпоинт: только явный Retry
    assert st["rows"]["A"]["state"] == "failed"
    assert ar.classify(env.failures["A"]) == "global"
    env.snaps["A"] = env.failures["A"]
    assert not ar.is_fresh("A", now=NOW)
    env.snaps["A"] = ci.Snapshot(account="A", state="partial", finished_at_utc=_iso(NOW),
                                 steps={"campaigns": "ok", "adsets": "failed"})
    assert not ar.is_fresh("A", now=NOW)


@pytest.mark.parametrize("kind,wait", [("rate_limited", 300.0), ("policy_block", 1800.0)])
def test_timed_stops_use_cooldown_table(env, kind, wait):
    env.failures["A"] = _stop_partial("A", kind)
    q = _queue(env)
    q.request("A", "attention")
    q._drain_once()
    assert q.state()["halt"]["until"] == NOW + wait


def test_call_budget_stop_is_untimed(env):
    env.failures["A"] = _stop_partial("A", "call_budget")
    q = _queue(env)
    q.request("A", "attention")
    q._drain_once()
    assert q.state()["halt"]["until"] is None


def test_account_level_stop_kinds_do_not_halt():
    assert ar.classify(ci.Snapshot(account="A", state="failed", stop="logged_out")) == "account"
    assert ar.classify(_stop_partial("A", "page_unresponsive")) == "ok"


def test_forced_retry_reads_only_itself_during_untimed_halt(env):
    env.failures["A"] = _failed("A", "[WinError 10061] launcher.mlx.yt")
    q = _queue(env)
    q.request("A", "attention")
    q.request("B", "schedule")
    q._drain_once()
    del env.failures["A"]
    assert q.request("A", "retry", force=True) == {"queued": True, "reason": "queued"}
    q._drain_once()
    assert [a for a, _ in env.read] == ["A", "A", "B"]  # B дочитан только после того, как Retry удался
    assert q.state()["halt"] is None


def test_forced_retry_does_not_read_others_while_halt_stands(env):
    env.failures["A"] = _failed("A", "[WinError 10061] launcher.mlx.yt")
    q = _queue(env)
    q.request("A", "attention")
    q.request("B", "schedule")
    q._drain_once()
    q.request("A", "retry", force=True)  # снова общий сбой: пауза остаётся, B не читается
    q._drain_once()
    assert [a for a, _ in env.read] == ["A", "A"]
    assert q.state()["halt"] is not None
    # Сбой самого аккаунта при Retry пауза тоже не снимает
    env.failures["A"] = _failed("A", "no access to the ad account")
    q.request("A", "retry", force=True)
    q._drain_once()
    assert [a for a, _ in env.read] == ["A", "A", "A"] and q.state()["halt"] is not None


def test_forced_request_waits_for_timer_in_timed_halt(env):
    env.failures["A"] = _failed("A", "Meta asked to slow down (about 5 min). Nothing was called.")
    q = _queue(env)
    q.request("A", "attention")
    q._drain_once()
    until = q.state()["halt"]["until"]
    del env.failures["A"]
    assert q.request("B", "action", force=True) == {"queued": True, "reason": "halted"}
    q._drain_once()
    assert [a for a, _ in env.read] == ["A"]
    env.clock.t = until + 1
    q._drain_once()
    assert env.read[-1] == ("B", "action") and q.state()["halt"] is None


def test_requests_during_halt_are_queued_not_dropped(env):
    env.failures["A"] = _failed("A", "[WinError 10061] launcher.mlx.yt")
    q = _queue(env)
    q.request("A", "attention")
    q._drain_once()
    assert q.request("B", "schedule")["queued"] is True
    assert q.request("C", "attention")["queued"] is True
    st = q.state()
    assert st["halt"] and [e["account"] for e in st["queued"]] == ["C", "B"]


def test_running_reset_when_reader_raises_base_exception(env):
    class Boom(BaseException):
        pass

    def reader(account, trigger):
        raise Boom()

    q = ar.RefreshQueue(reader=reader, clock=env.clock)
    q.request("A", "attention")
    with pytest.raises(Boom):
        q._drain_once()
    assert q.state()["running"] is None
    assert q._drain_lock.acquire(blocking=False)  # замок отпущен
    q._drain_lock.release()


def test_stop_during_blocking_read_and_start_spawns_nothing(env):
    entered, release = threading.Event(), threading.Event()
    reads: list[str] = []

    def reader(account, trigger):
        reads.append(account)
        entered.set()
        release.wait(5)
        return env.reader(account, trigger)

    q = ar.RefreshQueue(reader=reader, clock=env.clock)
    q.request("A", "attention")
    q.request("B", "schedule")
    q.start()
    assert entered.wait(5)
    thread = q._thread
    q.stop(timeout=0.2)
    assert q._thread is thread and thread.is_alive()  # пока читает — поток не забыт
    q.start()
    assert q._thread is thread
    release.set()
    thread.join(5)
    assert not thread.is_alive()
    assert reads == ["A"]
    q.stop()
    assert q._thread is None


def test_two_drains_never_read_concurrently(env):
    entered, release = threading.Event(), threading.Event()
    reads: list[str] = []

    def reader(account, trigger):
        reads.append(account)
        entered.set()
        release.wait(5)
        return env.reader(account, trigger)

    q = ar.RefreshQueue(reader=reader, clock=env.clock)
    q.request("A", "attention")
    q.request("B", "schedule")
    t = threading.Thread(target=q._drain_once)
    t.start()
    assert entered.wait(5)
    assert q._drain_once() == 0 and reads == ["A"]
    release.set()
    t.join(5)


def test_skip_telemetry_is_not_an_api_read(monkeypatch):
    seen = {"tm": [], "rec": []}
    monkeypatch.setattr(account_spend_api._tm, "record", lambda e: seen["tm"].append(e))
    monkeypatch.setattr(account_spend_api._as, "_record", lambda step, *a, **k: seen["rec"].append(step))
    account_spend_api._telemetry_skip("A", "attention")
    assert seen["rec"] == ["api_read_skip"] and "api_read" not in seen["rec"]
    assert seen["tm"][0]["event"] == "fb_api_skip" and seen["tm"][0]["kind"] == "skipped_fresh"
