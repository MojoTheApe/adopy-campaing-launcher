#!/usr/bin/env python3
"""Compatibility entry point; shared implementation is in .workflow/tools."""
from pathlib import Path
import runpy
import sys
if __name__ == '__main__':
    from acl_adoption import guard_tracker
    try:
        guard_tracker(sys.argv[1:])
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
_tools = Path(__file__).resolve().parents[1] / '.workflow/tools'
sys.path.insert(0, str(_tools))
globals().update(runpy.run_path(str(_tools / 'task_tracker.py'), run_name=__name__))
