"""Единое распознавание Mimic: обе платформы проверяются на любой ОС (параметр platform)."""
from __future__ import annotations

from pathlib import Path

from src.fb_ads_rpa.mimic_process import (
    is_browser_process_name,
    is_mimic_executable,
    is_root_mimic_name,
    mimic_executable_candidates,
    mimic_version_dir,
)

MAC_MIMIC = "/Users/u/mlx/deps/mimic_150.4/Mimic.app/Contents/MacOS/Mimic"
MAC_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

WIN_CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"  # linux-ok: строка пути Windows — только данные для сравнения


def test_win_browser_name() -> None:
    assert is_browser_process_name("chrome.exe", "win32")
    assert not is_browser_process_name("Mimic", "win32")
    assert not is_browser_process_name(None, "win32")


def test_win_root_name() -> None:
    assert is_root_mimic_name("chrome.exe", "win32")
    assert not is_root_mimic_name("chrome", "win32")


def test_win_candidates(tmp_path: Path) -> None:
    exe = tmp_path / "deps" / "mimic_150.4" / "chrome.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    found = mimic_executable_candidates(tmp_path / "deps", "win32")
    assert found == [exe]
    assert mimic_version_dir(exe) == exe.parent


def test_mac_browser_names() -> None:
    for name in ("Mimic", "Chromium", "Google Chrome"):
        assert is_browser_process_name(name, "darwin")
    assert not is_browser_process_name("Finder", "darwin")
    assert is_root_mimic_name("Mimic", "darwin")
    assert not is_root_mimic_name("Finder", "darwin")


def test_mac_mimic_executable() -> None:
    assert is_mimic_executable(MAC_MIMIC, "/Users/u/mlx")
    assert is_mimic_executable("/Users/u/mlx/deps/x/Browser", "/Users/u/mlx")
    assert not is_mimic_executable("/Users/u/other/Browser", "/Users/u/mlx")
    assert not is_mimic_executable("", "/Users/u/mlx")
    assert not is_mimic_executable(None, "/Users/u/mlx")


def test_mac_candidates(tmp_path: Path) -> None:
    deps = tmp_path / "deps"
    mimic = deps / "mimic_150.4" / "Mimic.app" / "Contents" / "MacOS" / "Mimic"
    helper = (deps / "mimic_150.4" / "Mimic.app" / "Contents" / "Frameworks"
              / "X Helper.app" / "Contents" / "MacOS" / "X Helper")
    for f in (mimic, helper):
        f.parent.mkdir(parents=True)
        f.write_bytes(b"")
    assert mimic_executable_candidates(deps, "darwin") == [mimic]
    assert mimic_version_dir(mimic) == deps / "mimic_150.4"
    assert mimic_version_dir(mimic).name == "mimic_150.4"


def test_ordinary_chrome_is_never_mimic() -> None:
    assert not is_mimic_executable(MAC_CHROME, "/Users/u/mlx")
    assert not is_mimic_executable(WIN_CHROME, "C:/Users/u/mlx")
    assert not is_mimic_executable(
        "/Users/u/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "/Users/u/mlx")


def test_mac_mimic_may_be_named_google_chrome() -> None:
    exe = "/Users/u/mlx/deps/mimic_150/Google Chrome.app/Contents/MacOS/Google Chrome"
    assert is_mimic_executable(exe, "/Users/u/mlx")


def test_applications_chrome_is_not_mimic() -> None:
    assert not is_mimic_executable(MAC_CHROME, "/Users/u/mlx")


def test_sibling_dir_with_mlx_prefix_is_not_mlx_home() -> None:
    assert not is_mimic_executable("/Users/u/mlx-old/x/chrome", "/Users/u/mlx")


def test_path_inside_mlx_home_without_mimic_word() -> None:
    assert is_mimic_executable("/Users/u/mlx/deps/browser_150/Browser", "/Users/u/mlx")


def _proc(exe: str, udd: str) -> object:
    from types import SimpleNamespace

    return SimpleNamespace(info={"exe": exe, "cmdline": [exe, f"--user-data-dir={udd}",
                                                         "--client-session-id=abc"]})


def test_root_profile_call_site_on_mac(monkeypatch, tmp_path) -> None:
    import sys

    from src.web import profile_slots

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("MLX_HOME", str(tmp_path / "mlx"))
    wanted = {"abc": "Doc1"}
    chrome = _proc(MAC_CHROME, "/x/profiles/abc")
    inside = _proc(str(tmp_path / "mlx" / "deps" / "mimic_150" / "Browser"), "/x/profiles/abc")
    assert profile_slots._root_profile(chrome, wanted) is None
    assert profile_slots._root_profile(inside, wanted) == "Doc1"
