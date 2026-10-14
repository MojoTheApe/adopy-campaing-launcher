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


def test_the_browser_files_are_spread_evenly_over_the_linux_shards():
    counts = [sum(1 for p in part if shard._BROWSER.search(p.read_text(encoding="utf-8", errors="replace")))
              for part in shard.shards(3, "linux")]
    assert max(counts) - min(counts) <= 3, counts


def test_costly_nonbrowser_addition_preserves_browser_balance(tmp_path, monkeypatch):
    # The cost-only greedy rule gives [5, 5, 5] before the addition and
    # [5, 7, 3] after it. These source fixtures are inspected, never executed.
    browser_costs = [60, 60, 90, 60, 46, 90, 75, 55, 47, 46, 90, 200, 90, 200, 60]
    plain_costs = [2, 100, 120, 60, 100, 80]
    files = []
    browser_files = set()
    for browser, costs in ((True, browser_costs), (False, plain_costs)):
        for index, value in enumerate(costs):
            path = tmp_path / f"test_{'browser' if browser else 'plain'}_{index:02}.py"
            extra = shard.BROWSER_EXTRA if browser else 0
            prefix = "from playwright.sync_api import sync_playwright\n" if browser else ""
            path.write_text(prefix + "".join(f"def test_{i}():\n    pass\n" for i in range(value-extra)), encoding="utf-8")
            assert shard.cost(path) == value
            files.append(path)
            if browser:
                browser_files.add(path)
    monkeypatch.setattr(platforms, "select", lambda platform: sorted(files))
    for added in (False, True):
        if added:
            diagnostic = tmp_path / "test_new_diagnostic.py"
            diagnostic.write_text("import subprocess\nsubprocess.run(['synthetic'])\n" +
                                  "".join(f"def test_{i}():\n    pass\n" for i in range(47)), encoding="utf-8")
            assert shard.cost(diagnostic) == 72
            files.append(diagnostic)
        parts = shard.shards(3, "linux")
        counts = [len(browser_files & set(part)) for part in parts]
        assert max(counts)-min(counts) <= 1, counts
        assert sorted(p for part in parts for p in part) == sorted(files)
        assert parts == shard.shards(3, "linux")
        loads = [sum(shard.cost(p) for p in part) for part in parts]
        assert max(loads)-min(loads) <= max(shard.cost(p) for p in files)


@pytest.mark.parametrize("platform", [None, "windows"])
def test_other_modes_keep_cost_only_assignment(tmp_path, monkeypatch, platform):
    # An expensive browser fixture deliberately differs from the Linux count
    # objective. Retained modes continue their existing minimum-cost split.
    files = []
    for name, tests, browser in (("large", 100, True), ("small_a", 1, True),
                                 ("small_b", 1, True), ("plain", 60, False)):
        path = tmp_path / f"test_{name}.py"
        path.write_text(("from playwright.sync_api import sync_playwright\n" if browser else "") +
                        "".join(f"def test_{i}():\n    pass\n" for i in range(tests)), encoding="utf-8")
        files.append(path)
    monkeypatch.setattr(shard, "test_files", lambda: sorted(files))
    monkeypatch.setattr(platforms, "select", lambda selected: sorted(files))
    # Largest cost145 -> bucket0; cost60 and both cost46 files -> bucket1.
    assert shard.shards(2, platform) == [sorted([files[0]]), sorted(files[1:])]
