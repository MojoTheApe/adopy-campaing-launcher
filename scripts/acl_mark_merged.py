"""Compatibility entry point for the portable, verified merge transition."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tracker.merge import (MERGED, REVIEW, REPOSITORY, GitHub, unchecked_subtasks,
                           related, validate_issue, prepare, apply, main)
if __name__ == "__main__":
    raise SystemExit(main())
