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
    assert any(".env" in n and "2 secret value(s)" in n for n in notes)
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


LIB_TOKEN = "ghp_FAKELIBRARYTOKEN0000000000000000"
BI_PASSWORD = "fake-bi-password-0000-xyz"


def _clear_team_env(monkeypatch):
    for key in ("ACL_LIBRARY_TOKEN", "BI_MYSQL_PASSWORD"):
        monkeypatch.delenv(key, raising=False)


def test_library_token_and_bi_password_are_vault_keys():
    assert token_vault.is_secret_env_key("ACL_LIBRARY_TOKEN")
    assert token_vault.is_secret_env_key("BI_MYSQL_PASSWORD")
    assert not token_vault.is_secret_env_key("ACL_LIBRARY_REPO")
    assert not token_vault.is_secret_env_key("BI_MYSQL_HOST")


def test_writing_library_token_and_bi_password_goes_through_the_vault(machine, fake_dpapi, monkeypatch):
    _clear_team_env(monkeypatch)
    paths.update_machine_env({"ACL_LIBRARY_TOKEN": LIB_TOKEN, "BI_MYSQL_PASSWORD": BI_PASSWORD,
                              "BI_MYSQL_HOST": "bi.example.invalid"})
    text = (machine / ".env").read_text(encoding="utf-8")
    assert LIB_TOKEN not in text and BI_PASSWORD not in text
    assert text.count("dpapi:v1:") == 2 and "bi.example.invalid" in text
    # the running process (and so team_library / bi_stats) sees the plain value
    assert os.environ["ACL_LIBRARY_TOKEN"] == LIB_TOKEN and os.environ["BI_MYSQL_PASSWORD"] == BI_PASSWORD
    # a restart reads them back through load_env
    monkeypatch.delenv("ACL_LIBRARY_TOKEN")
    monkeypatch.delenv("BI_MYSQL_PASSWORD")
    paths.load_env()
    assert os.environ["ACL_LIBRARY_TOKEN"] == LIB_TOKEN and os.environ["BI_MYSQL_PASSWORD"] == BI_PASSWORD


def test_old_clear_text_env_migrates_once_and_leaves_no_secret(machine, fake_dpapi, monkeypatch):
    _clear_team_env(monkeypatch)
    (machine / ".env").write_text(
        f'KEEP_ME=1\nACL_LIBRARY_TOKEN={LIB_TOKEN}\nBI_MYSQL_HOST=bi.example.invalid\nBI_MYSQL_PASSWORD="{BI_PASSWORD}"\n',
        encoding="utf-8")
    notes = paths.protect_machine_secrets()
    assert any("2 secret value(s)" in n for n in notes)
    text = (machine / ".env").read_text(encoding="utf-8")
    assert LIB_TOKEN not in text and BI_PASSWORD not in text
    assert "KEEP_ME=1" in text and "BI_MYSQL_HOST=bi.example.invalid" in text
    # once: second run changes nothing and makes no new backup
    assert paths.protect_machine_secrets() == []
    assert (machine / ".env").read_text(encoding="utf-8") == text
    assert len(list((machine / "protected-backup").glob("*.dpapi"))) == 1
    # and the values are still readable by the readers after load_env
    paths.load_env()
    assert os.environ["ACL_LIBRARY_TOKEN"] == LIB_TOKEN and os.environ["BI_MYSQL_PASSWORD"] == BI_PASSWORD


def test_load_env_migrates_a_clear_text_install_and_readers_still_work(machine, fake_dpapi, monkeypatch):
    from src.analysis_agent import bi_stats, team_library

    _clear_team_env(monkeypatch)
    for key in ("BI_MYSQL_HOST", "BI_MYSQL_USER", "FB_STATS_SOURCE"):
        monkeypatch.delenv(key, raising=False)
    (machine / ".env").write_text(
        f"ACL_LIBRARY_TOKEN={LIB_TOKEN}\nBI_MYSQL_HOST=h.example.invalid\nBI_MYSQL_USER=u\nBI_MYSQL_PASSWORD={BI_PASSWORD}\n",
        encoding="utf-8")
    monkeypatch.delenv("ACL_DEFER_TOKEN_PROTECTION", raising=False)
    paths.load_env()
    text = (machine / ".env").read_text(encoding="utf-8")
    assert LIB_TOKEN not in text and BI_PASSWORD not in text
    assert bi_stats.bi_enabled() and bi_stats._settings()["password"] == BI_PASSWORD
    assert team_library.configured()
    assert os.environ["ACL_LIBRARY_TOKEN"] == LIB_TOKEN


