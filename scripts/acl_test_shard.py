"""Split the test files into N CI shards (ACL-1397).

    python scripts/acl_test_shard.py 2 3      # prints the files of shard 2 of 3

The hosted Windows runner of a private repository has two CPUs, so
pytest-xdist alone gives CI almost nothing (measured on PR #409: 23-31 min
with `-n auto`, the same as serial). Each shard is its own job with its own
two CPUs; inside it xdist still runs two workers.

Every `tests/test_*.py` lands in exactly one shard (checked by
tests/test_acl_test_shard.py), so the shards together are the full suite.
Files are spread by a rough cost, largest first onto the lightest shard:
the number of tests in the file, and a flat extra for a file that starts a
real browser, Node or another process, because starting Chromium costs more
than most whole test files. Deterministic: the same tree gives the same split.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"

_TEST_DEF = re.compile(r"^\s*(?:async\s+)?def\s+test_", re.M)
_PARAM = re.compile(r"@pytest\.mark\.parametrize\(")
_HEAVY = re.compile(r"sync_playwright|chromium\.launch|subprocess\.(?:run|Popen)|TestClient\(")
HEAVY_EXTRA = 25


def test_files() -> list[Path]:
    return sorted(TESTS.glob("test_*.py"))


def cost(path: Path) -> int:
    text = path.read_text(encoding="utf-8", errors="replace")
    tests = len(_TEST_DEF.findall(text)) + 2 * len(_PARAM.findall(text))
    return max(1, tests) + (HEAVY_EXTRA if _HEAVY.search(text) else 0)


def shards(count: int) -> list[list[Path]]:
    if count < 1:
        raise ValueError("shard count must be at least 1")
    buckets: list[list[Path]] = [[] for _ in range(count)]
    load = [0] * count
    for path in sorted(test_files(), key=lambda p: (-cost(p), p.name)):
        i = min(range(count), key=lambda k: (load[k], k))
        buckets[i].append(path)
        load[i] += cost(path)
    return [sorted(b) for b in buckets]


def main(argv: list[str]) -> int:
    if len(argv) != 2 or not all(a.isdigit() for a in argv):
        print(__doc__.strip().splitlines()[2].strip(), file=sys.stderr)
        return 2
    index, count = int(argv[0]), int(argv[1])
    if not 1 <= index <= count:
        print(f"shard {index} is outside 1..{count}", file=sys.stderr)
        return 2
    for path in shards(count)[index - 1]:
        print(path.relative_to(ROOT).as_posix())
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
