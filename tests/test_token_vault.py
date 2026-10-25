"""Tokens live in Windows protected storage; plain files migrate safely (ACL-1358)."""
from __future__ import annotations

import json
import os

import pytest

from config import paths, token_vault

SECRET = "eyJ-secret-automation-token-value-1234567890"
GOOGLE = {"refresh_token": "1//refresh-secret-value-xyz", "client_id": "cid", "client_secret": "csecret-value-1"}


@pytest.fixture
def fake_dpapi(monkeypatch):
    """Reversible stand-in for DPAPI so the logic is testable on any OS."""
    monkeypatch.setattr(token_vault, "available", lambda: True)
    monkeypatch.setattr(token_vault, "protect_bytes", lambda b: b"SEAL" + bytes(x ^ 0x5A for x in b))

    def unseal(b):
        if not b.startswith(b"SEAL"):
            raise token_vault.VaultError("not ours")
        return bytes(x ^ 0x5A for x in b[4:])

    monkeypatch.setattr(token_vault, "unprotect_bytes", unseal)


@pytest.fixture
def machine(tmp_path, monkeypatch):
    config = tmp_path / "ACL"
    config.mkdir()
    monkeypatch.setenv("ACL_CONFIG_DIR", str(config))
    monkeypatch.delenv("GSHEET_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("MULTILOGIN_AUTOMATION_TOKEN_A", raising=False)
    return config


def test_saving_a_token_stores_ciphertext_but_the_process_gets_the_plain_value(machine, fake_dpapi):
    (machine / ".env").write_text("KEEP_ME=1\n", encoding="utf-8")
    paths.update_machine_env({"MULTILOGIN_AUTOMATION_TOKEN_A": SECRET, "OTHER": "visible"})
    text = (machine / ".env").read_text(encoding="utf-8")
    assert SECRET not in text and "dpapi:v1:" in text
    assert "KEEP_ME=1" in text and 'OTHER="visible"' in text
    assert os.environ["MULTILOGIN_AUTOMATION_TOKEN_A"] == SECRET


def test_load_env_opens_the_protected_token(machine, fake_dpapi, monkeypatch):
    paths.update_machine_env({"MULTILOGIN_AUTOMATION_TOKEN_A": SECRET})
    monkeypatch.delenv("MULTILOGIN_AUTOMATION_TOKEN_A")
    paths.load_env()
    assert os.environ["MULTILOGIN_AUTOMATION_TOKEN_A"] == SECRET


def test_a_token_that_cannot_be_opened_is_dropped_not_passed_on(machine, fake_dpapi, monkeypatch, capsys):
    (machine / ".env").write_text('MULTILOGIN_AUTOMATION_TOKEN_A="dpapi:v1:AAAA"\n', encoding="utf-8")
    monkeypatch.delenv("MULTILOGIN_AUTOMATION_TOKEN_A", raising=False)
    paths.load_env()
    assert "MULTILOGIN_AUTOMATION_TOKEN_A" not in os.environ
    assert "dpapi" not in capsys.readouterr().err


def test_migration_protects_env_keeps_other_lines_and_an_encrypted_backup(machine, fake_dpapi):
    original = f'KEEP_ME=1\nMULTILOGIN_AUTOMATION_TOKEN_A="{SECRET}"\nMULTILOGIN_AUTOMATION_TOKEN_B={SECRET}b\n# c\n'
    (machine / ".env").write_bytes(original.encode("utf-8"))
    notes = paths.protect_machine_secrets()
    text = (machine / ".env").read_text(encoding="utf-8")
    assert SECRET not in text and "KEEP_ME=1" in text and "# c" in text
    assert any(".env" in n and "2 Multilogin" in n for n in notes)
    backups = list((machine / "protected-backup").glob(".env.*.dpapi")) or list((machine / "protected-backup").glob("*.dpapi"))
    assert len(backups) == 1
    raw = backups[0].read_bytes()
    assert SECRET.encode() not in raw
    assert token_vault.unprotect_bytes(raw[len(token_vault.FILE_MAGIC):]).decode() == original
    # Both tokens survive a round trip.
    assert token_vault.decode_env_value(json.loads(text.split("MULTILOGIN_AUTOMATION_TOKEN_A=")[1].splitlines()[0])) == SECRET
    # Idempotent: a second run changes nothing and makes no new backup.
    assert paths.protect_machine_secrets() == []
    assert len(list((machine / "protected-backup").glob("*.dpapi"))) == 1


def test_migration_protects_google_token_files_in_place(machine, fake_dpapi):
    for name in ("token_accounts.json", "token_telemetry.json"):
        (machine / name).write_text(json.dumps(GOOGLE), encoding="utf-8")
    notes = paths.protect_machine_secrets()
    assert len(notes) == 2
    for name in ("token_accounts.json", "token_telemetry.json"):
        raw = (machine / name).read_bytes()
        assert token_vault.is_protected_bytes(raw) and b"refresh-secret" not in raw
        assert json.loads(token_vault.read_secret_text(machine / name)) == GOOGLE
    assert paths.protect_machine_secrets() == []


def test_a_failed_verification_leaves_the_plain_file_untouched(machine, fake_dpapi, monkeypatch):
    path = machine / "token_accounts.json"
    path.write_text(json.dumps(GOOGLE), encoding="utf-8")
    monkeypatch.setattr(token_vault, "unprotect_bytes", lambda b: b"garbage")
    notes = token_vault.migrate_file(path, machine)
    assert "did not verify" in notes
    assert json.loads(path.read_text(encoding="utf-8")) == GOOGLE


def test_without_dpapi_everything_stays_as_before(machine, monkeypatch):
    monkeypatch.setattr(token_vault, "available", lambda: False)
    (machine / ".env").write_text("A=1\n", encoding="utf-8")
    paths.update_machine_env({"MULTILOGIN_AUTOMATION_TOKEN_A": SECRET})
    assert SECRET in (machine / ".env").read_text(encoding="utf-8")
    (machine / "token_accounts.json").write_text(json.dumps(GOOGLE), encoding="utf-8")
    assert paths.protect_machine_secrets() == []
    token_vault.write_secret_text(machine / "token_telemetry.json", json.dumps(GOOGLE))
    assert json.loads((machine / "token_telemetry.json").read_text(encoding="utf-8")) == GOOGLE
    assert not (machine / "protected-backup").exists()


def test_support_bundle_denylist_knows_the_plain_values_of_protected_secrets(machine, fake_dpapi):
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "acl_diagnostics_under_test", Path(__file__).resolve().parents[1] / "packaging" / "windows" / "diagnostics.py")
    diagnostics = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(diagnostics)

    paths.update_machine_env({"MULTILOGIN_AUTOMATION_TOKEN_A": SECRET})
    (machine / "token_accounts.json").write_text(json.dumps(GOOGLE), encoding="utf-8")
    paths.protect_machine_secrets()
    values = diagnostics.local_values_to_redact(machine)
    assert SECRET in values and GOOGLE["refresh_token"] in values
    cleaned = diagnostics.sanitize_text(f"log {SECRET} and {GOOGLE['refresh_token']}", values)
    assert SECRET not in cleaned and GOOGLE["refresh_token"] not in cleaned


def test_telemetry_recognises_its_protected_token(machine, fake_dpapi, monkeypatch):
    from src.fb_ads_rpa import telemetry_sync

    token = {**GOOGLE, "scopes": list(telemetry_sync._SCOPES)}
    path = machine / telemetry_sync._TOKEN_FILE
    path.write_text(json.dumps(token), encoding="utf-8")
    paths.protect_machine_secrets()
    assert b"refresh-secret" not in path.read_bytes()
    assert telemetry_sync._has_oauth_token() is True
