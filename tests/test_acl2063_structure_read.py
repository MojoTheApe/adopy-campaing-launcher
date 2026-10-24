"""ACL-2063: чтение только структуры — без Insights, без периода, финал «ok» по трём шагам."""
from __future__ import annotations

import contextlib

import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.analysis_agent import campaign_inventory as ci  # noqa: E402
from src.analysis_agent.rules import evidence as ev  # noqa: E402


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from tests.profile_lease_fakes import fake_profile
    monkeypatch.setattr(ci, "_store_path", lambda: tmp_path / "campaign_inventory.json")
    # The production machine relay now binds the read to its physical target;
    # these isolated transport tests retain all assertions with a real valid key.
    monkeypatch.setattr(ci._as, "_entry_for", lambda account: SimpleNamespace(
        ad_account_id="act_12345678", profile_id=fake_profile(account)))
    ci._RUNNING.clear()
    yield tmp_path


class FakeClient:
    def __init__(self):
        self.calls = 0
        self.stopped = None
        self.truncated: list[str] = []
        self.log: list[str] = []

    def account(self, act):
        return {"id": act, "account_id": act[4:], "currency": "USD", "account_status": 1}

    def get_all(self, path, params=None, max_pages=5):
        self.log.append(path)
        key = path.rsplit("/", 1)[-1]
        row = {"id": "1", "name": "N", "status": "ACTIVE", "effective_status": "ACTIVE",
               "campaign_id": "1", "adset_id": "11"}
        return {"campaigns": [row], "adsets": [{**row, "id": "11"}], "ads": [{**row, "id": "111"}]}[key]

    def insights_range(self, *a, **kw):
        self.log.append("insights")
        raise AssertionError("insights requested")


def _structure_snap():
    snap = ci.Snapshot(account="A", state="failed")
    ci.read_into(snap, FakeClient(), "act_1", structure_only=True)
    ci.finalize(snap)
    return snap


def test_structure_only_reads_no_insights():
    client = FakeClient()
    snap = ci.Snapshot(account="A", state="failed")
    ci.read_into(snap, client, "act_1", structure_only=True)
    assert not any("insights" in p for p in client.log)
    assert set(snap.steps) == {"campaigns", "adsets", "ads"}
    assert snap.insights == {}


def test_structure_only_finalize_ok():
    snap = _structure_snap()
    assert snap.state == "ok"
    assert (snap.date_from, snap.date_to) == ("", "")


def test_run_read_stores_snapshot(monkeypatch):
    monkeypatch.setattr(ci, "backend_ready", lambda: True)
    monkeypatch.setattr(ci._as, "_lock_for", lambda account: contextlib.nullcontext())  # профиль не открываем
    seen = {}

    def fake(account, date_from="", date_to="", *, structure_only=False, trigger=""):
        seen.update(structure_only=structure_only, trigger=trigger)
        return _structure_snap().__class__(**{**_structure_snap().to_dict(), "account": account})

    monkeypatch.setattr(ci, "read_via_api", fake)
    done = ci.run_read("A", trigger="schedule")
    assert seen == {"structure_only": True, "trigger": "schedule"}
    snap = ci.get_snapshot("A")
    assert snap.state == "ok" and snap.date_from == "" and done.state == "ok"


def test_rules_without_insights_not_reconciled():
    out = ev.reconcile_spend(_structure_snap(), (date(2026, 10, 8), date(2026, 10, 8)), 100.0)
    assert out["status"] == "not_reconciled"


@pytest.mark.parametrize("kind", ["checkpoint", "rate_limited"])
def test_later_local_failure_does_not_erase_or_extend_an_unresolved_global_stop(kind):
    old_at = "2026-10-24T08:00:00+00:00"
    ci.put_result(ci.Snapshot(account="A", state="failed", stop=kind,
                              error="Original stop", finished_at_utc=old_at))
    ci.put_result(ci.Snapshot(account="A", state="failed", error="The account map changed",
                              finished_at_utc="2026-10-24T08:04:00+00:00"))
    assert ci.latest_stops()["A"] == (kind, old_at)
    ci.put_result(ci.Snapshot(account="A", state="empty", steps={s: "ok" for s in ci.STRUCTURE_STEPS},
                              finished_at_utc="2026-10-24T08:05:00+00:00"))
    assert "A" not in ci.latest_stops()


def _ctx_recorder(log, name):
    from contextlib import contextmanager

    @contextmanager
    def cm(*args):
        log.append(f"enter {name}")
        try:
            yield
        finally:
            log.append(f"exit {name}")
    return cm


def test_run_read_refuses_without_api_backend_and_opens_nothing(monkeypatch):
    monkeypatch.setattr(ci, "backend_ready", lambda: False)

    def boom(*a, **kw):
        raise AssertionError("session must not be opened")

    monkeypatch.setattr(ci, "read_via_api", boom)
    done = ci.run_read("A", trigger="schedule")
    assert done.state == "failed"
    assert done.error == ci.NEEDS_API_MESSAGE and done.error_code == ci.NEEDS_API_CODE


