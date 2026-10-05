"""CI shards together are exactly the full suite (ACL-1397)."""
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import acl_test_platform as platforms  # noqa: E402
from scripts import acl_test_shard as shard  # noqa: E402


def test_every_test_file_is_in_exactly_one_shard():
    everything = shard.test_files()
    assert Path(__file__).resolve() in everything
    for count in (1, 2, 3, 4):
        parts = shard.shards(count)
        assert len(parts) == count
        flat = [p for part in parts for p in part]
        assert sorted(flat) == everything, f"{count} shards lose or repeat a file"
        assert all(parts), f"an empty shard among {count}"


def test_the_split_is_deterministic_and_roughly_even():
    first, second = shard.shards(3), shard.shards(3)
    assert first == second
    loads = [sum(shard.cost(p) for p in part) for part in first]
    assert max(loads) - min(loads) <= max(shard.cost(p) for p in shard.test_files())


def test_the_command_prints_repo_relative_paths():
    out = subprocess.run([sys.executable, str(ROOT / "scripts/acl_test_shard.py"), "1", "3"],
                         capture_output=True, text=True, check=True).stdout.split()
    assert out and all(line.startswith("tests/test_") and line.endswith(".py") for line in out)
    assert out == [p.relative_to(ROOT).as_posix() for p in shard.shards(3)[0]]


def test_a_shard_outside_the_range_is_refused():
    r = subprocess.run([sys.executable, str(ROOT / "scripts/acl_test_shard.py"), "4", "3"],
                       capture_output=True, text=True)
    assert r.returncode == 2 and not r.stdout


# --- ACL-1582: shards of the Linux and the Windows job -------------------------------------------

@pytest.mark.parametrize("platform", ["linux", "windows"])
def test_platform_shards_are_exactly_that_platforms_selection(platform):
    for count in (1, 2, 3):
        flat = [p for part in shard.shards(count, platform) for p in part]
        assert sorted(flat) == platforms.select(platform)


def test_no_marked_file_lands_in_a_linux_shard():
    marked = set(platforms.windows_files())
    for part in shard.shards(3, "linux"):
        assert not marked & set(part)


def test_the_platform_flag_selects_files_on_the_command_line():
    out = subprocess.run([sys.executable, str(ROOT / "scripts/acl_test_shard.py"), "--platform", "windows", "1", "1"],
                         capture_output=True, text=True, check=True).stdout.split()
    assert out == [p.relative_to(ROOT).as_posix() for p in platforms.select("windows")]
    assert "tests/test_windows_launcher_contract.py" in out


@pytest.mark.parametrize("args", [["--platform"], ["--platform", "macos", "1", "3"], ["--platform", "linux", "1"]])
def test_a_bad_platform_call_is_refused(args):
    r = subprocess.run([sys.executable, str(ROOT / "scripts/acl_test_shard.py"), *args], capture_output=True, text=True)
    assert r.returncode == 2 and not r.stdout


def test_an_empty_shard_fails_instead_of_passing_with_no_tests(monkeypatch, capsys):
    one = platforms.select("windows")[:1]
    monkeypatch.setattr(platforms, "select", lambda platform: one)
    assert shard.main(["--platform", "windows", "2", "2"]) == 1
    assert "no test files" in capsys.readouterr().err
