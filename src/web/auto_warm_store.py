"""ACL-2268: one automatic warm-up attempt per physical profile per window, kept on disk.

The limit has to survive a dashboard restart and has to be per physical profile (two account labels can share
one profile), so it lives in a small file in the machine config directory. Unreadable or unwritable file ->
no attempt (fail closed): Publish and Check plan still open the profile themselves.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Callable

from config.paths import _atomic_write, get_local_config_dir, machine_file_lock

log = logging.getLogger(__name__)

FILE_NAME = "auto_warm_attempts.json"


def _path() -> Path:
    return Path(get_local_config_dir()) / FILE_NAME


def claim(profile_id: str, window_s: float, *, now: Callable[[], float] = time.time) -> bool:
    """True when this profile has had no attempt within `window_s`; the attempt is then recorded."""
    key = str(profile_id or "").strip().lower()
    if not key:
        return False
    path = _path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with machine_file_lock(path.with_suffix(".lock")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                data = {}
            if not isinstance(data, dict):
                return False
            stamp = float(now())
            data = {k: v for k, v in data.items()
                    if isinstance(v, (int, float)) and 0 <= stamp - float(v) < window_s}
            if key in data:
                return False
            data[key] = stamp
            _atomic_write(path, json.dumps(data, sort_keys=True).encode("utf-8"))
            return True
    except Exception:  # noqa: BLE001 - unreadable/unwritable: fail closed, nothing opens by itself
        log.warning("Automatic warm-up attempts could not be recorded; no automatic attempt")
        return False
