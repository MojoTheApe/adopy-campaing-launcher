"""ACL-2334: the MSIX package copy must not overwrite a real machine config it should not touch."""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import pytest

from config import config_unify, paths


# Windows-style paths as DATA for the env-text merge (platform-neutral string logic).
WIN_CLIENT = r"C:\cfg\oauth_client.json"  # linux-ok: data for the merge
WIN_A = r"C:\a.json"  # linux-ok: data for the merge
WIN_B = r"D:\b.json"  # linux-ok: data for the merge


def _age(path: Path, seconds_ago: float) -> None:
    t = time.time() - seconds_ago
    os.utime(path, (t, t))


def _layout(tmp_path):
    real = tmp_path / "real"
    shadow = tmp_path / "shadow"
    real.mkdir()
    shadow.mkdir()
    return real, shadow


def test_a_relative_path_never_replaces_an_absolute_one():
    real = f"GSHEET_OAUTH_CLIENT={WIN_CLIENT}\n"
    shadow = "GSHEET_OAUTH_CLIENT=config/oauth_client.json\n"
    text, keys = config_unify.merge_env_text_keys(real, shadow, shadow_newer=True)
    assert WIN_CLIENT in text and "config/oauth_client.json" not in text
    assert keys == []


def test_the_default_never_replaces_an_explicit_setting():
    text, keys = config_unify.merge_env_text_keys("ACL_MAX_PARALLEL_PROFILES=auto\n",
                                                  "ACL_MAX_PARALLEL_PROFILES=2\n", shadow_newer=True)
    assert "=auto" in text and keys == []


def test_a_legitimately_newer_value_still_merges():
    text, keys = config_unify.merge_env_text_keys(f"MLX_TOKEN=old\nGSHEET_OAUTH_CLIENT={WIN_A}\n",
                                                  f"MLX_TOKEN=new\nGSHEET_OAUTH_CLIENT={WIN_B}\n"
                                                  "ACL_MAX_PARALLEL_PROFILES=3\nNEW_KEY=1\n", shadow_newer=True)
    lines = dict(ln.split("=", 1) for ln in text.splitlines())
    assert lines == {"MLX_TOKEN": "new", "GSHEET_OAUTH_CLIENT": WIN_B,
                     "ACL_MAX_PARALLEL_PROFILES": "3", "NEW_KEY": "1"}
    assert sorted(keys) == ["ACL_MAX_PARALLEL_PROFILES", "GSHEET_OAUTH_CLIENT", "MLX_TOKEN", "NEW_KEY"]


def test_the_log_line_names_keys_and_never_values(tmp_path, caplog):
    real, shadow = _layout(tmp_path)
    (real / ".env").write_text("MLX_TOKEN=old-secret-value\n", encoding="utf-8")
    _age(real / ".env", 3600)
    (shadow / ".env").write_text("MLX_TOKEN=new-secret-value\n", encoding="utf-8")
    with caplog.at_level(logging.INFO, logger="acl.config_dir"):
        result = config_unify.merge_shadow_into(real, shadow)
    assert result.env_merged == [".env"]
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "MLX_TOKEN" in text and "secret-value" not in text


def test_the_shadow_is_not_merged_when_the_config_dir_is_explicit(tmp_path, monkeypatch):
    real, shadow = _layout(tmp_path)
    (shadow / ".env").write_text("ACL_MACHINE_LABEL=from-shadow\n", encoding="utf-8")
    (real / ".env").write_text("ACL_MACHINE_LABEL=real\n", encoding="utf-8")
    _age(real / ".env", 3600)
    monkeypatch.setenv("ACL_CONFIG_DIR", str(real))
    monkeypatch.setattr(paths, "get_shadow_config_dirs", lambda: [str(shadow)])
    assert paths.is_config_dir_explicit()
    result = config_unify.prepare_config_dir()
    assert not result.changed
    assert (real / ".env").read_text(encoding="utf-8") == "ACL_MACHINE_LABEL=real\n"


def test_the_default_folder_pinned_by_a_launcher_is_not_explicit(tmp_path, monkeypatch):
    monkeypatch.delenv("ACL_CONFIG_DIR", raising=False)
    assert not paths.is_config_dir_explicit()
    monkeypatch.setenv("ACL_CONFIG_DIR", paths.get_local_config_dir())
    assert not paths.is_config_dir_explicit()
    monkeypatch.setenv("ACL_CONFIG_DIR", str(tmp_path / "elsewhere"))
    assert paths.is_config_dir_explicit()
