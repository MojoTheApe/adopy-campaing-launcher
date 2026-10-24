"""Windows start from page opening; date navigation does not consume them."""
from datetime import datetime, timezone
import pytest
from src.analysis_agent import account_refresh as ar
from src.analysis_agent import campaign_inventory as ci
from src.analysis_agent.account_refresh_state import RefreshStore


@pytest.mark.parametrize("at,expected", [("07:59", None), ("08:00", "08:00"), ("12:59", "08:00"), ("13:00", "13:00"), ("17:59", "13:00"), ("18:00", "18:00"), ("23:59", "18:00")])
def test_local_window_boundaries(at, expected):
    assert ar.current_window(datetime.fromisoformat("2026-10-24T" + at)) == expected


def test_explicit_setting_test9095_and_packaged_working_default(monkeypatch):
    monkeypatch.delenv("ACL_AUTO_REFRESH", raising=False)
    monkeypatch.setattr(ar.sys, "frozen", False, raising=False)
    assert ar.auto_refresh_enabled(9090)
    assert not ar.auto_refresh_enabled(9095)
    monkeypatch.setenv("ACL_AUTO_REFRESH", "1")
    assert ar.auto_refresh_enabled(9095)
    monkeypatch.setenv("ACL_AUTO_REFRESH", "0")
    assert not ar.auto_refresh_enabled(9090)
    monkeypatch.delenv("ACL_AUTO_REFRESH")
    monkeypatch.setattr(ar.sys, "frozen", True, raising=False)
    assert ar.auto_refresh_enabled(9091)


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("ACL_AUTO_REFRESH", "1")
    now = datetime(2026, 10, 24, 8, tzinfo=timezone.utc).timestamp()
    snaps, seen, skips, accounts = {}, [], [], ["A", "B", "C"]
    monkeypatch.setattr(ci, "get_snapshot", lambda a: snaps.get(a))
    monkeypatch.setattr(ci, "get_attempt", lambda a: None)
    monkeypatch.setattr(ci, "latest_stops", lambda: {})
    def read(account, trigger):
        seen.append((account, trigger))
        snap = ci.Snapshot(account=account, state="empty", finished_at="08:01",
                           finished_at_utc=datetime.fromtimestamp(now, timezone.utc).isoformat(),
                           steps={step: "ok" for step in ci.STRUCTURE_STEPS})
        snaps[account] = snap
        return snap
    def queue(at=None):
        q = ar.RefreshQueue(read, clock=lambda: now if at is None else at, store=RefreshStore(tmp_path),
                            accounts=lambda: list(accounts))
        q._telemetry_skipped = lambda account, label: skips.append((account, label))
        return q
    return queue, snaps, seen, skips, accounts, now


def test_first_open_skips_successful_empty_and_reload_restart_is_idempotent(world):
    new, snaps, seen, skips, _accounts, now = world
    snaps["A"] = ci.Snapshot(account="A", state="empty", finished_at="07:55",
                             finished_at_utc=datetime.fromtimestamp(now - 300, timezone.utc).isoformat())
    q = new()
    assert q.page_open(now=datetime(2026, 10, 24, 8), port=9095) == {
        "sweep": "started", "window": "08:00", "queued": 2}
    assert new().page_open(now=datetime(2026, 10, 24, 8, 1), port=9095)["sweep"] == "already_done"
    assert new()._drain_once() == 2
    assert [a for a, _ in seen] == ["B", "C"]
    assert skips == [("A", "schedule:2026-10-24/08:00")]
    assert q.state()["sweep"] == {"window": "2026-10-24/08:00", "done": 3, "total": 3}
    assert new().page_open(now=datetime(2026, 10, 24, 12), port=9095)["sweep"] == "already_done"


def test_late_first_open_starts_only_current_window(world):
    new, _snaps, seen, _skips, _accounts, _now = world
    q = new(datetime(2026, 10, 24, 19).astimezone().timestamp())
    assert q.page_open(now=datetime(2026, 10, 24, 19), port=9095)["window"] == "18:00"
    q._drain_once()
    assert len(seen) == 3
    assert all(label == "schedule:2026-10-24/18:00" for _, label in seen)


def test_view_only_period_load_does_not_consume_or_start_first_window(world):
    new, _snaps, seen, _skips, _accounts, _now = world
    for hour in [7, 8, 9, 10, 11, 12, 13, 14, 18, 19]:
        q = new()
        assert q.page_open(now=datetime(2026, 10, 24, hour), port=9095, view_only=True)["sweep"] == "view_only"
        assert q._drain_once() == 0
    assert seen == []
    assert new().page_open(now=datetime(2026, 10, 24, 19), port=9095)["sweep"] == "started"


def test_removed_member_is_not_read_and_attention_upgrade_completes_sweep(world):
    new, _snaps, seen, _skips, accounts, _now = world
    q = new()
    q.page_open(now=datetime(2026, 10, 24, 8), port=9095)
    new().request("C", "attention")
    accounts.remove("A")
    q._drain_once()
    assert [a for a, _ in seen] == ["C", "B"]
    assert q.state()["sweep"]["done"] == 3


def test_restart_does_not_catch_up_a_missed_saved_window(world):
    new, _snaps, seen, _skips, _accounts, _now = world
    q = new()
    q.page_open(now=datetime(2026, 10, 24, 8), port=9095)
    # Persisted 08:00 work was never executed. Opening a process at 13:00
    # must not read it before the real page load admits the current window.
    restart = new(datetime(2026, 10, 24, 13).astimezone().timestamp())
    assert restart._drain_once() == 0 and seen == []
    restart.page_open(now=datetime(2026, 10, 24, 13), port=9095)
    assert restart._drain_once() == 3
    assert all(trigger.endswith('/13:00') for _, trigger in seen)
