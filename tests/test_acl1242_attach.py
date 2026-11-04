"""ACL-1242: вошедший менеджер на событиях, правках библиотеки и в маршрутах. Офлайн."""
from __future__ import annotations

import importlib.util
import json
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import acl_sentry  # noqa: E402
from src.acl_event_log import writer  # noqa: E402
from src.analysis_agent import google_identity as gi  # noqa: E402
from src.analysis_agent import team_library as tl  # noqa: E402

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
GOOD = {"email": "alice@adopy.com", "email_verified": True, "hd": "adopy.com", "name": "Alice Example"}


class _Creds:
    id_token = "fake.id.token"

    def to_json(self):
        return json.dumps({"refresh_token": "1//identity-refresh-token-value", "client_id": "cid", "client_secret": "csecret-1"})


@pytest.fixture(autouse=True)
def machine(tmp_path, monkeypatch):
    config = tmp_path / "ACL"
    config.mkdir()
    monkeypatch.setenv("ACL_CONFIG_DIR", str(config))
    monkeypatch.delenv("GSHEET_OAUTH_CLIENT", raising=False)
    monkeypatch.delenv("ACL_MACHINE_LABEL", raising=False)
    monkeypatch.setenv("MY_CM_NAME", "")
    (config / "oauth_client.json").write_text(
        json.dumps({"installed": {"client_id": "cid.apps.example"}}), encoding="utf-8")

    def no_network(*args, **kwargs):
        raise AssertionError("the test touched the network")

    monkeypatch.setattr(socket, "getaddrinfo", no_network)
    gi.reset_cache()
    writer.reset_identity()
    yield config
    gi.reset_cache()
    writer.reset_identity()


def _sign_in(cm_name="Alice", claims=GOOD):
    result = gi.sign_in(runner=lambda **kw: _Creds(), verifier=lambda t, c: dict(claims),
                        clock=lambda: NOW, names=lambda: None)
    assert result["ok"], result
    if cm_name:
        gi.set_name(cm_name)
    return result


# ── Sentry ────────────────────────────────────────────────────────────────────

def _tags(event):
    tags = event["tags"]
    return dict(tags) if isinstance(tags, dict) else {k: v for k, v in tags}


def test_sentry_prefers_the_bound_cm_name_and_tags_the_pseudonym(machine, monkeypatch):
    monkeypatch.setenv("MY_CM_NAME", "typed by hand")
    _sign_in("Alice")
    event = acl_sentry._before_send({"tags": {}, "message": "Failed for alice@adopy.com"}, {})
    tags = _tags(event)
    assert tags["manager"] == "Alice" and tags["identity"] == "verified"
    assert tags["identity_id"] == gi.hash_email("alice@adopy.com") and len(tags["identity_id"]) == 12


def test_identity_adds_no_email_to_an_event(machine):
    """Почту мы не кладём никуда: ни в теги, ни в контекст. (Свободный текст события,
    куда почту мог бы вписать сам код, `_scrub_deep` по значениям не чистит - так было до ACL-1242.)"""
    _sign_in("Alice")
    event = {"tags": {}, "message": "Failed", "contexts": {"report": {"reporter": "Alice", "identity": "verified"}}}
    sent = acl_sentry._before_send(event, {})
    dumped = json.dumps(sent)
    assert "@" not in dumped and "adopy.com" not in dumped
    assert _tags(sent)["identity_id"]


def test_without_sign_in_sentry_works_as_before_and_says_so(machine, monkeypatch):
    monkeypatch.setenv("MY_CM_NAME", "Bob")
    sent = acl_sentry._before_send({"tags": {}}, {})
    tags = _tags(sent)
    assert tags["manager"] == "Bob" and tags["identity"] == "none" and "identity_id" not in tags


def test_declined_is_visible_in_sentry(machine):
    gi.decline()
    assert _tags(acl_sentry._before_send({"tags": {}}, {}))["identity"] == "declined"


def test_expired_identity_still_names_the_manager_but_says_expired(machine):
    _sign_in("Alice")
    gi.recheck(refresher=lambda c: {**GOOD, "hd": "x.com"}, force=True)
    tags = _tags(acl_sentry._before_send({"tags": {}}, {}))
    assert tags["manager"] == "Alice" and tags["identity"] == "expired"


def test_a_broken_identity_file_never_blocks_the_event(machine):
    (machine / "identity.json").write_text("{broken", encoding="utf-8")
    gi.reset_cache()
    assert _tags(acl_sentry._before_send({"tags": {}}, {}))["identity"] in ("none", "unavailable")


# ── журнал действий ───────────────────────────────────────────────────────────

def _lines(folder):
    return [json.loads(x) for f in sorted(folder.glob("*.ndjson")) for x in f.read_text("utf-8").splitlines()]


def test_reset_identity_updates_a_running_stream(tmp_path):
    writer.use_outbox_for_tests(tmp_path / "box")
    try:
        writer.emit("job.created")
        _sign_in("Alice")  # sign_in зовёт reset_identity и пишет свои события
        writer.emit("job.started")
        lines = _lines(tmp_path / "box")
    finally:
        writer.use_outbox_for_tests(None)
    first = lines[0]
    last = [x for x in lines if x["type"] == "job.started"][0]
    assert first["identity"] in ("none", "unavailable") and first["email"] == ""
    assert last["identity"] == "verified" and last["email"] == "alice@adopy.com"
    assert last["identity_id"] == gi.hash_email("alice@adopy.com") and last["manager"] == "Alice"


