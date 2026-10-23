"""Seed the team library repository from this machine's targets and ad texts (ACL-1241).

Run once on the machine whose library becomes the team's starting point:

    python scripts/seed_team_library.py --dry-run
    python scripts/seed_team_library.py

An already seeded library is left alone. On another machine, ``--add-missing``
uploads only what the team library does not have yet: a name that is neither in
the library nor in Recently deleted, and not the same set of values under
another name.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import paths  # noqa: E402
from src.analysis_agent import team_library as tl  # noqa: E402

from src.analysis_agent.team_library_seed import KINDS, plan, seed  # noqa: E402,F401


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--add-missing", action="store_true", help="upload only what the team library lacks")
    ap.add_argument("--dry-run", action="store_true", help="show what would be uploaded, change nothing")
    args = ap.parse_args(argv)
    paths.load_env()
    if not tl.configured():
        print("The team library is not connected. Open Targeting or Ad titles in the dashboard "
              "and choose Connect team library.", file=sys.stderr)
        return 2
    for kind in KINDS:
        try:
            n, skipped = seed(kind, add_missing=args.add_missing, dry_run=args.dry_run)
        except tl.LibraryError as exc:
            print(f"{kind}: {exc}", file=sys.stderr)
            return 1
        print(f"{kind}: {'would upload' if args.dry_run else 'uploaded'} {n}")
        for line in skipped:
            print(f"  skipped {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
