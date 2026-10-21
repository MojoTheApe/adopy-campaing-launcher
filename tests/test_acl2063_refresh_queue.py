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
    monkeypatch.setattr(account_spend_api, "_telemetry",
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
    assert env.telemetry and "skipped_fresh" in repr(env.telemetry[0])


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
    assert q.request("C", "attention") == {"queued": False, "reason": "halted"}


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
    assert q.state()["halt"] is None
    q._drain_once()
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
