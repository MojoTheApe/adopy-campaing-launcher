"""Two Multilogin slots must never be the same workspace, and can be logged out.

A buyer typed the KLD login while the Inhouse slot was selected: both slots then
pointed at KLD and the page still said "Connected (Inhouse)". Only fakes here —
no Multilogin, no network; tokens are JWTs built in the test.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import secrets
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.web import mlx_slots  # noqa: E402
from src.web.app import app  # noqa: E402

LABELS = {"A": "Inhouse", "B": "KLD"}
KEYS = [f"MULTILOGIN_{k}_{s}" for s in "AB" for k in ("AUTOMATION_TOKEN", "WORKSPACE", "WORKSPACE_ID")]


def _token(workspace: str | None) -> str:
    def part(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    claims = {"nonce": secrets.token_hex(6)}
    if workspace:
        claims["workspaceID"] = workspace
    return f"{part({'alg': 'none'})}.{part(claims)}.fake{secrets.token_hex(8)}"


@pytest.fixture
def machine(monkeypatch):
    """Clean slot env + a recorder standing in for the machine .env."""
    for k in KEYS:
        monkeypatch.delenv(k, raising=False)
    written: list[dict] = []
    monkeypatch.setattr("config.paths.update_machine_env", lambda values: written.append(values) or "C:/fake/.env")
    return written


def _both(monkeypatch, a_ws: str, b_ws: str, name: str = "") -> None:
    monkeypatch.setenv("MULTILOGIN_AUTOMATION_TOKEN_A", _token(a_ws))
    monkeypatch.setenv("MULTILOGIN_AUTOMATION_TOKEN_B", _token(b_ws))
    if name:
        monkeypatch.setenv("MULTILOGIN_WORKSPACE_A", name)
        monkeypatch.setenv("MULTILOGIN_WORKSPACE_B", name)


def test_the_same_workspace_in_both_slots_is_found_from_the_tokens(machine, monkeypatch):
    _both(monkeypatch, "ws-kld", "ws-kld")
    assert mlx_slots.duplicate()["workspace_id"] == "ws-kld"


def test_two_different_workspaces_are_left_alone(machine, monkeypatch):
    _both(monkeypatch, "ws-inhouse", "ws-kld")
    assert mlx_slots.duplicate() is None
    assert mlx_slots.repair(LABELS) == {"repaired": False, "cleared": [], "kept": "", "message": ""}
    assert machine == []


def test_an_unreadable_token_is_never_reported_as_a_duplicate(machine, monkeypatch):
    # No workspace in either token and nothing remembered: nothing to compare.
    _both(monkeypatch, "", "")
    assert mlx_slots.duplicate() is None
    monkeypatch.setenv("MULTILOGIN_AUTOMATION_TOKEN_A", "opaque-token")
    monkeypatch.setenv("MULTILOGIN_AUTOMATION_TOKEN_B", "opaque-token-2")
    assert mlx_slots.repair(LABELS)["repaired"] is False


def test_a_single_connected_slot_is_not_a_conflict(machine, monkeypatch):
    monkeypatch.setenv("MULTILOGIN_AUTOMATION_TOKEN_B", _token("ws-kld"))
    assert mlx_slots.duplicate() is None


def test_repair_keeps_the_slot_the_workspace_name_belongs_to(machine, monkeypatch):
    _both(monkeypatch, "ws-kld", "ws-kld", name="KLD Media")
    out = mlx_slots.repair(LABELS)
    assert out["repaired"] and out["kept"] == "B" and out["cleared"] == ["A"]
    assert "Inhouse" in out["message"] and "disconnected" in out["message"]
    # Only the wrong slot's three keys were blanked.
    assert machine == [{"MULTILOGIN_AUTOMATION_TOKEN_A": "", "MULTILOGIN_WORKSPACE_A": "",
                        "MULTILOGIN_WORKSPACE_ID_A": ""}]


def test_repair_disconnects_both_when_the_name_settles_nothing(machine, monkeypatch):
    _both(monkeypatch, "ws-x", "ws-x", name="Some Workspace")
    out = mlx_slots.repair(LABELS)
    assert out["repaired"] and out["kept"] == "" and out["cleared"] == ["A", "B"]
    assert len(machine) == 2
    assert "unclear" in out["message"]


def test_remembered_id_is_used_when_the_token_does_not_carry_one(machine, monkeypatch):
    monkeypatch.setenv("MULTILOGIN_AUTOMATION_TOKEN_A", "opaque-1")
    monkeypatch.setenv("MULTILOGIN_AUTOMATION_TOKEN_B", "opaque-2")
    monkeypatch.setenv("MULTILOGIN_WORKSPACE_ID_A", "WS-KLD")
    monkeypatch.setenv("MULTILOGIN_WORKSPACE_ID_B", "ws-kld")
    assert mlx_slots.duplicate()["workspace_id"] == "ws-kld"


def test_disconnect_blanks_the_slot_and_the_process_environment(machine, monkeypatch):
    _both(monkeypatch, "ws-inhouse", "ws-kld", name="x")
    mlx_slots.disconnect("a")
    assert not mlx_slots.has_token("A") and mlx_slots.has_token("B")
    assert os.environ.get("MULTILOGIN_WORKSPACE_A") is None
    with pytest.raises(ValueError):
        mlx_slots.disconnect("C")


# ── HTTP ────────────────────────────────────────────────────────────────────

def _call(method: str, path: str, body: dict | None = None):
    async def run():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            r = await client.request(method, path, content=json.dumps(body or {}),
                                     headers={"Content-Type": "application/json"})
            return r.status_code, r.json()

    return asyncio.run(run())


def test_repair_endpoint_fixes_the_state_and_says_what_it_did(machine, monkeypatch):
    _both(monkeypatch, "ws-kld", "ws-kld", name="KLD")
    code, body = _call("POST", "/multilogin-slots-repair")
    assert code == 200 and body["ok"] and body["repaired"] and body["cleared"] == ["A"]
    # Idempotent: the page may call it on every load.
    code, again = _call("POST", "/multilogin-slots-repair")
    assert again["repaired"] is False


def test_logout_endpoint_disconnects_one_slot(machine, monkeypatch):
    _both(monkeypatch, "ws-inhouse", "ws-kld")
    code, body = _call("POST", "/multilogin-logout", {"instance": "A"})
    assert code == 200 and body == {"ok": True, "instance": "A"}
    assert not mlx_slots.has_token("A") and mlx_slots.has_token("B")


def test_logout_needs_a_slot(machine):
    code, body = _call("POST", "/multilogin-logout", {})
    assert code == 400 and not body["ok"]


def test_logout_waits_for_running_jobs(machine, monkeypatch):
    _both(monkeypatch, "ws-inhouse", "ws-kld")
    monkeypatch.setattr("src.web.publish_jobs.active_job_count", lambda: 2)
    code, body = _call("POST", "/multilogin-logout", {"instance": "B"})
    assert code == 409 and "2 campaign job" in body["error"]
    assert mlx_slots.has_token("B")


def test_a_pasted_token_for_the_other_slots_workspace_is_refused(machine, monkeypatch):
    monkeypatch.setenv("MULTILOGIN_AUTOMATION_TOKEN_B", _token("ws-kld"))
    code, body = _call("POST", "/multilogin-save-token", {"token": _token("ws-kld"), "instance": "A"})
    assert not body["ok"] and "KLD" in body["error"] and "nothing was saved" in body["error"]
    assert machine == []


def test_a_pasted_token_for_a_new_workspace_is_saved(machine, monkeypatch):
    monkeypatch.setattr("src.web.app.update_machine_env", lambda v: machine.append(v) or "C:/fake/.env")
    monkeypatch.setenv("MULTILOGIN_AUTOMATION_TOKEN_B", _token("ws-kld"))
    code, body = _call("POST", "/multilogin-save-token", {"token": _token("ws-inhouse"), "instance": "A"})
    assert body["ok"] and len(machine) == 1
