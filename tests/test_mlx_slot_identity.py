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
    recorder = lambda values: written.append(values) or "C:/fake/.env"  # noqa: E731
    monkeypatch.setattr("config.paths.update_machine_env", recorder)
    monkeypatch.setattr("src.web.app.update_machine_env", recorder)
    return written


def _both(monkeypatch, a_ws: str, b_ws: str, name: str = "") -> None:
    monkeypatch.setenv("MULTILOGIN_AUTOMATION_TOKEN_A", _token(a_ws))
    monkeypatch.setenv("MULTILOGIN_AUTOMATION_TOKEN_B", _token(b_ws))
    if name:
        # A name is trusted only next to an id equal to the token's claim.
        monkeypatch.setenv("MULTILOGIN_WORKSPACE_A", name)
        monkeypatch.setenv("MULTILOGIN_WORKSPACE_B", name)
        monkeypatch.setenv("MULTILOGIN_WORKSPACE_ID_A", a_ws)
        monkeypatch.setenv("MULTILOGIN_WORKSPACE_ID_B", b_ws)


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


def test_repair_ignores_the_remembered_id_fallback(machine, monkeypatch):
    # Opaque tokens + equal remembered ids: good enough for the save guard,
    # never enough to wipe a connection.
    monkeypatch.setenv("MULTILOGIN_AUTOMATION_TOKEN_A", "opaque-1")
    monkeypatch.setenv("MULTILOGIN_AUTOMATION_TOKEN_B", "opaque-2")
    monkeypatch.setenv("MULTILOGIN_WORKSPACE_ID_A", "ws-kld")
    monkeypatch.setenv("MULTILOGIN_WORKSPACE_ID_B", "ws-kld")
    assert mlx_slots.duplicate() is not None
    assert mlx_slots.repair(LABELS)["repaired"] is False
    assert machine == []


def test_a_stale_name_disconnects_both_slots(machine, monkeypatch):
    # Slot B's token is the KLD one, but the name "KLD Media" was written by an
    # older sign-in (its id is not the token's workspace): not trusted.
    _both(monkeypatch, "ws-kld", "ws-kld")
    monkeypatch.setenv("MULTILOGIN_WORKSPACE_B", "KLD Media")
    monkeypatch.setenv("MULTILOGIN_WORKSPACE_ID_B", "ws-older")
    out = mlx_slots.repair(LABELS)
    assert out["repaired"] and out["kept"] == "" and out["cleared"] == ["A", "B"]


def _jwt_with_payload(raw: bytes) -> str:
    return "a." + base64.urlsafe_b64encode(raw).decode().rstrip("=") + ".c"


@pytest.mark.parametrize("bad", [
    "a.!!!notbase64.c",
    "a.\u00e9\u00e9.c",
    _jwt_with_payload(b'["workspaceID", "x"]'),
    "only-one-part",
    "x",
])
def test_malformed_tokens_never_make_a_duplicate(machine, monkeypatch, bad):
    monkeypatch.setenv("MULTILOGIN_AUTOMATION_TOKEN_A", bad)
    monkeypatch.setenv("MULTILOGIN_AUTOMATION_TOKEN_B", bad)
    assert mlx_slots.token_workspace(bad) == ""
    assert mlx_slots.duplicate(claims_only=True) is None
    assert mlx_slots.duplicate() is None
    assert mlx_slots.repair(LABELS)["repaired"] is False


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


def test_repair_waits_while_work_is_running(machine, monkeypatch):
    _both(monkeypatch, "ws-kld", "ws-kld", name="KLD")
    monkeypatch.setattr("src.web.publish_jobs.publishing_busy", lambda: True)
    code, body = _call("POST", "/multilogin-slots-repair")
    assert code == 200 and body["ok"] and body["repaired"] is False and body["deferred"] is True
    assert "fixed when the running jobs finish" in body["message"]
    assert mlx_slots.has_token("A") and mlx_slots.has_token("B") and machine == []


def test_repair_with_busy_work_but_no_conflict_is_a_plain_no_op(machine, monkeypatch):
    _both(monkeypatch, "ws-inhouse", "ws-kld")
    monkeypatch.setattr("src.web.publish_jobs.publishing_busy", lambda: True)
    _, body = _call("POST", "/multilogin-slots-repair")
    assert body["repaired"] is False and not body.get("deferred")


def test_logout_is_refused_while_a_profile_slot_is_busy(machine, monkeypatch):
    _both(monkeypatch, "ws-inhouse", "ws-kld")
    monkeypatch.setattr("src.web.publish_jobs.active_job_count", lambda: 0)
    monkeypatch.setattr("src.web.publish_jobs.publishing_busy", lambda: True)
    code, body = _call("POST", "/multilogin-logout", {"instance": "A"})
    assert code == 409 and not body["ok"]
    assert mlx_slots.has_token("A")


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


def test_save_token_writes_the_id_from_the_claim_and_drops_a_stale_name(machine, monkeypatch):
    monkeypatch.setenv("MULTILOGIN_WORKSPACE_A", "Old Name")
    monkeypatch.setenv("MULTILOGIN_WORKSPACE_ID_A", "ws-old")
    token = _token("WS-New")
    _, body = _call("POST", "/multilogin-save-token", {"token": token, "instance": "A"})
    assert body["ok"]
    assert machine == [{"MULTILOGIN_AUTOMATION_TOKEN_A": token, "MULTILOGIN_WORKSPACE_ID_A": "ws-new",
                        "MULTILOGIN_WORKSPACE_A": ""}]


