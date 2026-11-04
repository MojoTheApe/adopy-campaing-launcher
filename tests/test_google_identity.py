"""ACL-1242: вход рабочим аккаунтом Google. Только офлайн, без настоящего Google.

Запуск OAuth, проверка id_token, обновление токена и часы подменены; любой
сокет в тесте — ошибка. Почты фиктивные (alice@adopy.com).
"""
from __future__ import annotations

import json
import socket
import threading
from datetime import datetime, timedelta, timezone

import pytest

from config import paths, token_vault
from src.analysis_agent import google_identity as gi

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
GOOD = {"email": "alice@adopy.com", "email_verified": True, "hd": "adopy.com", "name": "Alice Example"}
SHEET_TOKEN = b'{"refresh_token": "1//sheet-read-token-value"}'
TELEMETRY_TOKEN = b'{"refresh_token": "1//sheet-write-token-value"}'


class FakeCreds:
    id_token = "fake.id.token"
    refresh_token = "1//identity-refresh-token-value"

    def to_json(self):
        return json.dumps({"refresh_token": self.refresh_token, "client_id": "cid.apps.example",
                           "client_secret": "csecret-value-1", "token": "access-token-value-1"})


@pytest.fixture(autouse=True)
def machine(tmp_path, monkeypatch):
    config = tmp_path / "ACL"
    config.mkdir()
    monkeypatch.setenv("ACL_CONFIG_DIR", str(config))
    monkeypatch.delenv("GSHEET_OAUTH_CLIENT", raising=False)
    monkeypatch.setenv("MY_CM_NAME", "")
    (config / "oauth_client.json").write_text(
        json.dumps({"installed": {"client_id": "cid.apps.example", "client_secret": "s"}}), encoding="utf-8")
    (config / "token_accounts.json").write_bytes(SHEET_TOKEN)
    (config / "token_telemetry.json").write_bytes(TELEMETRY_TOKEN)

    def no_network(*args, **kwargs):
        raise AssertionError("the test touched the network")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "getaddrinfo", no_network)
    gi.reset_cache()
    yield config
    gi.reset_cache()


@pytest.fixture
def events(monkeypatch):
    seen: list[tuple] = []
    from src.acl_event_log import writer

    monkeypatch.setattr(writer, "emit", lambda kind, payload=None, **kw: seen.append((kind, payload)))
    return seen


def _runner(calls=None):
    def run(**kwargs):
        if calls is not None:
            calls.append(kwargs)
        return FakeCreds()

    return run


def _sign_in(claims=GOOD, names=lambda: None, calls=None, clock=lambda: NOW):
    return gi.sign_in(runner=_runner(calls), verifier=lambda token, cid: dict(claims),
                      clock=clock, names=names)


def _files(config):
    return {p.name for p in config.iterdir()}


# ── вход ──────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("claims", [
    GOOD,
    {**GOOD, "email": "Alice@AdoPy.com", "hd": "AdoPy.COM"},
])
def test_adopy_account_is_accepted_case_insensitively(machine, claims):
    result = _sign_in(claims)
    assert result["ok"] and result["state"] == "verified"
    who = gi.current()
    assert who.state == "verified" and who.email == "alice@adopy.com" and who.name == "Alice Example"
    assert {"token_identity.json", "identity.json"} <= _files(machine)


@pytest.mark.parametrize("claims", [
    {k: v for k, v in GOOD.items() if k != "hd"},
    {**GOOD, "hd": "other.com"},
    {**GOOD, "email_verified": False},
    {**GOOD, "email_verified": "true"},
    {**GOOD, "email": "alice@gmail.com"},
    {**GOOD, "email": "alice@adopy.com.evil.example"},
    {**GOOD, "email": "a@b@adopy.com"},
])
def test_other_accounts_are_rejected_and_nothing_is_stored(machine, claims, events):
    result = _sign_in(claims)
    assert result["ok"] is False and result["reason"] == "wrong_domain"
    assert result["message"] == "That is not an Adopy Google account. Sign in with your @adopy.com account."
    assert not ({"token_identity.json", "identity.json"} & _files(machine))
    assert events == [("identity.rejected", {"state": "none", "reason": "wrong_domain"})]


