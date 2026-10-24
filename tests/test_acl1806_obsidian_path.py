"""ACL-1806: one resolver for the Obsidian dashboard folder."""
from __future__ import annotations

from pathlib import Path

import pytest

from config import paths
from scripts import acl_board, acl_telemetry_report, build_creative_notes, meta_docs_watch


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.delenv("ACL_OBSIDIAN_DIR", raising=False)
    return tmp_path


def test_env_override_wins(home, monkeypatch):
    (home / "Desktop" / "Obsidian" / "дешборд").mkdir(parents=True)
    custom = home / "custom"
    custom.mkdir()
    monkeypatch.setenv("ACL_OBSIDIAN_DIR", str(custom))
    assert paths.get_obsidian_dashboard_dir() == custom
    assert meta_docs_watch.default_vault_dir() == custom


def test_env_pointing_nowhere_falls_back(home, monkeypatch):
    real = home / "Desktop" / "Obsidian" / "дешборд"
    real.mkdir(parents=True)
    monkeypatch.setenv("ACL_OBSIDIAN_DIR", str(home / "missing"))
    assert paths.get_obsidian_dashboard_dir() == real
    assert meta_docs_watch.default_vault_dir() == real


def test_first_existing_candidate(home):
    legacy = home / "Desktop" / "Obsidian" / "Kuwait Forms" / "дешборд"
    legacy.mkdir(parents=True)
    assert paths.get_obsidian_dashboard_dir() == legacy
    assert meta_docs_watch.default_vault_dir() == legacy
    real = home / "Desktop" / "Obsidian" / "дешборд"
    real.mkdir(parents=True)
    assert paths.get_obsidian_dashboard_dir() == real
    assert meta_docs_watch.default_vault_dir() == real


def test_none_when_nothing_exists(home):
    assert paths.get_obsidian_dashboard_dir() is None
    assert meta_docs_watch.default_vault_dir() is None


def test_watcher_uses_resolver(home, monkeypatch):
    monkeypatch.setattr(paths, "get_obsidian_dashboard_dir", lambda: home / "V")
    assert meta_docs_watch.default_vault_dir() == home / "V"


def test_board_uses_resolver(home, monkeypatch):
    monkeypatch.delenv(acl_board.BOARD_ENV, raising=False)
    monkeypatch.setattr(paths, "get_obsidian_dashboard_dir", lambda: home / "V")
    assert acl_board.board_path() == home / "V" / acl_board.BOARD_NAME


def test_board_warns_when_vault_missing(home, monkeypatch, capsys):
    monkeypatch.delenv(acl_board.BOARD_ENV, raising=False)
    acl_board.board_path()
    assert "ACL_OBSIDIAN_DIR" in capsys.readouterr().err


def test_telemetry_report_uses_resolver_and_warns(home, monkeypatch, capsys):
    monkeypatch.setattr(acl_telemetry_report.telemetry_sync, "fetch_rows",
                        lambda local=False: ([{"ts": "2026-10-01T00:00:00"}], "test"))
    monkeypatch.setattr(acl_telemetry_report, "analyse",
                        lambda rows, days: {"machines": [], "runs": 0, "regressions": []})
    monkeypatch.setattr(acl_telemetry_report, "render", lambda a, s: "TEXT")
    monkeypatch.setattr("sys.argv", ["x"])
    assert acl_telemetry_report.main() == 1
    cap = capsys.readouterr()
    assert "ACL_OBSIDIAN_DIR" in cap.err and "TEXT" in cap.out

    vault = home / "V"
    vault.mkdir()
    monkeypatch.setattr(paths, "get_obsidian_dashboard_dir", lambda: vault)
    assert acl_telemetry_report.main() == 0
    assert (vault / acl_telemetry_report.NOTE_NAME).read_text(encoding="utf-8") == "TEXT"


def test_creative_notes_use_resolver_and_warn(home, tmp_path, monkeypatch, capsys):
    db = tmp_path / "a.sqlite3"
    db.write_text("")
    assert build_creative_notes.main(["--db", str(db)]) == 2
    assert "ACL_OBSIDIAN_DIR" in capsys.readouterr().err

    seen = {}
    monkeypatch.setattr(paths, "get_obsidian_dashboard_dir", lambda: home)

    def boom(path):
        seen["db"] = path
        raise RuntimeError("stop")

    monkeypatch.setattr(build_creative_notes.ca, "connect", boom)
    with pytest.raises(RuntimeError):
        build_creative_notes.main(["--db", str(db)])
    assert seen["db"] == str(db)  # vault check passed with the resolved folder
