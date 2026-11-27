"""Limited_QA DEV API client: validation, idempotent submit, reconcile, no token leaks."""
from __future__ import annotations

import json

import pytest
import requests

from config import token_vault
from src.limited_qa import client, service

TOKEN = "dev-token-0123456789abcdef-SECRET"
URL = "https://api.example.test/webhook/acm-dev-xyz"


class FakeResp:
    def __init__(self, code, body=None):
        self.status_code = code
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv(client.TOKEN_ENV, TOKEN)
    monkeypatch.setenv(client.URL_ENV, URL)
    monkeypatch.setattr(service, "get_local_config_dir", lambda: str(tmp_path))
    calls = []
    script = {"post": [], "get": []}

    def _next(kind, url, kwargs):
        calls.append((kind, url, kwargs))
        item = script[kind].pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(client._SESSION, "post", lambda url, **kw: _next("post", url, kw))
    monkeypatch.setattr(client._SESSION, "get", lambda url, **kw: _next("get", url, kw))
    return calls, script


def submit():
    return service.submit(["https://Source.example/a#frag"], "Domain.com", "Mexico", "FacebookMKTO1")


RUNNING = {"status": "running", "jira_issue_key": "MAR-1", "dispatch_state": "dispatched"}


# ── validation ────────────────────────────────────────────────────────────────

def test_body_is_canonical():
    body = client.build_body("4C116B11-CC18-4FC3-8973-0EC286A79EC8", ["https://Source.example/a?x=1#f"],
                             "Domain.com", " Mexico ", "FacebookMKTO1234")
    assert body == {
        "request_id": "4c116b11-cc18-4fc3-8973-0ec286a79ec8",
        "idempotency_key": "acm-dev:4c116b11-cc18-4fc3-8973-0ec286a79ec8",
        "source_urls": ["https://source.example/a?x=1"],
        "target_domain": "domain.com", "country": "Mexico", "facebook_account": "MKTO1234",
    }
    assert client.build_body(client.new_request_id(), ["http://a.example"], "d.com", "Mexico", "")["facebook_account"] is None


@pytest.mark.parametrize("urls", [
    [], ["a"] * 4, ["ftp://a.example"], ["https://u:p@a.example"], ["https://a.example:8080/x"],
    ["https://a.example/a b"], ["https://a.example\\x"], ["https://a.example/#1", "https://A.example/#2"],
])
def test_bad_source_urls(urls):
    with pytest.raises(client.ValidationError):
        client.normalize_source_urls(urls)


@pytest.mark.parametrize("domain", ["https://domain.com", "domain.com/x", "1.2.3.4", "domain", "a b.com", "d.com:80", ""])
def test_bad_target_domain(domain):
    with pytest.raises(client.ValidationError):
        client.normalize_target_domain(domain)


@pytest.mark.parametrize("country", ["", "Mexico1", "x" * 81, "Mex_ico"])
def test_bad_country(country):
    with pytest.raises(client.ValidationError):
        client.normalize_country(country)


def test_country_accepts_unicode():
    assert client.normalize_country("  Côte   d'Ivoire ") == "Côte d'Ivoire"


# ── submit / idempotency ──────────────────────────────────────────────────────

def test_record_saved_before_post_and_headers_sent(env):
    calls, script = env
    seen = {}

    def post(url, **kw):
        seen["stored"] = service.list_requests()[0]["status"]
        calls.append(("post", url, kw))
        return FakeResp(202, {**RUNNING, "request_id": "x"})

    client._SESSION.post = post
    rec = submit()
    assert seen["stored"] == "submitting"           # persisted before the network call
    _, url, kw = calls[0]
    assert url == URL
    assert kw["headers"]["X-ACM-DEV-Token"] == TOKEN and kw["headers"]["X-ACM-DEV-Environment"] == "dev"
    assert kw["json"]["facebook_account"] == "MKTO1"
    assert rec["status"] == "running" and rec["jira_issue_key"] == "MAR-1"


def test_nothing_persisted_when_input_invalid_or_unconfigured(env, monkeypatch):
    with pytest.raises(client.ValidationError):
        service.submit(["nope"], "d.com", "Mexico")
    assert service.list_requests() == []
    monkeypatch.delenv(client.TOKEN_ENV)
    with pytest.raises(client.NotConfigured):
        submit()
    assert service.list_requests() == []


def test_lost_response_then_get_finds_it_without_new_post(env):
    calls, script = env
    script["post"] += [requests.Timeout("secret-url-in-message")]
    rec = submit()
    rid = rec["request_id"]
    assert rec["status"] == "submitting"
    script["get"].append(FakeResp(200, {**RUNNING, "request_id": rid}))
    assert service.refresh(rid)["status"] == "running"
    assert [c[0] for c in calls] == ["post", "get"]


