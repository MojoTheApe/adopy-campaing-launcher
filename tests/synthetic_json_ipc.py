"""Bounded reads of mutable JSON shared only by controlled test processes.

Atomic replacement can briefly deny a Windows reader an open handle. Retry only
that sharing error; malformed/missing data and unrelated failures stay visible.
This does not read product configuration or change any filesystem permissions.
"""
import json
from pathlib import Path
import time

_READ_ATTEMPTS = 10
_READ_PAUSE = 0.05


def read_json(path):
    for attempt in range(_READ_ATTEMPTS):
        try:
            return json.loads(Path(path).read_text(encoding="utf-8"))
        except PermissionError:
            if attempt == _READ_ATTEMPTS - 1:
                raise
            time.sleep(_READ_PAUSE)
