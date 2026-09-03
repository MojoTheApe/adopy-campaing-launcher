"""Signing in to Multilogin is not the same as being allowed to automate it.

Only the workspace owner may mint an automation token. A team member's correct
password signs in fine and then gets nothing — and without that token the RPA
cannot start a single profile.

The panel used to report both outcomes as a green "connected", differing only by
a trailing clause. So the operator saw success, pressed Publish, and got
"MULTILOGIN_AUTOMATION_TOKEN_A is not set in .env" — a message pointing at a
file they had just been told was written. Found on a second campaign manager's
first supervised run, 2026-09-03.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from src.web.app import app

_ROOT = Path(__file__).resolve().parents[1]


class _Response:
    def __init__(self, status_code: int, payload=None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text
        self.headers = {"content-type": "application/json"}

    def json(self):
        return self._payload


def _signin(monkeypatch, *, token_response: _Response, saved: list) -> dict:
    """Drive /multilogin-signin with the Multilogin API stubbed out."""
    import requests

    def fake_post(url, **kwargs):
        if url.endswith("/user/signin"):
            return _Response(200, {"data": {"token": "bearer-abc"}})
        return _Response(200, {"data": {"profiles": []}})

    def fake_get(url, **kwargs):
        if "automation_token" in url:
            return token_response
        return _Response(200, {"data": {}})

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(
        "src.web.app.update_machine_env",
        lambda values: saved.append(values) or "C:/fake/.env",
    )

    async def _run():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            response = await client.post(
                "/multilogin-signin",
                content=json.dumps({"email": "a@b.co", "password": "x", "save_token": True}),
                headers={"Content-Type": "application/json"},
            )
            return response.json()

    return asyncio.run(_run())


def test_signin_without_an_automation_token_does_not_report_a_saved_token(monkeypatch):
    saved: list = []
    body = _signin(
        monkeypatch,
        token_response=_Response(403, {"status": {"message": "only workspace owner"}}),
        saved=saved,
    )

    assert body["token_saved"] is False
    assert not saved, "nothing may be written when there is no token to write"
    assert "403" in body["token_error"]
    assert "only workspace owner" in body["token_error"]


def test_signin_with_a_token_saves_it_and_reports_no_problem(monkeypatch):
    saved: list = []
    body = _signin(
        monkeypatch,
        token_response=_Response(200, {"data": {"token": "automation-xyz"}}),
        saved=saved,
    )

    assert body["token_saved"] is True
    assert body["token_error"] == ""
    assert saved == [{"MULTILOGIN_AUTOMATION_TOKEN_A": "automation-xyz"}]


def test_the_panel_does_not_show_success_when_no_token_was_saved():
    """A green tick differing only by a trailing clause is not a warning."""
    template = (_ROOT / "src" / "web" / "templates" / "report.html").read_text(encoding="utf-8")

    start = template.index("function mlSignin(cid)")
    body = template[start:start + 3000]
    branch = body[body.index("if (data.token_saved)"):]

    assert "var(--warn)" in branch
    assert "NO automation token" in branch
    # The operator has to be told where to get one, or the warning is a dead end.
    assert "Generate Token" in branch
