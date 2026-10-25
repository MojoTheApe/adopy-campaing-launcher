"""Split the test files into N CI shards (ACL-1397).

    python scripts/acl_test_shard.py 2 3      # prints the files of shard 2 of 3
    python scripts/acl_test_shard.py --platform linux 2 3    # shard 2 of 3 of the Linux job

The hosted Windows runner of a private repository has two CPUs, so
pytest-xdist alone gives CI almost nothing (measured on PR #409: 23-31 min
with `-n auto`, the same as serial). Each shard is its own job with its own
two CPUs; inside it xdist still runs two workers.

Every `tests/test_*.py` lands in exactly one shard (checked by
tests/test_acl_test_shard.py), so the shards together are the full suite.
Files are spread by a rough cost, largest first onto the lightest shard:
the number of tests in the file, and a flat extra for a file that starts a
real browser (more), Node or another process, because starting Chromium costs
more than most whole test files. Deterministic: the same tree gives the same split.

Linux first spreads browser files by browser count, cost and stable shard index;
the remaining files then fill the lightest shard. A costly non-browser addition
therefore cannot change the browser-count balance (ACL-1879).

ACL-1582: ``--platform linux`` shards only the files without the
``windows_only`` marker, ``--platform windows`` the marked files plus the
Windows smoke set (scripts/acl_test_platform.py). Without the flag every file
is sharded, as before.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

try:
    from scripts import acl_test_platform as platforms
except ImportError:  # запуск как python scripts/acl_test_shard.py
    import acl_test_platform as platforms

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"

_TEST_DEF = re.compile(r"^\s*(?:async\s+)?def\s+test_", re.M)
_PARAM = re.compile(r"@pytest\.mark\.parametrize\(")
_BROWSER = re.compile(r"sync_playwright|chromium\.launch")
_HEAVY = re.compile(r"subprocess\.(?:run|Popen)|TestClient\(")
HEAVY_EXTRA = 25
# ACL-1582: на Linux шард с большим числом файлов, запускающих Chromium, шёл
# 14 минут против 9 у остальных: запуск браузера дороже, чем другой процесс.
BROWSER_EXTRA = 45


def test_files() -> list[Path]:
    return sorted(TESTS.glob("test_*.py"))


def cost(path: Path) -> int:
    text = path.read_text(encoding="utf-8", errors="replace")
    tests = len(_TEST_DEF.findall(text)) + 2 * len(_PARAM.findall(text))
    extra = BROWSER_EXTRA if _BROWSER.search(text) else HEAVY_EXTRA if _HEAVY.search(text) else 0
    return max(1, tests) + extra


def shards(count: int, platform: str | None = None) -> list[list[Path]]:
    """``platform`` — None (все файлы), "linux" или "windows" (ACL-1582)."""
    if count < 1:
        raise ValueError("shard count must be at least 1")
    files = test_files() if platform is None else platforms.select(platform)
    buckets: list[list[Path]] = [[] for _ in range(count)]
    load = [0] * count
    if platform == "linux":
        browser_files, other_files = [], []
        for path in files:
            target = browser_files if _BROWSER.search(path.read_text(encoding="utf-8", errors="replace")) else other_files
            target.append(path)
        browser_count = [0] * count
        for path in sorted(browser_files, key=lambda p: (-cost(p), p.name)):
            i = min(range(count), key=lambda k: (browser_count[k], load[k], k))
            buckets[i].append(path)
            load[i] += cost(path)
            browser_count[i] += 1
        files = other_files
    for path in sorted(files, key=lambda p: (-cost(p), p.name)):
        i = min(range(count), key=lambda k: (load[k], k))
        buckets[i].append(path)
        load[i] += cost(path)
    if platform == "linux":
        # A larger screenshot regression can skew the cost-first split's
        # browser counts. Repair only that skew; keep all selected files and
        # choose the move with the smallest resulting cost imbalance.
        browser = {p for p in files if _BROWSER.search(p.read_text(encoding="utf-8", errors="replace"))}
        browser_counts = [sum(p in browser for p in bucket) for bucket in buckets]
        while max(browser_counts) - min(browser_counts) > 1:
            donor = max(range(count), key=lambda k: (browser_counts[k], load[k], -k))
            receiver = min(range(count), key=lambda k: (browser_counts[k], load[k], k))

            def move_cost(path):
                moved = load.copy()
                moved[donor] -= cost(path)
                moved[receiver] += cost(path)
                return max(moved) - min(moved), path.name

            path = min((p for p in buckets[donor] if p in browser), key=move_cost)
            buckets[donor].remove(path)
            buckets[receiver].append(path)
            load[donor] -= cost(path)
            load[receiver] += cost(path)
            browser_counts[donor] -= 1
            browser_counts[receiver] += 1
    return [sorted(b) for b in buckets]


def main(argv: list[str]) -> int:
    platform = None
    if argv[:1] == ["--platform"]:
        if len(argv) < 2 or argv[1] not in platforms.PLATFORMS:
            print(f"--platform must be one of {', '.join(platforms.PLATFORMS)}", file=sys.stderr)
            return 2
        platform, argv = argv[1], argv[2:]
    if len(argv) != 2 or not all(a.isdigit() for a in argv):
        print(__doc__.strip().splitlines()[2].strip(), file=sys.stderr)
        return 2
    index, count = int(argv[0]), int(argv[1])
    if not 1 <= index <= count:
        print(f"shard {index} is outside 1..{count}", file=sys.stderr)
        return 2
    parts = shards(count, platform)
    if not parts[index - 1]:
        # Пустой шард — ошибка конфигурации CI, а не «зелёный» пустой прогон.
        print(f"shard {index} of {count} has no test files", file=sys.stderr)
        return 1
    for path in parts[index - 1]:
        print(path.relative_to(ROOT).as_posix())
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
