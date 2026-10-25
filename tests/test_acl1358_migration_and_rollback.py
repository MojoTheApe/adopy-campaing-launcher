"""ACL-1358: migration on the packaged start path, and the way back (--unprotect).

DPAPI is replaced by a reversible stand-in so the logic runs on any OS; the real
thing is in test_token_vault_windows.py.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest
from dotenv import dotenv_values

from config import paths, token_vault

ROOT = Path(__file__).resolve().parent.parent
SECRET = "eyJ-secret-automation-token-value-1234567890"
GOOGLE = {"refresh_token": "1//refresh-secret-value-xyz", "client_id": "cid", "client_secret": "csecret-value-1"}


@pytest.fixture
def fake_dpapi(monkeypatch):
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


def _plain_machine(machine):
    (machine / ".env").write_text(f'KEEP=1\nMULTILOGIN_AUTOMATION_TOKEN_A="{SECRET}"\n', encoding="utf-8")
    (machine / "token_accounts.json").write_text(json.dumps(GOOGLE), encoding="utf-8")
    (machine / "token_telemetry.json").write_text(json.dumps(GOOGLE), encoding="utf-8")


def _protected_machine(machine):
    _plain_machine(machine)
    assert paths.protect_machine_secrets()


def _script():
    spec = importlib.util.spec_from_file_location(
        "acl_migrate_config_under_test", ROOT / "scripts" / "acl_migrate_config.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ── packaged start path ───────────────────────────────────────────────────────

def test_load_env_protects_plain_tokens_on_the_packaged_start_path(machine, fake_dpapi):
    """ACL.exe -> launcher -> prepare_config_dir() -> serve.py -> load_env().

    The packaged app never runs ensure_machine_config / acl_migrate_config, so
    load_env is where an installer-based machine's plain tokens get protected.
    """
    from config import config_unify

    _plain_machine(machine)
    config_unify.prepare_config_dir()  # what packaging/windows/launcher.py does first
    paths.load_env()                    # what serve.py does next

    env = (machine / ".env").read_text(encoding="utf-8")
    assert SECRET not in env and "KEEP=1" in env
    for name in ("token_accounts.json", "token_telemetry.json"):
        raw = (machine / name).read_bytes()
        assert raw.startswith(token_vault.FILE_MAGIC) and b"refresh-secret" not in raw
    assert os.environ["MULTILOGIN_AUTOMATION_TOKEN_A"] == SECRET  # the process still gets the plain value
    assert (machine / "protected-backup").is_dir()
    os.environ.pop("MULTILOGIN_AUTOMATION_TOKEN_A", None)


def test_load_env_is_idempotent_and_does_not_pile_up_backups(machine, fake_dpapi):
    _plain_machine(machine)
    paths.load_env()
    first = (machine / ".env").read_bytes()
    backups = sorted(p.name for p in (machine / "protected-backup").iterdir())
    paths.load_env()
    paths.load_env()
    assert (machine / ".env").read_bytes() == first
    assert sorted(p.name for p in (machine / "protected-backup").iterdir()) == backups
    os.environ.pop("MULTILOGIN_AUTOMATION_TOKEN_A", None)


def test_the_packaged_chain_stays_wired():
    launcher = (ROOT / "packaging" / "windows" / "launcher.py").read_text(encoding="utf-8")
    assert "prepare_config_dir()" in launcher
    serve = (ROOT / "serve.py").read_text(encoding="utf-8")
    assert "from config.paths import load_env" in serve and "load_env()" in serve


# ── rollback ──────────────────────────────────────────────────────────────────

def test_unprotect_writes_plaintext_back_for_every_protected_file(machine, fake_dpapi):
    _protected_machine(machine)
    notes = paths.unprotect_machine_secrets()
    assert notes and not any(SECRET in n or "refresh-secret" in n for n in notes)
    env = (machine / ".env").read_text(encoding="utf-8")
    assert "dpapi:v1:" not in env and "KEEP=1" in env
    assert dotenv_values(machine / ".env")["MULTILOGIN_AUTOMATION_TOKEN_A"] == SECRET
    for name in ("token_accounts.json", "token_telemetry.json"):
        assert json.loads((machine / name).read_text(encoding="utf-8")) == GOOGLE
    assert paths.unprotect_machine_secrets() == []  # second run: nothing to do


def test_unprotect_restores_a_file_that_cannot_be_opened_from_the_backup(machine, fake_dpapi):
    _protected_machine(machine)
    broken = machine / "token_accounts.json"
    broken.write_bytes(token_vault.FILE_MAGIC + b"damaged-bytes")
    notes = paths.unprotect_machine_secrets()
    assert any("token_accounts.json" in n and "backup" in n for n in notes)
    assert json.loads(broken.read_text(encoding="utf-8")) == GOOGLE


def test_unprotect_from_backup_restores_the_newest_encrypted_backup(machine, fake_dpapi):
    _protected_machine(machine)
    (machine / "token_accounts.json").write_bytes(token_vault.FILE_MAGIC + b"also-damaged")
    (machine / ".env").write_text("KEEP=1\n", encoding="utf-8")
    notes = paths.unprotect_machine_secrets(from_backup=True)
    assert any("backup" in n for n in notes)
    assert json.loads((machine / "token_accounts.json").read_text(encoding="utf-8")) == GOOGLE
    assert SECRET in (machine / ".env").read_text(encoding="utf-8")


def test_unprotect_leaves_a_file_that_cannot_be_opened_and_has_no_backup(machine, fake_dpapi):
    sealed = token_vault.FILE_MAGIC + b"not-ours"
    (machine / "token_accounts.json").write_bytes(sealed)
    notes = paths.unprotect_machine_secrets()
    assert (machine / "token_accounts.json").read_bytes() == sealed
    assert any("left as it was" in n for n in notes)


def test_unprotect_without_dpapi_does_nothing(machine, monkeypatch):
    (machine / "token_accounts.json").write_text(json.dumps(GOOGLE), encoding="utf-8")
    monkeypatch.setattr(token_vault, "available", lambda: False)
    assert paths.unprotect_machine_secrets() == []


def test_the_migration_script_has_an_unprotect_switch(machine, fake_dpapi, capsys):
    script = _script()
    _protected_machine(machine)
    assert script.main(["--unprotect"]) == 0
    out = capsys.readouterr().out
    assert SECRET not in out and "refresh-secret" not in out
    assert json.loads((machine / "token_accounts.json").read_text(encoding="utf-8")) == GOOGLE
    assert script.main(["--unprotect"]) == 0
    assert "nothing to restore" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        script.main(["--from-backup"])  # only meaningful together with --unprotect


def test_protected_backup_is_not_merged_between_config_folders():
    from config import config_unify

    assert "protected-backup" in config_unify.SKIP_DIRS
