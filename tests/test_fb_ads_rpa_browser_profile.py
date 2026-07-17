"""Unit tests for MLX profile chrome version helpers."""
from __future__ import annotations

from pathlib import Path

from src.fb_ads_rpa.browser import (
    _cleanup_chrome_migration_leftovers,
    _parse_chrome_major_minor,
    _profile_chrome_version,
)


def test_parse_chrome_major_minor():
    assert _parse_chrome_major_minor("149.0.7827.55") == (149, 0)
    assert _parse_chrome_major_minor("150.4") == (150, 4)
    assert _parse_chrome_major_minor(None) is None


def test_profile_chrome_version_from_last_version(tmp_path: Path):
    (tmp_path / "Last Version").write_text("149.0.7827.55\n", encoding="utf-8")
    assert _profile_chrome_version(tmp_path) == "149.0.7827.55"


def test_profile_chrome_version_from_snapshots(tmp_path: Path):
    snaps = tmp_path / "Snapshots"
    snaps.mkdir()
    (snaps / "149.0.7827.23").mkdir()
    (snaps / "149.0.7827.55").mkdir()
    assert _profile_chrome_version(tmp_path) == "149.0.7827.55"


def test_cleanup_removes_chrome_delete_dirs(tmp_path: Path):
    junk = tmp_path / "profile.CHROME_DELETE"
    junk.mkdir()
    (junk / "x.txt").write_text("x", encoding="utf-8")
    gpu = tmp_path / "GPUPersistentCache"
    gpu.mkdir()
    (gpu / "y.bin").write_text("y", encoding="utf-8")
    _cleanup_chrome_migration_leftovers(tmp_path)
    assert not junk.exists()
    assert not gpu.exists()
