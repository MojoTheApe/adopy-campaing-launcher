import os
import stat
import unicodedata
from pathlib import Path

import pytest

from config import paths

REPO = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")  # linux-ok: права POSIX проверяются на Linux
def test_machine_folder_is_owner_only(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("X=1")
    (tmp_path / "token_accounts.json").write_text("{}")
    paths.harden_machine_dir(str(tmp_path))
    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / ".env").stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "token_accounts.json").stat().st_mode) == 0o600


def test_hardening_is_silent_on_a_missing_folder(tmp_path: Path) -> None:
    absent = tmp_path / "absent"
    paths.harden_machine_dir(str(absent))
    assert not absent.exists()


def test_hardening_does_not_touch_nested_folders(tmp_path: Path, monkeypatch) -> None:
    nested = tmp_path / "backups"
    nested.mkdir()
    (nested / "a.txt").write_text("x")
    modes = []
    monkeypatch.setattr(paths.os, "chmod", lambda p, m: modes.append((os.path.basename(p), m)))
    monkeypatch.setattr(paths.os, "name", "posix")
    (tmp_path / ".env").write_text("X=1")
    paths.harden_machine_dir(str(tmp_path))
    assert (tmp_path.name, 0o700) in modes
    assert (".env", 0o600) in modes
    assert all(name not in ("backups", "a.txt") for name, _ in modes)


def test_launcher_keeps_the_mac_awake_only_while_serving() -> None:
    launcher = (REPO / "start_dashboard.sh").read_text(encoding="utf-8")
    assert 'caffeinate -i -w "$SERVER_PID"' in launcher


def test_creative_found_when_disk_name_is_nfd(tmp_path: Path) -> None:
    from src.meta_campaign_agent import creative_finder
    from src.campaign_bridge.validation import _creative_file_on_disk
    from src.creative_agent.creative_scanner import find_creative_file as scan_find

    disk = unicodedata.normalize("NFD", "Крей.mp4")
    asked = unicodedata.normalize("NFC", "Крей.mp4")
    assert disk != asked
    (tmp_path / disk).write_bytes(b"x")
    creative_finder.reset_creative_index()
    assert creative_finder.find_creative_file(tmp_path, asked).name == disk
    assert _creative_file_on_disk(asked, str(tmp_path))
    assert scan_find(asked, tmp_path) is not None
