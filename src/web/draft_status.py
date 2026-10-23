"""Current build verdicts: one locked transaction per account update.

The existing account -> verdict JSON shape is retained. Job identity fences late
writers; an earlier success is history, never a fallback for an unfinished run.
"""
from __future__ import annotations

import copy
import json
import threading
from pathlib import Path

from config.paths import _atomic_write, machine_file_lock

_lock = threading.RLock()
_failure_lock = threading.Lock()
_failed_writes: dict[tuple[str, str], tuple[dict, str | None, int]] = {}
_UNFINISHED = {"running"}


def _unverified(message: str, current: dict | None = None) -> dict:
    return {**(current or {}), "state": "unverified", "ts": (current or {}).get("ts", ""),
            "errors": [message]}


class Snapshot(dict):
    """Missing account entries carry a visible, fail-closed verdict to the UI."""

    def __init__(self, data: dict, error: str = ""):
        super().__init__(data)
        self.error = error

    def get(self, key, default=None):
        if key in self:
            return super().get(key)
        return _unverified(self.error or "No saved build result for this account.")


def _read(path: Path) -> dict:
    with path.open(encoding="utf-8") as stream:
        data = json.load(stream)
    if not isinstance(data, dict) or any(not isinstance(v, dict) for v in data.values()):
        raise ValueError("Invalid draft status structure")
    for current in data.values():
        records = [current]
        if "previous_ready" in current:
            if not isinstance(current["previous_ready"], dict):
                raise ValueError("Invalid draft history")
            records.append(current["previous_ready"])
        for record in records:
            for field in ("state", "ts", "job_id", "plan_revision"):
                if not isinstance(record.get(field, ""), str):
                    raise ValueError(f"Invalid draft {field}")
            for field in ("status_revision", "count"):
                value = record.get(field, 0)
                if type(value) is not int or value < 0:
                    raise ValueError(f"Invalid draft {field}")
            for field in ("campaign_names", "errors", "blocking_failures"):
                value = record.get(field, [])
                if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
                    raise ValueError(f"Invalid draft {field}")
    return data


def save(path: str, label: str, verdict: dict, *, begin: bool = False) -> str | None:
    """Return a visible error on failure; never discard unreadable account data."""
    target = Path(path)
    key = (str(target), label)
    observed_job_id = None
    observed_revision = 0
    try:
        with _lock, machine_file_lock(target.with_suffix(".lock"), timeout=2):
            try:
                data = _read(target)
            except FileNotFoundError:
                data = {}
            previous = data.get(label, {})
            observed_job_id = previous.get("job_id")
            observed_revision = previous.get("status_revision", 0)
            job_id = verdict.get("job_id")
            if job_id and not begin and previous.get("job_id") != job_id:
                # A late worker cannot replace the status of a newer run.
                if not previous.get("job_id"):
                    raise ValueError("Current build identity is missing")
                return None
            if (not begin and job_id and
                    previous.get("status_revision", 0) > verdict.get("status_revision", 0)):
                return None
            current = dict(verdict)
            history = previous.get("previous_ready")
            if begin and previous.get("state") == "ready":
                history = {k: v for k, v in previous.items() if k != "previous_ready"}
            if history:
                current["previous_ready"] = history
            data[label] = current
            _atomic_write(target, json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"))
            with _failure_lock:
                _failed_writes.pop(key, None)
        return None
    except (OSError, ValueError, TypeError) as exc:
        # Do not include filesystem paths or the corrupt document in the UI.
        message = f"Build status could not be saved ({type(exc).__name__}); result is unverified."
        with _failure_lock:
            _failed_writes[key] = (
                _unverified(message, verdict),
                observed_job_id if begin else verdict.get("job_id"),
                observed_revision if begin else verdict.get("status_revision", 0),
            )
        return message


def load(path: str, *, active_jobs: set[str]) -> Snapshot:
    target = Path(path)
    try:
        data = _read(target)
        error = ""
    except FileNotFoundError:
        data, error = {}, "Saved build status is missing; result is unverified."
    except (OSError, ValueError, TypeError) as exc:
        data, error = {}, f"Saved build status could not be read ({type(exc).__name__}); result is unverified."
    for current in data.values():
        # Legacy records may omit display-only fields introduced later.
        for record in (current, current.get("previous_ready", {})):
            for field, default in (("ts", ""), ("count", 0), ("campaign_names", [])):
                record.setdefault(field, default)
        if current.get("state") in _UNFINISHED and current.get("job_id") not in active_jobs:
            current["state"] = "interrupted"
            current["errors"] = ["This build did not record a final result; verify its partial drafts."]
    with _failure_lock:
        for (failed_path, label), (current, observed_job_id, observed_revision) in _failed_writes.items():
            if failed_path == str(target):
                stored = data.get(label, {})
                # A delayed failure from an older job/update cannot obscure a
                # successful newer verdict. Failed admission may mask only the
                # previous job/revision it observed, not a later result from
                # that same job or a subsequently accepted run.
                if (not stored.get("job_id") or
                        stored.get("job_id") == observed_job_id and
                        stored.get("status_revision", 0) <= observed_revision):
                    data[label] = copy.deepcopy(current)
    return Snapshot(data, error)