def test_identity_events_carry_no_email_or_token_in_the_payload(tmp_path):
    writer.use_outbox_for_tests(tmp_path / "box")
    try:
        _sign_in("Alice")
        gi.sign_out()
        lines = _lines(tmp_path / "box")
    finally:
        writer.use_outbox_for_tests(None)
    kinds = [x["type"] for x in lines]
    assert "identity.signed_in" in kinds and "identity.signed_out" in kinds
    for line in lines:
        payload = json.dumps(line["payload"])
        assert "@" not in payload and "refresh" not in payload and "token" not in payload.lower()


# ── библиотека ────────────────────────────────────────────────────────────────

def test_author_is_the_signed_in_manager_even_when_viewing_someone_else(machine, monkeypatch):
    from src.web import buyer

    _sign_in("Alice")
    monkeypatch.setattr(buyer, "get_selected_cm", lambda: "Bob")
    assert tl.author() == "Alice"


def test_author_without_sign_in_matches_today(machine, monkeypatch):
    from src.web import buyer

    monkeypatch.setattr(buyer, "get_selected_cm", lambda: "Bob")
    assert tl.author() == "Bob"


# ── пакет диагностики ─────────────────────────────────────────────────────────

def test_support_bundle_denylist_knows_the_identity_files(machine):
    _sign_in("Alice")
    spec = importlib.util.spec_from_file_location(
        "acl_diag_1242", ROOT / "packaging" / "windows" / "diagnostics.py")
    diagnostics = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(diagnostics)
    values = diagnostics.local_values_to_redact(machine)
    cleaned = diagnostics.sanitize_text(
        "mail alice@adopy.com refresh 1//identity-refresh-token-value secret csecret-1", values)
    assert "alice@adopy.com" not in cleaned and "1//identity-refresh-token-value" not in cleaned


# ── маршруты ──────────────────────────────────────────────────────────────────

@pytest.fixture
def client(monkeypatch):
    from fastapi.testclient import TestClient
    from src.web import app as web_app

    monkeypatch.setattr(gi, "_default_runner", lambda **kw: _Creds())
    monkeypatch.setattr(gi, "_default_verifier", lambda t, c: dict(GOOD))
    monkeypatch.setattr(gi, "known_cm_names", lambda: ["Alice", "Bob"])
    return TestClient(web_app.app, base_url="http://127.0.0.1")


def test_get_identity_before_sign_in_is_none_and_has_no_secrets(client):
    r = client.get("/api/identity")
    assert r.status_code == 200 and r.json()["state"] == "none" and r.json()["ok"]
    assert "token" not in r.text.lower()


def test_sign_in_name_and_sign_out_round_trip(client):
    r = client.post("/api/identity/sign-in")
    body = r.json()
    assert r.status_code == 200 and body["ok"] and body["name_step"] == "confirm"
    r = client.post("/api/identity/name", json={"name": "alice"})
    assert r.status_code == 200 and r.json()["identity"]["cm_name"] == "alice" and r.json()["identity"]["cm_checked"]
    assert client.get("/api/identity").json()["email"] == "alice@adopy.com"
    r = client.post("/api/identity/sign-out")
    assert r.json()["identity"]["state"] == "none"


def test_unlisted_name_needs_confirmation(client):
    client.post("/api/identity/sign-in")
    r = client.post("/api/identity/name", json={"name": "Alise"})
    assert r.status_code == 409 and r.json()["suggestions"] == ["Alice"]
    assert r.json()["message"] == '"Alise" is not in the campaign manager list. Did you mean: Alice?'
    r = client.post("/api/identity/name", json={"name": "Alise", "confirm_unlisted": True})
    assert r.status_code == 200 and r.json()["identity"]["cm_checked"] is False


def test_name_before_sign_in_is_refused(client):
    r = client.post("/api/identity/name", json={"name": "Alice"})
    assert r.status_code == 400 and r.json()["message"] == "Sign in first."


def test_wrong_domain_answers_400_with_the_fixed_text(client, monkeypatch):
    monkeypatch.setattr(gi, "_default_verifier", lambda t, c: {**GOOD, "hd": "other.com"})
    r = client.post("/api/identity/sign-in")
    assert r.status_code == 400 and r.json()["reason"] == "wrong_domain"


@pytest.mark.parametrize("path", ["sign-in", "name", "sign-out", "decline"])
def test_cross_site_posts_are_blocked(client, path):
    r = client.post(f"/api/identity/{path}", json={"name": "x"},
                    headers={"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403
    assert client.get("/api/identity").json()["state"] == "none"


def test_other_routes_do_not_depend_on_the_identity_state(client):
    assert client.get("/api/identity").json()["state"] == "none"
    # метка, а не замок: маршрут соседней функции отвечает так же, как без входа
    r = client.get("/api/connections")
    assert r.status_code == 200


def test_problem_report_context_names_the_reporter(client, monkeypatch):
    seen = {}

    def fake_report(text, context=None):
        seen.update(context or {})
        return "evt1"

    monkeypatch.setattr(acl_sentry, "report_problem", fake_report)
    client.post("/api/identity/sign-in")
    client.post("/api/identity/name", json={"name": "Alice"})
    r = client.post("/report-problem", data={"text": "it broke"})
    assert r.status_code == 202
    assert seen["reporter"] == "Alice" and seen["identity"] == "verified"
    assert "adopy.com" not in json.dumps(seen)