def test_get_404_after_ambiguous_post_replays_same_body(env):
    calls, script = env
    script["post"] += [requests.ConnectionError("x"), FakeResp(202, RUNNING)]
    rec = submit()
    rid = rec["request_id"]
    script["get"].append(FakeResp(404, {"error": "request_not_found"}))
    assert service.refresh(rid)["status"] == "running"
    posts = [c[2]["json"] for c in calls if c[0] == "post"]
    assert len(posts) == 2 and posts[0] == posts[1]


def test_503_keeps_identity_and_does_not_create_new_id(env):
    calls, script = env
    script["post"].append(FakeResp(503, {"error": "dev_intake_unavailable"}))
    rec = submit()
    assert rec["status"] == "submitting" and len(service.list_requests()) == 1


def test_400_is_terminal_and_not_retried(env):
    calls, script = env
    script["post"].append(FakeResp(400, {"error": "invalid_request", "code": None}))
    rec = submit()
    assert rec["status"] == "rejected"
    assert service.refresh(rec["request_id"])["status"] == "rejected"
    assert [c[0] for c in calls] == ["post"]


def test_502_marks_failed(env):
    calls, script = env
    script["post"].append(FakeResp(502, {"error": "x"}))
    assert submit()["status"] == "failed"


# ── status handling ───────────────────────────────────────────────────────────

def test_mixed_keeps_ready_and_failed_items_and_stops_polling(env):
    calls, script = env
    script["post"].append(FakeResp(202, RUNNING))
    rid = submit()["request_id"]
    items = [{"index": 0, "url": "https://d.com/a", "state": "ready"}, {"index": 1, "url": None, "state": "failed"}]
    script["get"].append(FakeResp(200, {"status": "mixed", "result_urls": ["https://d.com/a"], "result_items": items,
                                        "completed_at": "2026-11-26T10:00:00.000Z", "unknown_field": 1}))
    rec = service.refresh(rid)
    assert rec["status"] == "mixed" and rec["result_urls"] == ["https://d.com/a"] and rec["result_items"] == items
    assert "unknown_field" not in rec
    service.refresh(rid)
    assert [c[0] for c in calls].count("get") == 1   # terminal: no more calls


def test_paused_and_unknown_stay_pollable(env):
    calls, script = env
    script["post"].append(FakeResp(202, {"status": "unknown", "dispatch_state": "unknown"}))
    rec = submit()
    assert rec["status"] == "unknown" and rec["status"] not in service.TERMINAL
    script["get"].append(FakeResp(200, {"status": "paused"}))
    assert service.refresh(rec["request_id"])["status"] == "paused"


# ── secrets ───────────────────────────────────────────────────────────────────

def test_token_never_lands_in_store_or_errors(env, tmp_path):
    calls, script = env
    script["post"].append(requests.ConnectionError(f"boom {URL} {TOKEN}"))
    rec = submit()
    assert TOKEN not in json.dumps(rec) and TOKEN not in (tmp_path / service.STORE_NAME).read_text(encoding="utf-8")
    with pytest.raises(client.Unreachable) as exc:
        script["post"].append(requests.ConnectionError(TOKEN))
        client.post_request({})
    assert TOKEN not in str(exc.value) and exc.value.__cause__ is None


def test_token_key_is_protected_by_vault():
    assert token_vault.is_secret_env_key("ACM_DEV_TOKEN")


# ── HTTP surface ──────────────────────────────────────────────────────────────

def test_routes_hide_internals_and_validate(env, monkeypatch):
    from fastapi.testclient import TestClient

    from src.web import app as web

    monkeypatch.setattr(service, "start_poller", lambda: None)
    calls, script = env
    H = {"Origin": "http://127.0.0.1:9090"}
    c = TestClient(web.app, base_url="http://127.0.0.1:9090")
    assert c.get("/limited-qa").status_code == 200
    bad = c.post("/api/limited-qa/requests", headers=H, json={"source_urls": ["nope"], "target_domain": "d.com", "country": "Mexico"})
    assert bad.status_code == 400 and "URL" in bad.json()["error"]
    script["post"].append(FakeResp(202, RUNNING))
    ok = c.post("/api/limited-qa/requests", headers=H, json={"source_urls": ["https://a.example/x"], "target_domain": "d.com",
                                                   "country": "Mexico"})
    assert ok.status_code == 202
    listing = c.get("/api/limited-qa/requests").json()
    assert listing["requests"][0]["status"] == "running"
    assert TOKEN not in json.dumps(listing) and "idempotency_key" not in listing["requests"][0]
    assert c.post("/api/limited-qa/requests/not-a-uuid/refresh", headers=H).status_code == 400


def test_requests_are_pinned_to_ipv4_unless_auto(monkeypatch):
    seen = {}
    monkeypatch.delenv(client.IP_ENV, raising=False)
    monkeypatch.setattr(client.HTTPAdapter, "init_poolmanager", lambda self, *a, **kw: seen.update(kw))
    client._IPv4Adapter().init_poolmanager(1, 1)
    assert seen["source_address"] == ("0.0.0.0", 0)
    seen.clear()
    monkeypatch.setenv(client.IP_ENV, "auto")
    client._IPv4Adapter().init_poolmanager(1, 1)
    assert "source_address" not in seen