def test_save_token_without_a_claim_blanks_name_and_id(machine, monkeypatch):
    monkeypatch.setenv("MULTILOGIN_WORKSPACE_A", "Old Name")
    monkeypatch.setenv("MULTILOGIN_WORKSPACE_ID_A", "ws-old")
    _, body = _call("POST", "/multilogin-save-token", {"token": "opaque", "instance": "A"})
    assert body["ok"]
    assert machine[0]["MULTILOGIN_WORKSPACE_ID_A"] == "" and machine[0]["MULTILOGIN_WORKSPACE_A"] == ""


class _Signed:
    ok = True
    needs_choice = False
    token_error = ""
    workspaces: list = []
    bearer = "bearer-secret-value"
    workspace_name = "KLD Media"

    def __init__(self, token: str, workspace_id: str):
        self.automation_token = token
        self.workspace_id = workspace_id


def test_signin_for_the_other_slots_workspace_is_refused(machine, monkeypatch):
    monkeypatch.setenv("MULTILOGIN_AUTOMATION_TOKEN_B", _token("ws-kld"))
    signed = _Signed(_token("ws-kld"), "ws-kld")
    monkeypatch.setattr("src.web.multilogin_auth.sign_in", lambda *a, **k: signed)
    code, body = _call("POST", "/multilogin-signin",
                       {"email": "a@example.com", "password": "pw-not-real", "instance": "A"})
    assert not body["ok"] and "nothing was saved" in body["error"] and "KLD" in body["error"]
    assert machine == []


def test_no_token_value_appears_in_any_response(machine, monkeypatch):
    ta, tb = _token("ws-kld"), _token("ws-kld")
    pasted = _token("ws-kld")
    seen = []
    monkeypatch.setenv("MULTILOGIN_AUTOMATION_TOKEN_A", ta)
    monkeypatch.setenv("MULTILOGIN_AUTOMATION_TOKEN_B", tb)
    seen.append(json.dumps(_call("POST", "/multilogin-save-token", {"token": pasted, "instance": "A"})[1]))
    seen.append(json.dumps(_call("POST", "/multilogin-slots-repair", {})[1]))
    monkeypatch.setenv("MULTILOGIN_AUTOMATION_TOKEN_A", ta)
    monkeypatch.setenv("MULTILOGIN_AUTOMATION_TOKEN_B", tb)
    seen.append(json.dumps(_call("POST", "/multilogin-logout", {"instance": "B"})[1]))
    monkeypatch.setenv("MULTILOGIN_AUTOMATION_TOKEN_A", ta)
    monkeypatch.setenv("MULTILOGIN_AUTOMATION_TOKEN_B", tb)
    monkeypatch.setattr("src.web.publish_jobs.publishing_busy", lambda: True)
    seen.append(json.dumps(_call("POST", "/multilogin-slots-repair", {})[1]))
    seen.append(json.dumps(_call("POST", "/multilogin-logout", {"instance": "A"})[1]))
    for token in (ta, tb, pasted):
        for text in seen:
            assert token not in text and token.split(".")[2] not in text


def test_repair_and_logout_wait_for_warm_ups_too(machine, monkeypatch):
    _both(monkeypatch, "ws-kld", "ws-kld", name="KLD")
    monkeypatch.setattr("src.web.publish_jobs.active_job_count", lambda: 0)
    monkeypatch.setattr("src.web.publish_jobs.publishing_busy", lambda: False)
    monkeypatch.setattr("src.web.warm_api.running_job_count", lambda: 1)
    _, body = _call("POST", "/multilogin-slots-repair")
    assert body["deferred"] is True and machine == []
    code, out = _call("POST", "/multilogin-logout", {"instance": "A"})
    assert code == 409 and machine == []


def test_automatic_warm_ups_count_as_running_work(monkeypatch):
    from src.web import warm_api

    monkeypatch.setattr(warm_api, "_AUTO_QUEUE", [])
    monkeypatch.setattr(warm_api, "_JOBS", {})
    monkeypatch.setattr(warm_api, "_AUTO_RUNNING", [0])
    assert warm_api.running_job_count() == 0
    warm_api._AUTO_QUEUE.append("UL1")
    assert warm_api.running_job_count() == 1
    warm_api._AUTO_QUEUE.clear()
    warm_api._AUTO_RUNNING[0] = 1
    assert warm_api.running_job_count() == 1


def test_a_deferred_note_is_retried_from_the_60s_tick_and_cleared_one_slot_is_not_enough():
    html = (ROOT / "src" / "web" / "templates" / "report.html").read_text(encoding="utf-8")
    tick = html[html.index("function aclMlxExpiryRefresh()"):]
    tick = tick[:tick.index("function aclMlxExpiryBoot")]
    assert "AMAP_ML_NOTE_WARN" in tick and "amapMlRepair()" in tick
    assert "setInterval(aclMlxExpiryRefresh, 60000)" in html
    # Sign-in clears the note only through the both-slots check.
    assert "amapMlNoteClearIfConnected();" in html
    assert "['A', 'B'].every(" in html
