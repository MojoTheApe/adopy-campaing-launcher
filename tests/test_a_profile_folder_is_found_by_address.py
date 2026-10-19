"""ACL-144: the Multilogin profile folder is addressed, not searched for.

The old lookup ran `rglob` over every Chrome profile (about ten seconds per
call). These tests count tree-walk calls instead of timing anything.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from src.fb_ads_rpa import browser as br
from src.analysis_agent import account_spend as sp


@pytest.fixture()
def mlx(tmp_path, monkeypatch):
    monkeypatch.setenv("MLX_HOME", str(tmp_path))
    return tmp_path / "profiles"


@pytest.fixture()
def walks(monkeypatch):
    """Counts every recursive scan; a scan is never expected to happen."""
    calls = {"rglob": 0, "glob": 0, "walk": 0}

    def _count(name):
        def _raise(*a, **k):
            calls[name] += 1
            raise AssertionError(f"recursive scan: {name}")

        return _raise

    monkeypatch.setattr(Path, "rglob", _count("rglob"))
    monkeypatch.setattr(Path, "glob", _count("glob"))
    monkeypatch.setattr(os, "walk", _count("walk"))
    return calls


def test_a_known_profile_resolves_by_its_address_without_a_scan(mlx, walks):
    wanted = mlx / "ws-1" / "folder-1" / "p-1"
    wanted.mkdir(parents=True)
    # deep Chrome content must never be visited
    (wanted / "Default" / "Cache" / "p-1").mkdir(parents=True)
    assert br._find_mlx_profile_dir("p-1", "folder-1") == wanted
    assert br._find_mlx_profile_dir("p-1") == wanted
    assert walks == {"rglob": 0, "glob": 0, "walk": 0}


def test_the_flat_layout_without_a_folder_resolves_too(mlx, walks):
    flat = mlx / "ws-1" / "p-2"
    flat.mkdir(parents=True)
    assert br._find_mlx_profile_dir("p-2") == flat
    assert sum(walks.values()) == 0


def test_another_profile_folder_is_never_picked(mlx, walks):
    (mlx / "ws-1" / "folder-1" / "p-10").mkdir(parents=True)
    (mlx / "ws-1" / "folder-1" / "p-1-copy").mkdir(parents=True)
    (mlx / "ws-2" / "folder-9" / "p-1").mkdir(parents=True)
    # exact name only: p-1 is neither p-10 nor p-1-copy
    assert br._find_mlx_profile_dir("p-1") == mlx / "ws-2" / "folder-9" / "p-1"
    # a known folder_id picks that profile even if the same name sits elsewhere
    (mlx / "ws-1" / "folder-1" / "p-1").mkdir()
    assert br._find_mlx_profile_dir("p-1", "folder-1") == mlx / "ws-1" / "folder-1" / "p-1"
    # a file with the profile's name is not a profile folder
    (mlx / "ws-1" / "folder-1" / "p-5").write_text("x")
    assert br._find_mlx_profile_dir("p-5") is None


def test_a_profile_deeper_than_the_known_layout_is_not_found(mlx, walks):
    (mlx / "ws-1" / "folder-1" / "nested" / "p-3").mkdir(parents=True)
    assert br._find_mlx_profile_dir("p-3") is None
    assert sum(walks.values()) == 0


def test_missing_root_and_missing_profile_fail_safely(tmp_path, monkeypatch, walks):
    monkeypatch.setenv("MLX_HOME", str(tmp_path / "absent"))
    assert br._find_mlx_profile_dir("p-1") is None
    monkeypatch.setenv("MLX_HOME", str(tmp_path))
    (tmp_path / "profiles" / "ws-1" / "folder-1").mkdir(parents=True)
    assert br._find_mlx_profile_dir("p-1", "folder-1") is None


@pytest.mark.parametrize("bad", ["", "  ", ".", "..", "../x", "a/b", "a\\b"])  # linux-ok: a backslash profile id given as data
def test_an_unsafe_profile_id_resolves_to_nothing(mlx, bad):
    (mlx / "ws-1" / "folder-1").mkdir(parents=True)
    assert br._find_mlx_profile_dir(bad) is None


def test_the_dashboard_uses_the_same_lookup(mlx, walks):
    wanted = mlx / "ws-1" / "folder-1" / "p-1"
    wanted.mkdir(parents=True)
    assert sp._mlx_profile_dir("p-1", "folder-1") == wanted