def test_a_verifier_that_raises_stores_nothing(machine):
    def broken(token, client_id):
        raise ValueError("bad signature; body from google must not be shown")

    result = gi.sign_in(runner=_runner(), verifier=broken, clock=lambda: NOW)
    assert result["ok"] is False and result["reason"] == "failed"
    assert "google" not in result["message"].lower() or "Could not sign in" in result["message"]
    assert not ({"token_identity.json", "identity.json"} & _files(machine))


def test_flow_gets_hint_prompt_scopes_and_timeout(machine):
    calls: list[dict] = []
    _sign_in(calls=calls)
    (call,) = calls
    assert call["hd"] == "adopy.com" and call["prompt"] == "select_account consent"
    assert call["timeout_seconds"] == 180
    assert call["scopes"] == ["openid", "https://www.googleapis.com/auth/userinfo.email",
                              "https://www.googleapis.com/auth/userinfo.profile"]
    assert call["client_path"].endswith("oauth_client.json")


def test_verifier_receives_the_client_id_as_audience(machine):
    seen = []
    gi.sign_in(runner=_runner(), verifier=lambda t, cid: seen.append((t, cid)) or dict(GOOD), clock=lambda: NOW)
    assert seen == [("fake.id.token", "cid.apps.example")]


@pytest.mark.parametrize("error, reason", [
    (ConnectionError("down"), "offline"),
    (TimeoutError("slow"), "offline"),
    (RuntimeError("boom"), "failed"),
])
def test_failures_have_fixed_texts(machine, error, reason):
    def run(**kwargs):
        raise error

    result = gi.sign_in(runner=run, verifier=lambda *a: GOOD, clock=lambda: NOW)
    assert result["reason"] == reason and result["message"] == gi.MESSAGES[reason]
    assert "boom" not in result["message"] and "down" not in result["message"]


def test_browser_timeout_is_a_try_again_message(machine):
    from google_auth_oauthlib.flow import WSGITimeoutError

    def run(**kwargs):
        raise WSGITimeoutError("timed out")

    result = gi.sign_in(runner=run, verifier=lambda *a: GOOD)
    assert result["reason"] == "timeout" and result["message"] == "Sign-in was not finished. Try again."


def test_no_client_in_this_build_is_unavailable(machine, tmp_path, monkeypatch):
    monkeypatch.setenv("GSHEET_OAUTH_CLIENT", str(tmp_path / "missing.json"))
    gi.reset_cache()
    assert gi.current().state == "unavailable"
    result = _sign_in()
    assert result["reason"] == "unavailable"
    assert result["message"].startswith("Google sign-in is not set up in this version.")


def test_second_concurrent_sign_in_is_refused(machine):
    started, release = threading.Event(), threading.Event()

    def slow(**kwargs):
        started.set()
        release.wait(5)
        return FakeCreds()

    first = {}
    thread = threading.Thread(
        target=lambda: first.update(gi.sign_in(runner=slow, verifier=lambda *a: dict(GOOD), clock=lambda: NOW)))
    thread.start()
    assert started.wait(5)
    second = _sign_in()
    release.set()
    thread.join(5)
    assert second["ok"] is False and second["reason"] == "busy"
    assert first["ok"] is True


def test_events_carry_state_and_reason_only(machine, events):
    _sign_in()
    gi.sign_out()
    assert events == [("identity.signed_in", {"state": "verified", "reason": ""}),
                      ("identity.signed_out", {"state": "none", "reason": ""})]
    assert "alice" not in json.dumps(events)


# ── состояния ─────────────────────────────────────────────────────────────────

def test_verified_turns_stale_after_seven_days_without_deleting(machine):
    _sign_in()
    assert gi.current(clock=lambda: NOW + timedelta(days=7)).state == "verified"
    late = gi.current(clock=lambda: NOW + timedelta(days=9))
    assert late.state == "stale" and late.stale_days == 9 and late.bound_name == ""
    assert "token_identity.json" in _files(machine)


def test_recheck_is_due_after_24_hours(machine):
    _sign_in()
    assert not gi.recheck_due(clock=lambda: NOW + timedelta(hours=23))
    assert gi.recheck_due(clock=lambda: NOW + timedelta(hours=25))


def test_successful_recheck_moves_checked_at_and_recovers_stale(machine):
    _sign_in()
    later = NOW + timedelta(days=9)
    assert gi.recheck(refresher=lambda creds: dict(GOOD), clock=lambda: later) == "verified"
    assert gi.current(clock=lambda: later).state == "verified"


