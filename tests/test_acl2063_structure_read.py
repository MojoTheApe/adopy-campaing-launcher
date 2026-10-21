"""ACL-2063: чтение только структуры — без Insights, без периода, финал «ok» по трём шагам."""
from __future__ import annotations

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
    monkeypatch.setattr(ci, "_store_path", lambda: tmp_path / "campaign_inventory.json")
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
