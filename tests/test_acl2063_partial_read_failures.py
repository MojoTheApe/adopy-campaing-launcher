"""Прерванная структура остаётся доступной, но не скрывает ошибку и остановки."""
from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.analysis_agent import account_refresh as ar, account_spend_api as api, campaign_inventory as ci
from src.fb_ads_rpa.profile_lease import profile_lock
from src.fb_api.graph import GraphClient
from tests.profile_lease_fakes import fake_profile


class Transport:
    def __init__(self, mode, edge):
        self.mode, self.edge, self.requests = mode, edge, []

    def state(self):
        return {"kind": "ready"}

    def request(self, method, path, params):
        assert method == "GET"
        self.requests.append(path)
        if path.endswith("/" + self.edge):
            if self.mode == "exception":
                raise ConnectionError("Synthetic proxy disconnected after campaigns")
            if self.mode in {"network", "call_unknown"}:
                return {"error": self.mode}
            if self.mode == "graph_error":
                return {"status": 400, "body": {"error": {"code": 100, "message": "Synthetic edge refused"}}}
        if path.endswith("/campaigns"):
            body = {"data": [{"id": "1", "name": "Synthetic campaign", "status": "ACTIVE", "effective_status": "ACTIVE"}]}
        elif path.endswith("/adsets"):
            body = {"data": [{"id": "11", "name": "Synthetic adset", "campaign_id": "1", "status": "ACTIVE"}]}
        elif path.endswith("/ads"):
            body = {"data": []}
        else:
            body = {"id": "act_1234567", "account_id": "1234567", "currency": "USD", "account_status": 1}
        return {"status": 200, "body": body}


@pytest.fixture
def read_result(monkeypatch, tmp_path):
    monkeypatch.setattr(ci, "_store_path", lambda: tmp_path / "inventory.json")
    monkeypatch.setattr(ci, "backend_ready", lambda: True)
    telemetry = []
    monkeypatch.setattr(api, "_telemetry", lambda *a, **kw: telemetry.append((a, kw)))
    monkeypatch.setattr(ci._as, "_entry_for", lambda account: SimpleNamespace(
        ad_account_id="act_1234567", profile_id=fake_profile(account)))

    def read(mode, edge, entry="cold", *, closing_error=False):
        transport = Transport(mode, edge)
        client = GraphClient(transport, min_interval_s=0)
        sess = SimpleNamespace(client=client, act="act_1234567", profile_id=fake_profile("A"), notes={})

        @contextmanager
        def session(*a, **kw):
            yield sess
            if closing_error:
                raise ConnectionError("Synthetic proxy disconnected while closing")

        monkeypatch.setattr(api, "_session", session)
        if entry == "leased":
            with profile_lock(fake_profile("A")):
                snap = ci.read_in_session("A", sess, trigger="action")
        else:
            snap = ci.read_via_api("A", structure_only=True, trigger="retry")
        snap.finished_at = "2026-10-24 08:10:00"
        snap.finished_at_utc = datetime.now(timezone.utc).isoformat()
        ci.put_result(snap)
        assert telemetry[-1][0][2] == ci.read_succeeded(snap)
        assert telemetry[-1][0][4]["trigger"] == ("action" if entry == "leased" else "retry")
        return snap, transport

    return read


@pytest.mark.parametrize("entry", ["cold", "leased"])
@pytest.mark.parametrize("edge", ["adsets", "ads"])
@pytest.mark.parametrize("mode", ["network", "call_unknown", "graph_error", "exception"])
def test_partial_edge_failure_retains_campaigns_and_exposes_retry(read_result, entry, edge, mode):
    snap, transport = read_result(mode, edge, entry)
    assert snap.state == "partial" and snap.campaigns[0]["id"] == "1"
    queue = ar.RefreshQueue(accounts=lambda: ["A"])
    queue.record_result(snap, trigger="action")
    row = queue.state()["rows"]["A"]
    assert row["state"] == "failed" and row["error"]
    assert queue.state()["halt"] is None
    assert not ar.is_fresh("A")
    assert transport.requests.count("/act_1234567/campaigns") == 1
    assert not any("insights" in path for path in transport.requests)


def test_close_failure_is_not_a_fresh_success_even_after_all_edges_succeeded(read_result):
    snap, _ = read_result("ok", "none", closing_error=True)
    assert snap.state == "partial" and all(v == "ok" for v in snap.steps.values())
    queue = ar.RefreshQueue(accounts=lambda: ["A"])
    queue.record_result(snap, trigger="retry")
    assert queue.state()["rows"]["A"]["state"] == "failed"
    assert "closing" in queue.state()["rows"]["A"]["error"]
    assert not ar.is_fresh("A")


def test_incomplete_retry_does_not_clear_persisted_checkpoint(read_result):
    old = "2026-10-24T08:00:00+00:00"
    ci.put_result(ci.Snapshot(account="A", state="failed", stop="checkpoint", error="Synthetic checkpoint", finished_at_utc=old))
    queue = ar.RefreshQueue(reader=lambda account, trigger: read_result("exception", "adsets")[0], accounts=lambda: ["A"])
    assert queue.state()["halt"]["kind"] == "checkpoint"
    queue.request("A", "retry", force=True)
    assert queue._drain_once() == 1
    assert queue.state()["halt"]["kind"] == "checkpoint"
    assert queue.state()["rows"]["A"]["state"] == "failed"
    assert ci.latest_stops()["A"] == ("checkpoint", old)


def test_intentional_page_limit_partial_remains_usable_and_fresh(read_result):
    snap, _ = read_result("ok", "none")
    snap.state = "partial"
    snap.steps["ads"] = "truncated"
    snap.problems.append("The ads list reached the page limit")
    ci.put_result(snap)
    assert ar.classify(snap) == "ok" and ar.is_fresh("A")
