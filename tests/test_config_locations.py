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

import pytest

paths = importlib.import_module("config.paths")


@pytest.fixture()
def local_dir(tmp_path, monkeypatch):
    """Point the machine-local config dir at a throwaway folder."""
    d = tmp_path / "ACL"
    monkeypatch.setenv("ACL_CONFIG_DIR", str(d))
    monkeypatch.delenv("META_AD_ACCOUNT_MAP_PATH", raising=False)
    return d


def test_local_config_dir_is_outside_the_checkout(local_dir, monkeypatch):
    monkeypatch.delenv("ACL_CONFIG_DIR", raising=False)
    resolved = paths.get_local_config_dir().replace("/", "\\").lower()
    checkout = str(paths.__file__).rsplit("\\config\\", 1)[0].lower()
    assert not resolved.startswith(checkout), (
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
