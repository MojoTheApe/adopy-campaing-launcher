from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.web import app as web_app
from src.web import limited_qa


DEV_URL = "https://n8n.maragents.cloud/webhook/acm-dev-0123456789abcdef"
DEV_TOKEN = "t" * 48


def enable(monkeypatch):
    monkeypatch.setenv("ACL_LIMITED_QA_ENV", "dev")
    monkeypatch.setenv("ACL_LIMITED_QA_INTAKE_URL", DEV_URL)
    monkeypatch.setenv("ACL_LIMITED_QA_TOKEN", DEV_TOKEN)


def submission(**overrides):
    value = {
        "source_urls": ["https://source.example/landing"],
        "target_domain": "domain.com",
        "country": "Mexico",
        "facebook_account": None,
    }
    value.update(overrides)
    return value


def test_configuration_fails_closed_outside_dev(monkeypatch):
    monkeypatch.delenv("ACL_LIMITED_QA_ENV", raising=False)
    monkeypatch.delenv("ACL_LIMITED_QA_INTAKE_URL", raising=False)
    monkeypatch.delenv("ACL_LIMITED_QA_TOKEN", raising=False)
    assert limited_qa.availability()["enabled"] is False

    monkeypatch.setenv("ACL_LIMITED_QA_ENV", "prod")
    monkeypatch.setenv("ACL_LIMITED_QA_INTAKE_URL", DEV_URL)
    monkeypatch.setenv("ACL_LIMITED_QA_TOKEN", DEV_TOKEN)
    with pytest.raises(limited_qa.LimitedQaConfigError, match="must be dev"):
        limited_qa.load_config()


@pytest.mark.parametrize("url", [
    "http://n8n.maragents.cloud/webhook/acm-dev-x",
    "https://evil.example/webhook/acm-dev-x",
    "https://n8n.maragents.cloud/webhook/prod-x",
    "https://n8n.maragents.cloud/webhook/acm-dev-x?token=leak",
])
def test_configuration_rejects_untrusted_webhooks(monkeypatch, url):
    enable(monkeypatch)
    monkeypatch.setenv("ACL_LIMITED_QA_INTAKE_URL", url)
    with pytest.raises(limited_qa.LimitedQaConfigError, match="not trusted"):
        limited_qa.load_config()


def test_submission_canonicalizes_root_and_keeps_facebook_optional():
    value = limited_qa.normalize_submission(submission(
        source_urls=["https://source.example/a#part", "https://source.example/b"],
        target_domain="https://lpa.Domain.com/",
        facebook_account="",
    ))
    assert value["target_domain"] == "domain.com"
    assert value["facebook_account"] is None
    assert value["source_urls"] == ["https://source.example/a", "https://source.example/b"]
    assert value["idempotency_key"] == "acm-dev:" + value["request_id"]


def test_submission_rejects_more_than_three_duplicates_and_arbitrary_subdomains():
    with pytest.raises(limited_qa.LimitedQaValidationError, match="one and three"):
        limited_qa.normalize_submission(submission(source_urls=[f"https://s.example/{i}" for i in range(4)]))
    with pytest.raises(limited_qa.LimitedQaValidationError, match="Duplicate"):
        limited_qa.normalize_submission(submission(source_urls=["https://s.example/a", "https://s.example/a"]))
    with pytest.raises(limited_qa.LimitedQaValidationError, match="root domain"):
        limited_qa.normalize_submission(submission(target_domain="promo.domain.com"))


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self):
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return FakeResponse(202, {"request_id": kwargs["json"]["request_id"], "status": "running"})

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return FakeResponse(200, {"request_id": kwargs["params"]["request_id"], "status": "succeeded", "result_urls": ["https://domain.com/page"]})


def test_client_sends_token_only_in_dev_headers(monkeypatch):
    enable(monkeypatch)
    fake = FakeSession()
    client = limited_qa.LimitedQaClient(limited_qa.load_config(), fake)
    payload = limited_qa.normalize_submission(submission())
    client.submit(payload)
    method, url, kwargs = fake.calls[0]
    assert (method, url) == ("POST", DEV_URL)
    assert kwargs["headers"]["X-ACM-DEV-Environment"] == "dev"
    assert kwargs["headers"]["X-ACM-DEV-Token"] == DEV_TOKEN
    assert DEV_TOKEN not in str(kwargs["json"])


def test_history_is_atomic_local_and_never_persists_a_token(monkeypatch):
    row = limited_qa.normalize_submission(submission())
    row.update({"status": "running", "created_at": "2026-11-24T10:00:00+00:00", "token": "forbidden"})
    limited_qa.save_request(row)
    saved = limited_qa.get_request(row["request_id"])
    assert saved and "token" not in saved
    raw = (Path(limited_qa.get_local_config_dir()) / "limited_qa_requests_dev.json").read_text()
    assert "forbidden" not in raw


class StubClient:
    def __init__(self):
        self.submissions = []

    def submit(self, payload):
        self.submissions.append(payload)
        return {
            "request_id": payload["request_id"],
            "status": "running",
            "jira_issue_key": "MAR-123",
            "jira_issue_url": "https://mar-adopy.atlassian.net/browse/MAR-123",
            "dispatch_state": "dispatched",
        }

    def status(self, request_id):
        return {
            "request_id": request_id,
            "status": "succeeded",
            "result_urls": ["https://domain.com/landing"],
            "result_items": [{"index": 0, "url": "https://domain.com/landing", "state": "ready"}],
        }


def test_acm_endpoints_submit_poll_and_hide_internal_replay_data(monkeypatch):
    enable(monkeypatch)
    stub = StubClient()
    monkeypatch.setattr(web_app, "_limited_qa_client", lambda: stub)
    client = TestClient(web_app.app, base_url="http://127.0.0.1:9090")

    created = client.post("/api/limited-qa/requests", json=submission())
    assert created.status_code == 202
    assert created.json()["status"] == "running"
    assert stub.submissions[0]["facebook_account"] is None
    request_id = created.json()["request_id"]

    status = client.get(f"/api/limited-qa/requests/{request_id}")
    assert status.status_code == 200
    assert status.json()["result_urls"] == ["https://domain.com/landing"]

    rows = client.get("/api/limited-qa/requests").json()["requests"]
    assert rows[0]["status"] == "succeeded"
    assert "payload" not in rows[0]
    assert "idempotency_key" not in rows[0]
    assert DEV_TOKEN not in str(rows)


def test_page_explains_optional_account_and_root_domain(monkeypatch):
    enable(monkeypatch)
    client = TestClient(web_app.app, base_url="http://localhost")
    page = client.get("/limited-qa")
    assert page.status_code == 200
    assert "Facebook account" in page.text and "optional" in page.text
    assert "domain.com/page" in page.text
    assert "/static/limited_qa.js" in page.text