def test_run_read_holds_reading_and_account_lock_around_the_read(monkeypatch):
    log: list[str] = []
    monkeypatch.setattr(ci, "backend_ready", lambda: True)
    monkeypatch.setattr(ci.browser_operation, "reading", _ctx_recorder(log, "reading"))
    monkeypatch.setattr(ci._as, "_lock_for", _ctx_recorder(log, "lock"))

    def fake(account, *a, **kw):
        log.append("read")
        snap = _structure_snap()
        snap.account = account
        return snap

    monkeypatch.setattr(ci, "read_via_api", fake)
    ci.run_read("A", trigger="attention")
    assert log == ["enter reading", "enter lock", "read", "exit lock", "exit reading"]


def test_run_read_failure_keeps_previous_good_snapshot(monkeypatch):
    monkeypatch.setattr(ci, "backend_ready", lambda: True)
    monkeypatch.setattr(ci._as, "_lock_for", lambda account: contextlib.nullcontext())  # профиль не открываем
    good = _structure_snap()
    good.account = "A"
    ci.put_result(good)

    def fail(*a, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(ci, "read_via_api", fail)
    done = ci.run_read("A", trigger="schedule")
    assert done.state == "failed" and "boom" in done.error
    kept = ci.get_snapshot("A")
    assert kept is not None and kept.state == "ok" and len(kept.campaigns) == 1
    assert (ci.get_attempt("A") or {}).get("state") == "failed"


def _stub_session(monkeypatch, events):
    from contextlib import contextmanager
    from types import SimpleNamespace

    from src.analysis_agent import account_spend_api as api

    @contextmanager
    def session(account, **kw):
        yield SimpleNamespace(client=FakeClient(), act="act_1", notes={"calls": 3})

    monkeypatch.setattr(api, "_session", session)
    monkeypatch.setattr(api, "_telemetry", lambda kind, account, ok, took, notes, **kw: events.append(dict(notes)))


def test_read_via_api_structure_only_records_trigger_and_blanks_dates(monkeypatch):
    events: list[dict] = []
    _stub_session(monkeypatch, events)
    snap = ci.read_via_api("A", "2026-10-01", "2026-10-02", structure_only=True, trigger="schedule")
    assert snap.state == "ok" and (snap.date_from, snap.date_to) == ("", "")
    assert events == [{"calls": 3, "trigger": "schedule"}]


def test_read_via_api_empty_trigger_adds_no_trigger_note(monkeypatch):
    events: list[dict] = []
    _stub_session(monkeypatch, events)
    ci.read_via_api("A", structure_only=True)
    assert events and "trigger" not in events[0]


def test_failed_attempt_persists_its_stop_and_a_later_success_clears_it():
    bad = ci.Snapshot(account="A", state="failed", error="checkpoint", stop="checkpoint",
                      finished_at_utc="2026-10-23T08:00:00+00:00")
    ci.put_result(bad)
    assert ci.latest_stops() == {"A": ("checkpoint", "2026-10-23T08:00:00+00:00")}
    ci.put_result(_structure_snap())
    assert ci.latest_stops() == {}


def test_partial_snapshot_stop_is_listed_until_the_next_read():
    part = ci.Snapshot(account="B", state="partial", stop="rate_limited",
                       finished_at_utc="2026-10-23T08:00:00+00:00")
    ci.put_result(part)
    assert ci.latest_stops() == {"B": ("rate_limited", "2026-10-23T08:00:00+00:00")}


def test_waiting_request_joins_a_newer_same_session_result_without_reopening(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from datetime import datetime, timezone, timedelta
    import threading
    from src.analysis_agent import account_spend as spend
    monkeypatch.setattr(ci, '_store_path', lambda: tmp_path / 'inventory.json')
    monkeypatch.setattr(ci, 'backend_ready', lambda: True)
    began, released = threading.Event(), threading.Event()
    @contextmanager
    def physical(account):
        began.set()
        assert released.wait(3)
        yield
    monkeypatch.setattr(spend, '_lock_for', physical)
    opened = []
    monkeypatch.setattr(ci, 'read_via_api', lambda *a, **k: opened.append(a))
    old = ci.Snapshot(account='A', state='empty', finished_at_utc=(datetime.now(timezone.utc)-timedelta(hours=2)).isoformat())
    ci.put_result(old)
    result = []
    thread = threading.Thread(target=lambda: result.append(ci.run_read('A', trigger='action')))
    thread.start()
    try:
        assert began.wait(3)
        # Actual same-session publisher result arrives while physical profile is held.
        fresh = ci.Snapshot(account='A', state='empty', finished_at_utc=datetime.now(timezone.utc).isoformat(),
                            steps={step: 'ok' for step in ci.STRUCTURE_STEPS})
        ci.put_result(fresh)
    finally:
        released.set(); thread.join(3)
    assert not thread.is_alive() and opened == []
    assert result[0].finished_at_utc == fresh.finished_at_utc
