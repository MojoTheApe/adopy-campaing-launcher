"""POST /account-spend/turn-off: by-name requests and the busy-profile refusal (ACL-1379)."""
from __future__ import annotations

import asyncio
import json
from urllib.parse import urlencode

import pytest

from src.analysis_agent import account_spend as sp
from src.web import app as web
from src.web import publish_jobs


def _post(fields: dict) -> tuple[int, dict]:
    body = urlencode(fields).encode()
    scope = {"type": "http", "method": "POST", "path": "/account-spend/turn-off",
             "headers": [(b"content-type", b"application/x-www-form-urlencoded")]}

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def run():
        from starlette.requests import Request

        return await web.account_spend_turn_off(Request(scope, receive=receive))

    response = asyncio.run(run())
    return response.status_code, json.loads(response.body)


BY_NAME = {"account": "FacebookDoc19", "level": "campaign", "object_id": "", "match": "name",
           "name": "Campaign 1", "date_from": "2026-09-01", "date_to": "2026-09-01"}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(sp, "_store_path", lambda: tmp_path / "spend.json")
    monkeypatch.setattr(sp, "_profile_key", lambda account, *a, **k: "profile-" + account)
    monkeypatch.setattr(publish_jobs, "profile_key_for", lambda label: "profile-" + label)
    monkeypatch.setattr(publish_jobs, "_jobs", {})


def _job(label: str, status: str, finished: str | None = None) -> None:
    publish_jobs._jobs[label + status] = {"account_label": label, "status": status,
                                          "finished_at": finished}


def test_a_name_request_reaches_turn_off_with_the_match_and_the_busy_check(monkeypatch):
    seen = {}

    def fake(account, level, object_id, **kw):
        seen.update(kw, account=account, level=level, object_id=object_id)
        return sp.TurnOffResult(account=account, level=level, object_id="", name="Campaign 1",
                                state="off", matched_by="name")

    monkeypatch.setattr(sp, "turn_off", fake)
    status, body = _post(BY_NAME)
    assert status == 200 and body["result"]["state"] == "off"
    assert seen["match"] == "name" and seen["object_id"] == "" and seen["expected_name"] == "Campaign 1"
    # The in-lock check sees the same profile queue.
    assert seen["profile_busy"]("profile-FacebookDoc19") is False
    _job("FacebookDoc19", "queued")
    assert seen["profile_busy"]("profile-FacebookDoc19") is True


@pytest.mark.parametrize("status_", ["pending", "queued", "running", "cancelling"])
def test_a_publish_on_the_same_profile_refuses_before_the_browser(monkeypatch, status_):
    monkeypatch.setattr(sp, "turn_off", lambda *a, **k: pytest.fail("the browser was touched"))
    _job("FacebookDoc19", status_)
    status, body = _post(BY_NAME)
    assert status == 409 and body["busy"] is True
    assert body["error"] == sp.PROFILE_BUSY_ERROR
    record = sp.turn_off_history(account="FacebookDoc19")[0]
    assert record["error"] == sp.PROFILE_BUSY_ERROR and record["matched_by"] == "name"


def test_a_publish_on_another_profile_does_not_block(monkeypatch):
    monkeypatch.setattr(sp, "turn_off", lambda *a, **k: sp.TurnOffResult(
        account="FacebookDoc19", level="campaign", object_id="", state="off"))
    _job("FacebookDoc21", "running")
    _job("FacebookDoc19", "done", finished="2026-01-01T00:00:00+00:00")
    status, body = _post(BY_NAME)
    assert status == 200 and body["result"]["state"] == "off"


def test_a_publish_that_finished_while_waiting_still_counts():
    _job("FacebookDoc19", "done", finished="2026-09-29T10:00:05+00:00")
    assert publish_jobs.profile_in_use("profile-FacebookDoc19", since="2026-09-29T10:00:00+00:00")
    assert not publish_jobs.profile_in_use("profile-FacebookDoc19", since="2026-09-29T10:00:06+00:00")


@pytest.mark.parametrize("fields", [
    dict(BY_NAME, match=""),                 # a lost id is never a silent name search
    dict(BY_NAME, level="ad", parent_id="1"),
    dict(BY_NAME, name=""),
])
def test_incomplete_name_requests_are_rejected(monkeypatch, fields):
    monkeypatch.setattr(sp, "turn_off", lambda *a, **k: pytest.fail("the browser was touched"))
    status, body = _post(fields)
    assert status == 400 and body["ok"] is False