@pytest.mark.parametrize("days", [2, 9])
def test_network_error_never_downgrades_or_deletes(machine, days):
    _sign_in()
    later = NOW + timedelta(days=days)

    def offline(creds):
        raise ConnectionError("no route")

    before = {n: (machine / n).read_bytes() for n in ("token_identity.json", "identity.json")}
    gi.recheck(refresher=offline, clock=lambda: later)
    assert {n: (machine / n).read_bytes() for n in before} == before
    assert gi.current(clock=lambda: later).state == ("verified" if days == 2 else "stale")


def test_invalid_grant_drops_the_token_keeps_the_record_and_expires(machine, events):
    from google.auth.exceptions import RefreshError

    _sign_in()
    events.clear()

    def refused(creds):
        raise RefreshError("invalid_grant: Token has been expired or revoked.")

    assert gi.recheck(refresher=refused, clock=lambda: NOW + timedelta(days=2)) == "expired"
    assert not (machine / "token_identity.json").exists() and (machine / "identity.json").exists()
    who = gi.current()
    assert who.state == "expired" and who.email == "alice@adopy.com"
    assert events == [("identity.expired", {"state": "expired", "reason": "invalid_grant"})]


def test_failed_reverification_expires(machine):
    _sign_in()
    gi.recheck(refresher=lambda c: {**GOOD, "hd": "other.com"}, clock=lambda: NOW + timedelta(days=2))
    assert gi.current().state == "expired"


def test_a_different_email_on_recheck_expires(machine):
    _sign_in()
    gi.recheck(refresher=lambda c: {**GOOD, "email": "bob@adopy.com"}, clock=lambda: NOW + timedelta(days=2))
    assert gi.current().state == "expired"


def test_sealed_or_damaged_token_is_expired_not_a_crash(machine, monkeypatch):
    _sign_in()

    def sealed(path):
        raise token_vault.VaultError("protected storage could not process the value")

    monkeypatch.setattr(token_vault, "read_secret_text", sealed)
    assert gi.recheck(refresher=lambda c: GOOD, clock=lambda: NOW + timedelta(days=2)) == "expired"
    assert gi.current().state == "expired"


def test_missing_token_file_is_expired(machine):
    _sign_in()
    (machine / "token_identity.json").unlink()
    assert gi.recheck(refresher=lambda c: GOOD, clock=lambda: NOW + timedelta(days=2)) == "expired"


def test_corrupt_identity_file_reads_as_none_and_never_raises(machine):
    _sign_in()
    (machine / "identity.json").write_text("{not json", encoding="utf-8")
    gi.reset_cache()
    assert gi.current().state == "none"
    assert gi.recheck(refresher=lambda c: GOOD) == "none"


def test_decline_marks_state_and_never_overrides_a_sign_in(machine):
    assert gi.decline()["identity"]["state"] == "declined"
    assert gi.current().state == "declined" and gi.current().bound_name == ""
    _sign_in()
    assert gi.decline()["identity"]["state"] == "verified"


# ── имя CM ────────────────────────────────────────────────────────────────────

def test_listed_my_cm_name_binds_silently(machine, monkeypatch):
    monkeypatch.setenv("MY_CM_NAME", "Alice")
    result = _sign_in(names=lambda: ["Alice", "Bob"])
    assert result["name_step"] == "bound" and gi.current().cm_name == "Alice" and gi.current().cm_checked


def test_unlisted_name_asks_with_suggestions(machine, monkeypatch):
    monkeypatch.setenv("MY_CM_NAME", "Alise")
    result = _sign_in(names=lambda: ["Alice", "Bob"])
    assert result["name_step"] == "confirm" and result["suggestions"] == ["Alice"]
    assert gi.current().cm_name == ""


def test_no_list_asks_without_suggestions(machine):
    result = _sign_in(names=lambda: None)
    assert result["name_step"] == "confirm" and result["suggestions"] == []


def test_another_person_on_this_computer_is_asked_who_works_here(machine):
    _sign_in()
    gi.set_name("Alice")
    result = _sign_in({**GOOD, "email": "bob@adopy.com", "name": "Bob Example"})
    assert result["name_step"] == "who" and result["suggestions"] == ["Alice"]
    assert gi.current().email == "bob@adopy.com" and gi.current().cm_name == ""


def test_same_person_signing_in_again_keeps_the_binding(machine):
    _sign_in()
    gi.set_name("Alice", cm_checked=True)
    result = _sign_in()
    assert result["name_step"] == "bound" and gi.current().cm_name == "Alice"


