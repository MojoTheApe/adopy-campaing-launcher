"""Where ACL reads a machine's own configuration from.

The rule the whole distribution rests on: code lives in git, a machine's
settings live on the machine. Break it and an update either overwrites ten
buyers' account maps with one person's, or ships their ad account ids into a
repository outside developers read.

These tests pin the three behaviours that keep that rule true (audit findings
F-07…F-09). They run entirely on temporary directories — nothing here reads or
writes the real machine config.
"""
from __future__ import annotations

import csv
import importlib
from pathlib import Path

import pytest

paths = importlib.import_module("config.paths")


@pytest.fixture()
def local_dir(tmp_path, monkeypatch):
    """Point the machine-local config dir at a throwaway folder."""
    d = tmp_path / "ACL"
    monkeypatch.setenv("ACL_CONFIG_DIR", str(d))
    monkeypatch.delenv("META_AD_ACCOUNT_MAP_PATH", raising=False)
    monkeypatch.delenv("GSHEET_OAUTH_CLIENT", raising=False)
    monkeypatch.delenv("GSHEET_OAUTH_TOKEN", raising=False)
    return d


def test_local_config_dir_is_outside_the_checkout(local_dir, monkeypatch):
    monkeypatch.delenv("ACL_CONFIG_DIR", raising=False)
    resolved = Path(paths.get_local_config_dir()).resolve()
    checkout = Path(paths.__file__).resolve().parents[1]  # config/paths.py -> the checkout root
    assert not resolved.is_relative_to(checkout), (
        f"конфиг машины оказался внутри чекаута ({resolved}) — обновление или "
        "повторный clone унесёт настройки байера с собой"
    )


def test_account_map_prefers_the_machine_copy(local_dir):
    local_dir.mkdir(parents=True)
    (local_dir / "ad_account_map.csv").write_text("account_name\n", encoding="utf-8")
    assert paths.get_ad_account_map_path() == str(local_dir / "ad_account_map.csv")


