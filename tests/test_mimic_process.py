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
    assert is_mimic_executable(MAC_MIMIC, "/Users/u/mlx", "darwin")
    assert is_mimic_executable("/Users/u/mlx/deps/x/Browser", "/Users/u/mlx", "darwin")
    assert not is_mimic_executable("/Users/u/other/Browser", "/Users/u/mlx", "darwin")
    assert not is_mimic_executable("", "/Users/u/mlx", "darwin")
    assert not is_mimic_executable(None, "/Users/u/mlx", "darwin")


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
    for platform in ("win32", "darwin"):
        assert not is_mimic_executable(MAC_CHROME, "/Users/u", platform)
        assert not is_mimic_executable(WIN_CHROME, "C:/Users/u/mlx", platform)
        assert not is_mimic_executable("/Users/u/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                                       "/Users/u", platform)
