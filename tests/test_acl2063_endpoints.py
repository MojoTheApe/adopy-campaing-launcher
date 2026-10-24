"""Thin actual HTTP handlers queue work; they never synchronously open Meta."""
from datetime import datetime, timezone
import pytest
from fastapi.testclient import TestClient
from src.analysis_agent import account_refresh as ar
from src.analysis_agent import campaign_inventory as ci
from src.analysis_agent.account_refresh_state import RefreshStore


@pytest.fixture
def world(tmp_path, monkeypatch):
    from src.web import app as web
    monkeypatch.setenv("ACL_AUTO_REFRESH", "1")
    monkeypatch.setattr(ci, "backend_ready", lambda: True)
    monkeypatch.setattr(ci, "_store_path", lambda: tmp_path / "inventory.json")
    monkeypatch.setattr(ar, "dashboard_accounts", lambda: ["A"])
    monkeypatch.setattr(web, "_mlx_batch_refusal", lambda form, account: None)
    class LocalTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 24, 8, tzinfo=timezone.utc)
    monkeypatch.setattr(ar, "datetime", LocalTime)
    reads, starts = [], []
    def reader(account, trigger):
        reads.append((account, trigger))
        snap = ci.Snapshot(account=account, state="empty", finished_at="08:01",
                           finished_at_utc="2026-10-24T08:01:00+00:00",
                           steps={step: "ok" for step in ci.STRUCTURE_STEPS})
        ci.put_result(snap)
        return snap
    q = ar.RefreshQueue(reader, clock=lambda: LocalTime.now().timestamp(), store=RefreshStore(tmp_path), accounts=ar.dashboard_accounts)
    monkeypatch.setattr(ar, "_QUEUE", q)
    monkeypatch.setattr(q, "start", lambda: starts.append(True))
    def forbidden(*args, **kwargs):
        raise AssertionError("An HTTP handler must not synchronously open a profile")
    monkeypatch.setattr(ci, "read_via_api", forbidden)
    return web, TestClient(web.app, base_url="http://127.0.0.1:9095"), q, reads, starts


def test_page_open_uses_server_accounts_once_and_does_not_run_reader(world):
    _web, client, q, reads, starts = world
    response = client.post("/account-refresh/page-open", data={"accounts": "foreign"})
    assert response.status_code == 200 and response.json()["sweep"] == "started"
    assert reads == [] and len(starts) == 1
    assert q.state()["queued"][0]["account"] == "A"
    assert client.post("/account-refresh/page-open").json()["sweep"] == "already_done"
    q._drain_once()
    assert reads == [("A", "schedule:2026-10-24/08:00")]
    state = client.get("/account-refresh/state").json()
    assert state["running"] is None and state["queued"] == []
    assert state["rows"]["A"]["state"] == "empty"
    assert state["accounts"]["A"]["state"] == "empty"


def test_period_request_does_not_trigger_first_sweep(world):
    _web, client, q, reads, _starts = world
    for period in range(10):
        response = client.post("/account-refresh/page-open", data={"view_only": "1", "period": str(period)})
        assert response.json()["sweep"] == "view_only"
        assert q._drain_once() == 0
    assert reads == []
    assert client.post("/account-refresh/page-open").json()["sweep"] == "started"


def test_only_attention_and_retry_are_public_and_alias_uses_canonical_account(world):
    _web, client, q, reads, _starts = world
    for trigger in ["action", "schedule", "", "unsupported"]:
        assert client.post("/account-refresh/request", data={"account": "A", "trigger": trigger}).status_code == 400
    assert client.post("/account-refresh/request", data={"account": "a", "trigger": "attention"}).status_code == 200
    assert client.post("/account-refresh/request", data={"account": "removed", "trigger": "retry"}).status_code == 404
    assert reads == []
    q._drain_once()
    assert reads == [("A", "attention")]


def test_backend_refusal_and_batch_refusal_do_not_queue_or_open(world, monkeypatch):
    web, client, q, reads, _starts = world
    monkeypatch.setattr(ci, "backend_ready", lambda: False)
    assert client.post("/account-refresh/page-open").status_code == 409
    assert client.post("/account-refresh/request", data={"account": "A", "trigger": "retry"}).status_code == 409
    assert q.state()["queued"] == [] and reads == []
    monkeypatch.setattr(ci, "backend_ready", lambda: True)
    monkeypatch.setattr(web, "_mlx_batch_refusal", lambda *a: web.JSONResponse(
        {"ok": False, "code": "mlx_session_lost", "error": "Multilogin session lost"}, status_code=409))
    assert client.post("/account-refresh/request", data={"account": "A", "trigger": "retry"}).status_code == 409
    assert client.post("/account-refresh/page-open").status_code == 409
    assert q.state()["queued"] == [] and reads == []


def test_local_state_get_preserves_cached_snapshot_with_no_read(world):
    _web, client, q, reads, _starts = world
    ci.put_result(ci.Snapshot(account="A", state="empty", finished_at="07:55"))
    for period in ["all", "2026-10-01", "2026-10-22"]:
        body = client.get("/account-refresh/state", params={"span": period}).json()
        assert body["accounts"]["A"]["finished_at"] == "07:55"
    assert reads == [] and q.state()["queued"] == []