def test_migration_copies_a_legacy_map_once(local_dir, tmp_path, monkeypatch):
    """A machine upgrading from a pre-2026-08-25 install keeps its accounts."""
    legacy = tmp_path / "checkout_map.csv"
    legacy.write_text(
        "account_name,ad_account_id\nFacebookTest1 CR,act_1\nFacebookTest2 EC,act_2\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(paths, "AD_ACCOUNT_MAP_DEFAULT", str(legacy))

    note = paths.migrate_legacy_account_map()
    assert note, "миграция не сработала на машине со старой раскладкой"
    moved = local_dir / "ad_account_map.csv"
    assert moved.is_file()
    with open(moved, newline="", encoding="utf-8") as f:
        assert len(list(csv.DictReader(f))) == 2

    # Second run must be silent: re-running the installer or the doctor is a
    # normal thing to do.
    assert paths.migrate_legacy_account_map() == ""


def test_migration_never_resurrects_a_removed_account(local_dir, tmp_path, monkeypatch):
    """The dashboard can remove an account; the removal has to stick.

    The tempting version of this migration merges both files. That would put
    back exactly the rows someone deleted, on every start, with no way to make
    the deletion hold.
    """
    legacy = tmp_path / "checkout_map.csv"
    legacy.write_text(
        "account_name,ad_account_id\nKept CR,act_1\nDeliberatelyRemoved EC,act_2\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(paths, "AD_ACCOUNT_MAP_DEFAULT", str(legacy))
    local_dir.mkdir(parents=True)
    (local_dir / "ad_account_map.csv").write_text(
        "account_name,ad_account_id\nKept CR,act_1\n", encoding="utf-8"
    )

    paths.migrate_legacy_account_map()
    text = (local_dir / "ad_account_map.csv").read_text(encoding="utf-8")
    assert "DeliberatelyRemoved" not in text, (
        "миграция вернула удалённый аккаунт — удаление на дешборде перестанет держаться"
    )


def test_machine_settings_win_over_the_checkout(local_dir, tmp_path, monkeypatch):
    """python-dotenv keeps the first value it sees, so order is the precedence."""
    local_dir.mkdir(parents=True)
    (local_dir / ".env").write_text("ACL_PROBE=machine\n", encoding="utf-8")
    files = paths.get_env_files()
    assert files and files[0] == str(local_dir / ".env"), (
        "машинный .env должен читаться первым, иначе настройки из чекаута его перекроют"
    )


def test_placeholder_machine_map_does_not_hide_valid_legacy_map(local_dir, tmp_path, monkeypatch):
    legacy = tmp_path / "checkout_map.csv"
    legacy.write_text(
        "account_name,ad_account_id,profile_id\nReal account,act_987654,real-profile\n",
        encoding="utf-8",
    )
    example = tmp_path / "example.csv"
    example.write_text(
        "account_name,ad_account_id,profile_id\nExample,act_1234567890,your-profile-uuid\n",
        encoding="utf-8",
    )
    local_dir.mkdir(parents=True)
    machine_map = local_dir / "ad_account_map.csv"
    machine_map.write_bytes(example.read_bytes())
    monkeypatch.setattr(paths, "AD_ACCOUNT_MAP_DEFAULT", str(legacy))

    note = paths.migrate_legacy_account_map()

    assert "copied" in note
    assert "Real account" in machine_map.read_text(encoding="utf-8")
    assert Path(f"{machine_map}.pre-migration.bak").read_bytes() == example.read_bytes()


def test_invalid_nonplaceholder_map_is_never_overwritten(local_dir, tmp_path, monkeypatch):
    legacy = tmp_path / "checkout_map.csv"
    legacy.write_text(
        "account_name,ad_account_id,profile_id\nLegacy,act_987654,profile\n",
        encoding="utf-8",
    )
    local_dir.mkdir(parents=True)
    machine_map = local_dir / "ad_account_map.csv"
    machine_map.write_text(
        "account_name,ad_account_id,profile_id\nNeeds repair,not-an-act-id,profile\n",
        encoding="utf-8",
    )
    before = machine_map.read_bytes()
    monkeypatch.setattr(paths, "AD_ACCOUNT_MAP_DEFAULT", str(legacy))

    note = paths.migrate_legacy_account_map()

    assert "left untouched" in note
    assert machine_map.read_bytes() == before


def test_legacy_env_moves_once_and_normalizes_blank_engine(local_dir, tmp_path, monkeypatch):
    repo_env = tmp_path / "repo.env"
    repo_env.write_text("ACL_PROBE=legacy\nFB_RPA_ENGINE=\n", encoding="utf-8")
    example = tmp_path / "example.env"
    example.write_text("ACL_PROBE=example\nFB_RPA_ENGINE=recorded_flow\n", encoding="utf-8")
    monkeypatch.setattr(paths, "get_repo_env_path", lambda: str(repo_env))
    monkeypatch.setattr(paths, "ENV_EXAMPLE", str(example))
    monkeypatch.setenv("FB_RPA_ENGINE", "")

    note = paths.migrate_legacy_env(create_from_example=True)

    machine_env = local_dir / ".env"
    text = machine_env.read_text(encoding="utf-8")
    assert "copied out of the checkout" in note
    assert 'FB_RPA_ENGINE="recorded_flow"' in text
    assert paths.get_env_files() == [str(machine_env)]
    assert paths.migrate_legacy_env(create_from_example=True) == ""


def test_machine_env_update_never_writes_the_checkout(local_dir, tmp_path, monkeypatch):
    repo_env = tmp_path / "repo.env"
    repo_env.write_text("UNCHANGED=yes\n", encoding="utf-8")
    local_dir.mkdir(parents=True)
    (local_dir / ".env").write_text("# machine\nTOKEN=old\n", encoding="utf-8")
    monkeypatch.setattr(paths, "get_repo_env_path", lambda: str(repo_env))
    monkeypatch.setenv("TOKEN", "old")

    written = paths.update_machine_env({"TOKEN": "value with # and spaces"})

    assert written == str(local_dir / ".env")
    assert 'TOKEN="value with # and spaces"' in (local_dir / ".env").read_text(encoding="utf-8")
    assert repo_env.read_text(encoding="utf-8") == "UNCHANGED=yes\n"


def test_atomic_csv_failure_preserves_previous_account_map(tmp_path, monkeypatch):
    target = tmp_path / "accounts.csv"
    target.write_text("account_name,ad_account_id\nOld,act_111111\n", encoding="utf-8")
    before = target.read_bytes()

    def fail_replace(_source, _destination):
        raise PermissionError("target is locked")

    monkeypatch.setattr(paths.os, "replace", fail_replace)

    with pytest.raises(PermissionError):
        paths.write_csv_atomic(
            target,
            ["account_name", "ad_account_id"],
            [{"account_name": "New", "ad_account_id": "act_222222"}],
        )

    assert target.read_bytes() == before
    assert not list(tmp_path.glob(".*.tmp"))


def test_legacy_draft_status_moves_to_machine_state(local_dir, tmp_path, monkeypatch):
    checkout = tmp_path / "checkout"
    legacy = checkout / "output" / "draft_status.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text('{"Account": {"state": "blocked"}}', encoding="utf-8")
    monkeypatch.setattr(paths, "PROJECT_ROOT", checkout)

    note = paths.migrate_legacy_draft_status()

    assert "copied" in note
    assert (local_dir / "draft_status.json").read_bytes() == legacy.read_bytes()
    assert paths.migrate_legacy_draft_status() == ""


def test_provision_migrates_real_map_before_creating_example(local_dir, tmp_path, monkeypatch):
    legacy = tmp_path / "checkout_map.csv"
    legacy.write_text(
        "account_name,ad_account_id,profile_id\nReal,act_987654,profile\n",
        encoding="utf-8",
    )
    example = tmp_path / "example.csv"
    example.write_text(
        "account_name,ad_account_id,profile_id\nExample,act_1234567890,your-profile-uuid\n",
        encoding="utf-8",
    )
    env_example = tmp_path / "env.example"
    env_example.write_text("FB_RPA_ENGINE=recorded_flow\n", encoding="utf-8")
    monkeypatch.setattr(paths, "AD_ACCOUNT_MAP_DEFAULT", str(legacy))
    monkeypatch.setattr(paths, "AD_ACCOUNT_MAP_EXAMPLE", str(example))
    monkeypatch.setattr(paths, "ENV_EXAMPLE", str(env_example))
    monkeypatch.setattr(paths, "get_repo_env_path", lambda: str(tmp_path / "absent.env"))

    paths.ensure_machine_config()

    assert "Real" in (local_dir / "ad_account_map.csv").read_text(encoding="utf-8")
    assert "Example" not in (local_dir / "ad_account_map.csv").read_text(encoding="utf-8")


def test_google_oauth_defaults_are_machine_local(local_dir, tmp_path, monkeypatch):
    checkout = tmp_path / "empty-checkout"
    checkout.mkdir()
    monkeypatch.setattr(paths, "PROJECT_ROOT", checkout)

    assert paths.get_google_oauth_client_path() == str(local_dir / "oauth_client.json")
    assert paths.get_google_oauth_token_path() == str(local_dir / "token_accounts.json")


def test_google_oauth_explicit_paths_win(local_dir, tmp_path, monkeypatch):
    client = tmp_path / "custom-client.json"
    token = tmp_path / "custom-token.json"
    monkeypatch.setenv("GSHEET_OAUTH_CLIENT", str(client))
    monkeypatch.setenv("GSHEET_OAUTH_TOKEN", str(token))

    assert paths.get_google_oauth_client_path() == str(client)
    assert paths.get_google_oauth_token_path() == str(token)


def test_legacy_google_oauth_files_move_out_of_checkout(local_dir, tmp_path, monkeypatch):
    checkout = tmp_path / "checkout"
    legacy_config = checkout / "config"
    legacy_config.mkdir(parents=True)
    (legacy_config / "oauth_client.json").write_text('{"installed": {}}', encoding="utf-8")
    (legacy_config / "token_accounts.json").write_text('{"token": "fake"}', encoding="utf-8")
    monkeypatch.setattr(paths, "PROJECT_ROOT", checkout)

    notes = paths.migrate_legacy_google_oauth()

    assert len(notes) == 2
    assert (local_dir / "oauth_client.json").read_bytes() == (legacy_config / "oauth_client.json").read_bytes()
    assert (local_dir / "token_accounts.json").read_bytes() == (legacy_config / "token_accounts.json").read_bytes()
    assert paths.migrate_legacy_google_oauth() == []