def test_scrubbers_hide_library_token_and_bi_password(machine, fake_dpapi, monkeypatch):
    _clear_team_env(monkeypatch)
    paths.update_machine_env({"ACL_LIBRARY_TOKEN": LIB_TOKEN, "BI_MYSQL_PASSWORD": BI_PASSWORD})
    cleaned = token_vault.hide_configured_secrets(f"url?t={LIB_TOKEN} pw={BI_PASSWORD}")
    assert LIB_TOKEN not in cleaned and BI_PASSWORD not in cleaned


def test_unreadable_library_token_is_dropped_not_sent_as_bearer(machine, fake_dpapi, monkeypatch, capsys):
    _clear_team_env(monkeypatch)
    (machine / ".env").write_text('ACL_LIBRARY_TOKEN="dpapi:v1:AAAA"\n', encoding="utf-8")
    paths.load_env()
    assert "ACL_LIBRARY_TOKEN" not in os.environ
    assert "dpapi" not in capsys.readouterr().err


def test_without_dpapi_library_token_stays_as_before(machine, monkeypatch):
    _clear_team_env(monkeypatch)
    monkeypatch.setattr(token_vault, "available", lambda: False)
    paths.update_machine_env({"ACL_LIBRARY_TOKEN": LIB_TOKEN})
    assert LIB_TOKEN in (machine / ".env").read_text(encoding="utf-8")
    assert paths.protect_machine_secrets() == []


def test_hand_written_escape_is_not_sealed_as_a_different_value(machine, fake_dpapi, monkeypatch):
    r"""ACL-2318 review: dotenv reads "a\"b\c" as a"b\c; our own parser must not change it."""
    from dotenv import dotenv_values

    _clear_team_env(monkeypatch)
    line = r'BI_MYSQL_PASSWORD="a\"b\c"'
    (machine / ".env").write_text(f"KEEP_ME=1\n{line}\nACL_LIBRARY_TOKEN={LIB_TOKEN}\n", encoding="utf-8")
    expected = dotenv_values(machine / ".env")["BI_MYSQL_PASSWORD"]
    notes = paths.protect_machine_secrets()
    text = (machine / ".env").read_text(encoding="utf-8")
    assert line in text                       # left as the user wrote it, not sealed wrongly
    assert LIB_TOKEN not in text              # the other secret is still moved
    assert any("left in clear text" in n and "BI_MYSQL_PASSWORD" in n for n in notes)
    assert expected not in "".join(notes)
    paths.load_env()
    assert os.environ["BI_MYSQL_PASSWORD"] == expected


def test_restore_from_backup_reads_values_like_the_app(machine, fake_dpapi, monkeypatch):
    from dotenv import dotenv_values

    _clear_team_env(monkeypatch)
    (machine / ".env").write_text(r'BI_MYSQL_PASSWORD="p\q\"r"' + "\n", encoding="utf-8")
    expected = dotenv_values(machine / ".env")["BI_MYSQL_PASSWORD"]
    # a backup of the original, as an older migration would have left it
    token_vault._backup((machine / ".env").read_bytes(), ".env", machine)
    (machine / ".env").write_text("OTHER=1\n", encoding="utf-8")
    token_vault.unprotect_env_file(machine / ".env", machine, from_backup=True)
    assert dotenv_values(machine / ".env")["BI_MYSQL_PASSWORD"] == expected


def test_bi_password_of_three_characters_is_hidden_by_the_vault_scrubber(machine, fake_dpapi, monkeypatch):
    _clear_team_env(monkeypatch)
    paths.update_machine_env({"BI_MYSQL_PASSWORD": "xQ7"})
    assert "xQ7" not in token_vault.hide_configured_secrets("pw=xQ7")
