"""ACL-2330: the vault migration cleans up the clear-text secret lines it used to leave behind."""
from __future__ import annotations

import os

from config import paths, token_vault
from src.analysis_agent import connection_settings as cs

from tests.test_token_vault import SECRET, fake_dpapi, machine  # noqa: F401  (fixtures)

KEY = "MULTILOGIN_AUTOMATION_TOKEN_A"
OLD = "old-duplicate-token-value-0000000000"
# python-dotenv keeps A literally inside double quotes, the migrator's own parser turns it into "A".
MISMATCH_LINE = 'ACL_LIBRARY_TOKEN="abc' + chr(92) + 'u0041defghijklmnopqrstuvwxyz0123"'


def _read(machine):
    return (machine / ".env").read_text(encoding="utf-8")


def test_a_dead_duplicate_is_removed_and_the_live_value_is_encrypted(machine, fake_dpapi):  # noqa: F811
    (machine / ".env").write_text(f"{KEY}={OLD}\nKEEP=1\n{KEY}={SECRET}\n", encoding="utf-8")
    note = token_vault.migrate_env_file(machine / ".env", machine)
    text = _read(machine)
    assert OLD not in text and SECRET not in text
    assert text.count(KEY) == 1 and "dpapi:v1:" in text and "KEEP=1" in text
    assert "1 earlier duplicate" in note
    assert token_vault.backups_of(".env", machine), "the encrypted backup keeps the dropped line"
    paths.load_env()
    assert os.environ[KEY] == SECRET


def test_a_quoting_mismatch_note_points_to_the_sheet_sign_in(machine, fake_dpapi):  # noqa: F811
    (machine / ".env").write_text(MISMATCH_LINE + "\n", encoding="utf-8")
    note = token_vault.migrate_env_file(machine / ".env", machine)
    assert "ACL_LIBRARY_TOKEN" in note and "reads differently" in note
    assert "Sign in with Google" in note and "Settings" not in note
    assert "duplicate" not in note
    assert _read(machine) == MISMATCH_LINE + "\n"


def test_a_sheet_refresh_with_the_same_value_encrypts_a_clear_text_line(machine, fake_dpapi, monkeypatch):  # noqa: F811
    token = "abcqdefghijklmnopqrstuvwxyz0123"
    (machine / ".env").write_text('ACL_LIBRARY_TOKEN=' + token + "\n", encoding="utf-8")
    monkeypatch.setenv("ACL_LIBRARY_TOKEN", token)       # the process already holds the same value
    assert token_vault.clear_text_secret_keys(machine / ".env") == {"ACL_LIBRARY_TOKEN"}
    saved = []
    monkeypatch.setattr(cs, "validate", lambda values: dict(values))
    result = cs.apply({"ACL_LIBRARY_TOKEN": token}, writer=lambda changed: saved.append(dict(changed)))
    assert saved == [{"ACL_LIBRARY_TOKEN": token}] and result["updated"] == 1
    # through the real writer the line ends up encrypted
    cs.apply({"ACL_LIBRARY_TOKEN": token})
    assert token not in _read(machine) and "dpapi:v1:" in _read(machine)
    assert token_vault.clear_text_secret_keys(machine / ".env") == set()


def test_an_already_encrypted_line_is_not_rewritten(machine, fake_dpapi, monkeypatch):  # noqa: F811
    paths.update_machine_env({"ACL_LIBRARY_TOKEN": SECRET})
    monkeypatch.setattr(cs, "validate", lambda values: dict(values))
    saved = []
    result = cs.apply({"ACL_LIBRARY_TOKEN": SECRET}, writer=lambda changed: saved.append(changed))
    assert saved == [] and result["updated"] == 0
