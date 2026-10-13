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


# --- ACL-1582: a browser file weighs more, so Chromium-heavy files spread over the shards -----------

def test_a_file_that_launches_chromium_weighs_more_than_one_that_starts_a_process(tmp_path, monkeypatch):
    body = "def test_a():\n    pass\n\n\ndef test_b():\n    pass\n"
    plain = tmp_path / "test_plain.py"
    proc = tmp_path / "test_proc.py"
    browser = tmp_path / "test_browser.py"
    plain.write_text(body, encoding="utf-8")
    proc.write_text("import subprocess\n" + body + "subprocess.run(['x'])\n", encoding="utf-8")
    browser.write_text("from playwright.sync_api import sync_playwright\n" + body, encoding="utf-8")
    assert shard.cost(plain) == 2
    assert shard.cost(proc) == 2 + shard.HEAVY_EXTRA
    assert shard.cost(browser) == 2 + shard.BROWSER_EXTRA
    assert shard.cost(plain) < shard.cost(proc) < shard.cost(browser)


def test_a_large_regression_does_not_skew_linux_browser_counts(tmp_path, monkeypatch):
    files = []
    for index, tests in enumerate((220, 45, 30, 20, 12, 8, 5, 3, 2)):
        path = tmp_path / f"test_browser_{index}.py"
        path.write_text("from playwright.sync_api import sync_playwright\n" +
                        "\n".join(f"def test_{n}(): pass" for n in range(tests)), encoding="utf-8")
        files.append(path)
    plain = tmp_path / "test_large_plain.py"
    plain.write_text("\n".join(f"def test_{n}(): pass" for n in range(300)), encoding="utf-8")
    files.append(plain)
    monkeypatch.setattr(platforms, "select", lambda platform: files)
    parts = shard.shards(3, "linux")
    counts = [sum(p != plain for p in part) for part in parts]
    assert max(counts) - min(counts) <= 1
    assert sorted(p for part in parts for p in part) == sorted(files)
    assert parts == shard.shards(3, "linux")


def test_the_browser_files_are_spread_evenly_over_the_linux_shards():
    counts = [sum(1 for p in part if shard._BROWSER.search(p.read_text(encoding="utf-8", errors="replace")))
              for part in shard.shards(3, "linux")]
    assert max(counts) - min(counts) <= 3, counts
