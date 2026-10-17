#!/usr/bin/env python3
"""ACL operator entry point: preserve local adoption preflight before the exact shared client."""
from pathlib import Path
import runpy
import sys


def main():
    from acl_adoption import guard_tracker
    try:
        guard_tracker(sys.argv[1:])
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
    runpy.run_path(str(Path(__file__).resolve().with_name('task_tracker.py')), run_name='__main__')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