def test_set_name_updates_record_and_my_cm_name_together(machine):
    _sign_in()
    gi.set_name("  Alice   Example ", cm_checked=True)
    import os

    assert os.environ["MY_CM_NAME"] == "Alice Example" and gi.current().cm_name == "Alice Example"
    assert 'MY_CM_NAME="Alice Example"' in (machine / ".env").read_text(encoding="utf-8") \
        or "MY_CM_NAME=Alice Example" in (machine / ".env").read_text(encoding="utf-8")


def test_set_name_needs_a_sign_in_and_a_sane_name(machine):
    with pytest.raises(gi.IdentityError):
        gi.set_name("Alice")
    _sign_in()
    for bad in ("", "   ", "x" * 101, "a\x00b"):
        with pytest.raises(gi.IdentityError):
            gi.set_name(bad)


def test_check_name_is_case_insensitive_and_suggests(machine):
    assert gi.check_name("alice", ["Alice", "Bob"]) == (True, [])
    assert gi.check_name("Alise", ["Alice", "Bob"]) == (False, ["Alice"])
    assert gi.check_name("Alice", None) == (False, [])


# ── выход и чужие токены ──────────────────────────────────────────────────────

def test_sign_out_deletes_both_files_keeps_cm_name_and_calls_no_revoke(machine, monkeypatch):
    import os

    import requests

    def no_http(*args, **kwargs):
        raise AssertionError("sign-out must not call Google")

    monkeypatch.setattr(requests, "post", no_http)
    monkeypatch.setattr(requests.Session, "request", no_http)
    _sign_in()
    gi.set_name("Alice")
    result = gi.sign_out()
    assert not ({"token_identity.json", "identity.json"} & _files(machine))
    assert os.environ["MY_CM_NAME"] == "Alice"
    assert result["identity"]["state"] == "none"


def test_sheet_and_telemetry_tokens_are_untouched_by_every_path(machine):
    from google.auth.exceptions import RefreshError

    def untouched():
        return ((machine / "token_accounts.json").read_bytes() == SHEET_TOKEN
                and (machine / "token_telemetry.json").read_bytes() == TELEMETRY_TOKEN)

    _sign_in()
    assert untouched()
    gi.recheck(refresher=lambda c: dict(GOOD), clock=lambda: NOW + timedelta(days=2))
    assert untouched()

    def refused(creds):
        raise RefreshError("invalid_grant")

    gi.recheck(refresher=refused, force=True)
    assert untouched()
    _sign_in()
    gi.sign_out()
    assert untouched()


def test_identity_token_is_written_sealed_and_listed_in_the_vault(machine, monkeypatch):
    monkeypatch.setattr(token_vault, "available", lambda: True)
    monkeypatch.setattr(token_vault, "protect_bytes", lambda b: b"SEAL" + bytes(x ^ 0x5A for x in b))
    monkeypatch.setattr(token_vault, "unprotect_bytes",
                        lambda b: bytes(x ^ 0x5A for x in b[4:]))
    assert "token_identity.json" in token_vault.TOKEN_FILES
    _sign_in()
    raw = (machine / "token_identity.json").read_bytes()
    assert token_vault.is_protected_bytes(raw) and b"identity-refresh-token" not in raw
    # identity.json остаётся читаемым без DPAPI: на горячих путях от неё зависит before_send
    assert json.loads((machine / "identity.json").read_text(encoding="utf-8"))["email"] == "alice@adopy.com"


def test_identity_token_migrates_like_the_other_tokens(machine, monkeypatch):
    monkeypatch.setattr(token_vault, "available", lambda: True)
    monkeypatch.setattr(token_vault, "protect_bytes", lambda b: b"SEAL" + bytes(x ^ 0x5A for x in b))
    monkeypatch.setattr(token_vault, "unprotect_bytes", lambda b: bytes(x ^ 0x5A for x in b[4:]))
    (machine / "token_identity.json").write_text(FakeCreds().to_json(), encoding="utf-8")
    notes = paths.protect_machine_secrets()
    assert any("token_identity.json" in n for n in notes)
    assert token_vault.is_protected_bytes((machine / "token_identity.json").read_bytes())
    paths.unprotect_machine_secrets()
    assert json.loads((machine / "token_identity.json").read_text(encoding="utf-8"))["client_id"] == "cid.apps.example"
