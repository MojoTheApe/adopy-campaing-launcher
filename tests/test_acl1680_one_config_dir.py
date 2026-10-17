"""ACL-1680: every launch path uses one real settings folder, never the MSIX copy."""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from config import config_unify, paths

VIRTUAL = r"C:\Users\Buyer\AppData\Local\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Local"
REAL = r"C:\Users\Buyer\AppData\Local"


def test_virtual_package_path_maps_to_the_real_folder():
    assert paths.unvirtualize_path(VIRTUAL) == REAL
    assert paths.unvirtualize_path(VIRTUAL + r"\ACL\.env") == REAL + r"\ACL\.env"
    slashed = paths.unvirtualize_path(VIRTUAL.replace("\\", "/") + "/ACL")
    assert slashed.replace("/", "\\") == REAL + r"\ACL"
    assert paths.unvirtualize_path(REAL + r"\ACL") == REAL + r"\ACL"
    assert paths.unvirtualize_path("") == ""


def test_default_config_dir_ignores_the_virtual_localappdata(monkeypatch):
    monkeypatch.delenv("ACL_CONFIG_DIR", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", VIRTUAL)
    assert paths.is_virtualized_environment()
    assert paths.get_local_config_dir() == os.path.join(REAL, "ACL")


def test_plain_localappdata_is_unchanged(monkeypatch):
    monkeypatch.delenv("ACL_CONFIG_DIR", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", REAL)
    assert not paths.is_virtualized_environment()
    assert paths.get_local_config_dir() == os.path.join(REAL, "ACL")


def test_explicit_dir_is_honoured_but_a_virtual_one_is_corrected(monkeypatch, tmp_path):
    monkeypatch.setenv("ACL_CONFIG_DIR", str(tmp_path / "portable"))
    assert paths.get_local_config_dir() == str(tmp_path / "portable")
    monkeypatch.setenv("ACL_CONFIG_DIR", VIRTUAL + r"\ACL")
    assert paths.get_local_config_dir() == REAL + r"\ACL"


def test_pin_sets_the_env_for_child_processes(monkeypatch):
    monkeypatch.delenv("ACL_CONFIG_DIR", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", VIRTUAL)
    assert paths.pin_config_dir_env() == REAL + r"\ACL"
    assert os.environ["ACL_CONFIG_DIR"] == REAL + r"\ACL"


def _layout(tmp_path, monkeypatch):
    real_local = tmp_path / "AppData" / "Local"
    shadow = real_local / "Packages" / "Claude_x" / "LocalCache" / "Local" / "ACL"
    real = real_local / "ACL"
    shadow.mkdir(parents=True)
    real.mkdir(parents=True)
    monkeypatch.delenv("ACL_CONFIG_DIR", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(shadow.parents[0]))
    return real, shadow


def _age(path: Path, seconds_ago: float) -> None:
    t = time.time() - seconds_ago
    os.utime(path, (t, t))


@pytest.mark.skipif(sys.platform != "win32", reason="package paths are Windows paths")
def test_shadow_dirs_are_discovered_under_the_real_appdata(tmp_path, monkeypatch):
    real, shadow = _layout(tmp_path, monkeypatch)
    assert paths.get_local_config_dir() == str(real)
    assert paths.get_shadow_config_dirs() == [str(shadow)]


@pytest.mark.skipif(sys.platform != "win32", reason="package paths are Windows paths")
def test_merge_copies_missing_files_and_never_overwrites_newer_data(tmp_path, monkeypatch):
    real, shadow = _layout(tmp_path, monkeypatch)
    (shadow / "account_notes.json").write_text('{"a": 1}', encoding="utf-8")  # only in the package copy
    (shadow / "goals.json").write_text("old-shadow", encoding="utf-8")
    (real / "goals.json").write_text("newer-real", encoding="utf-8")
    _age(shadow / "goals.json", 3600)
    (shadow / "registry.json").write_text("newer-shadow", encoding="utf-8")
    (real / "registry.json").write_text("older-real", encoding="utf-8")
    _age(real / "registry.json", 3600)
    (shadow / "logs").mkdir()
    (shadow / "logs" / "server.log").write_text("noise", encoding="utf-8")

    result = config_unify.prepare_config_dir()

    assert (real / "account_notes.json").read_text(encoding="utf-8") == '{"a": 1}'
    assert (real / "goals.json").read_text(encoding="utf-8") == "newer-real"
    assert (real / "registry.json").read_text(encoding="utf-8") == "newer-shadow"
    assert not (real / "logs").exists()
    assert result.copied == ["account_notes.json"]
    assert result.replaced == ["registry.json"]
    assert result.kept == ["goals.json"]
    backups = list((real / "backups").rglob("*"))
    names = {p.name for p in backups if p.is_file()}
    assert {"registry.json", "goals.json"} <= names  # both losing sides are preserved
    assert (shadow / "goals.json").read_text(encoding="utf-8") == "old-shadow"  # source untouched
    assert os.environ["ACL_CONFIG_DIR"] == str(real)


@pytest.mark.skipif(sys.platform != "win32", reason="package paths are Windows paths")
def test_env_files_are_merged_by_key_and_a_second_env_is_warned_about(tmp_path, monkeypatch, capsys):
    real, shadow = _layout(tmp_path, monkeypatch)
    (real / ".env").write_text("MLX_TOKEN=expired\nONLY_REAL=1\n", encoding="utf-8")
    _age(real / ".env", 3600)
    (shadow / ".env").write_text("MLX_TOKEN=fresh\nMEMORY_MB=512\n", encoding="utf-8")

    result = config_unify.prepare_config_dir()

    merged = dict(
        line.split("=", 1) for line in (real / ".env").read_text(encoding="utf-8").splitlines() if "=" in line
    )
    assert merged == {"MLX_TOKEN": "fresh", "ONLY_REAL": "1", "MEMORY_MB": "512"}
    assert result.env_merged == [".env"]
    assert (next((real / "backups").rglob(".env"))).read_text(encoding="utf-8").startswith("MLX_TOKEN=expired")
    # The package .env still differs from the merged one: the user is told.
    out = capsys.readouterr().out
    assert "settings folder" in out and "WARNING" in out and "different" in out
    assert any("different" in w for w in result.warnings)


@pytest.mark.skipif(sys.platform != "win32", reason="package paths are Windows paths")
def test_second_run_is_a_no_op(tmp_path, monkeypatch):
    real, shadow = _layout(tmp_path, monkeypatch)
    (shadow / "notes.json").write_text("x", encoding="utf-8")
    assert config_unify.prepare_config_dir().copied == ["notes.json"]
    again = config_unify.prepare_config_dir()
    assert not again.changed


def test_launcher_and_dashboard_agree_on_the_folder(monkeypatch):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packaging" / "windows"))
    import launcher

    monkeypatch.delenv("ACL_CONFIG_DIR", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", VIRTUAL)
    assert str(launcher._local_acl_dir()) == paths.get_local_config_dir()
    assert str(launcher._local_acl_dir()).endswith(os.path.join("AppData", "Local", "ACL"))
