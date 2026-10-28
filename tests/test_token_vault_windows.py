"""Real Windows DPAPI for the token vault (ACL-1358). The logic tests live in test_token_vault.py."""
from __future__ import annotations

import json

import pytest

from config import paths, token_vault

pytestmark = pytest.mark.windows_only(reason="real Windows DPAPI (crypt32 CryptProtectData)")

SECRET = "eyJ-secret-automation-token-value-1234567890"
GOOGLE = {"refresh_token": "1//refresh-secret-value-xyz", "client_id": "cid", "client_secret": "csecret-value-1"}


def test_real_dpapi_round_trip_is_user_bound_ciphertext():
    sealed = token_vault.encode_env_value(SECRET)
    assert sealed.startswith("dpapi:v1:") and SECRET not in sealed
    assert token_vault.decode_env_value(sealed) == SECRET
    with pytest.raises(token_vault.VaultError):
        token_vault.decode_env_value("dpapi:v1:AAAA")


def test_real_dpapi_migrate_then_unprotect_restores_the_exact_files(tmp_path, monkeypatch):
    config = tmp_path / "ACL"
    config.mkdir()
    monkeypatch.setenv("ACL_CONFIG_DIR", str(config))
    monkeypatch.delenv("GSHEET_OAUTH_TOKEN", raising=False)
    (config / ".env").write_text(f'KEEP=1\nMULTILOGIN_AUTOMATION_TOKEN_A="{SECRET}"\n', encoding="utf-8")
    original = json.dumps(GOOGLE)
    (config / "token_accounts.json").write_text(original, encoding="utf-8")

    assert paths.protect_machine_secrets()
    assert SECRET.encode() not in (config / ".env").read_bytes()
    assert b"refresh-secret" not in (config / "token_accounts.json").read_bytes()

    assert paths.unprotect_machine_secrets()
    assert SECRET in (config / ".env").read_text(encoding="utf-8")
    assert (config / "token_accounts.json").read_text(encoding="utf-8") == original
