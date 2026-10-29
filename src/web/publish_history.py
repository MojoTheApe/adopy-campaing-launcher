"""ACL-2292: durable history of finished publish jobs.

`publish_jobs` keeps jobs in memory, so after a dashboard restart every earlier
job answered "Job not found". When a job ends (done / failed / cancelled) one
JSON line is appended to `<ACL config dir>/publish_history.jsonl`:

    {"v": 1, "job_id", "account_label", "country", "status", "created_at",
     "started_at", "finished_at", "build", "fb_backend", "total", "done_count",
     "publishable", "errors": [...],
     "results": [{"campaign_name", "success", "publishable", "status",
                  "campaign_id", "adset_id", "ad_id", "ad_ids", "creative_id",
                  "warnings", "blocking_failures", "error", "steps"}]}

Only these fields are copied (an allow-list), and every string goes through
`redact()`: tokens, cookies, passwords and URL query strings never reach the
file. A line is written with one append + flush; a crash can damage only the
last line, which readers skip. The file rotates at 20 MB, keeping 3 old files
(`.1` newest ... `.3` oldest).
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from typing import Any

from config.paths import get_publish_history_path

log = logging.getLogger(__name__)

MAX_BYTES = 20 * 1024 * 1024
KEEP_OLD = 3
RECENT_LIMIT = 50
_MAX_TEXT = 2000
_MAX_ITEMS = 200
_TAIL_BYTES = 4 * 1024 * 1024

_lock = threading.Lock()
_written: set[str] = set()   # one line per job even if two end paths fire


# ── redaction ─────────────────────────────────────────────────────────────
_COOKIE_PAIR = re.compile(
    r"\b(c_user|xs|datr|sb|fr|wd|presence|fbsr_\d+|sessionid|csrftoken|fbl_st|ps_l|ps_n)=[^;\s,\"']+", re.I)
_COOKIE_HEADER = re.compile(r"\b(set-cookie|cookie)\s*[:=]\s*[^\r\n]+", re.I)
_URL = re.compile(r"\b(https?://[^\s\"'<>?#]+)(?:[?#][^\s\"'<>]*)?", re.I)
_BEARER = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", re.I)
_TOKEN_PAIR = re.compile(
    r"\b(access[_-]?token|refresh[_-]?token|token|password|passwd|secret|client[_-]?secret|api[_-]?key|"
    r"fb_dtsg|lsd|authorization)(\"?\s*[:=]\s*\"?)[^\s\"'&,;}]+", re.I)
_EAA = re.compile(r"(?<![A-Za-z0-9])EAA[A-Za-z0-9]{20,}")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}(?:\.[A-Za-z0-9_-]*)?")


def redact(text: Any) -> str:
    """Remove tokens, cookies, passwords and URL query strings from `text`.

    Numeric ids (campaign, ad set, ad) are kept on purpose: they are the point
    of the history. The shared telemetry scrubber hides every 10+ digit number,
    so it is applied only for its secret / proxy patterns, never for digits.
    """
    out = str(text if text is not None else "")
    out = _URL.sub(lambda m: m.group(1), out)   # drop ?query and #fragment
    try:
        from src.fb_ads_rpa import run_telemetry as _t

        out = _t._hide_configured_secrets(out)
        for pattern, replacement in _t._SCRUB:
            src = pattern.pattern
            if "{10,}" in src or src.startswith("(act=)") or "[0-9a-f]{8}" in src:
                continue   # digits / act= / uuid: ids stay readable
            out = pattern.sub(replacement, out)
    except Exception:
        log.debug("telemetry scrubber unavailable", exc_info=True)
    out = _COOKIE_HEADER.sub(lambda m: m.group(1) + ": <hidden>", out)
    out = _COOKIE_PAIR.sub(lambda m: m.group(1) + "=<hidden>", out)
    out = _BEARER.sub("Bearer <hidden>", out)
    out = _TOKEN_PAIR.sub(lambda m: m.group(1) + m.group(2) + "<hidden>", out)
    out = _EAA.sub("<token>", out)
    out = _JWT.sub("<token>", out)
    return out[:_MAX_TEXT]


def _text(value: Any) -> str:
    return redact(value)


def _texts(values: Any) -> list[str]:
    if not isinstance(values, (list, tuple)):
        return []
    return [_text(v) for v in list(values)[:_MAX_ITEMS]]


def _id(value: Any) -> str | None:
    return None if value in (None, "") else _text(value)[:200]


def _campaign_status(result: dict[str, Any], publishable: bool) -> str:
    if result.get("success") is not True:
        return "failed"
    return "ready" if publishable else "blocked"


def _result_line(result: dict[str, Any]) -> dict[str, Any]:
    blocking = result.get("blocking_failures") or []
    publishable = (result.get("success") is True and result.get("publishable") is True and not blocking)
    ad_ids = result.get("ad_ids") or ([result["ad_id"]] if result.get("ad_id") else [])
    return {
        "campaign_name": _text(result.get("campaign_name")),
        "success": result.get("success") is True,
        "publishable": publishable,
        "status": _campaign_status(result, publishable),
        "campaign_id": _id(result.get("campaign_id")),
        "adset_id": _id(result.get("adset_id")),
        "ad_id": _id(result.get("ad_id")),
        "ad_ids": [_text(a)[:200] for a in list(ad_ids)[:_MAX_ITEMS]] if isinstance(ad_ids, (list, tuple)) else [],
        "creative_id": _id(result.get("creative_id")),
        "warnings": _texts(result.get("warnings")),
        "blocking_failures": _texts(blocking),
        "error": _text(result.get("error")) if result.get("error") else None,
        "steps": _texts(result.get("steps_completed")),
    }


def _build_id() -> str:
    try:
        from src.packaged_runtime import build_identity

        return str(build_identity().get("tag") or "source")
    except Exception:
        return "source"


def entry_from_job(job: dict[str, Any]) -> dict[str, Any]:
    results = [r for r in (job.get("results") or []) if isinstance(r, dict)]
    return {
        "v": 1,
        "job_id": str(job.get("job_id") or ""),
        "account_label": _text(job.get("account_label")),
        "country": _text(job.get("country")),
        "status": str(job.get("status") or ""),
        "created_at": job.get("created_at"),
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
        "build": _build_id(),
        "fb_backend": _text(job.get("fb_backend")),
        "total": int(job.get("total") or 0),
        "done_count": int(job.get("done_count") or 0),
        "publishable": bool(job.get("publishable")),
        "errors": _texts(job.get("errors")),
        "results": [_result_line(r) for r in results[:_MAX_ITEMS]],
    }


# ── file ──────────────────────────────────────────────────────────────────
def _old(path: str, n: int) -> str:
    return f"{path}.{n}"


def _rotate_locked(path: str) -> None:
    try:
        if os.path.getsize(path) < MAX_BYTES:
            return
    except OSError:
        return
    try:
        oldest = _old(path, KEEP_OLD)
        if os.path.exists(oldest):
            os.remove(oldest)
        for n in range(KEEP_OLD - 1, 0, -1):
            if os.path.exists(_old(path, n)):
                os.replace(_old(path, n), _old(path, n + 1))
        os.replace(path, _old(path, 1))
    except OSError:
        log.warning("ACL-2292: publish history rotation failed", exc_info=True)


def record_job(job: dict[str, Any]) -> bool:
    """Append one line for an ended job. Returns False when nothing was written."""
    job_id = str(job.get("job_id") or "")
    if not job_id:
        return False
    with _lock:
        if job_id in _written:
            return False
        line = json.dumps(entry_from_job(job), ensure_ascii=False, separators=(",", ":"))
        path = get_publish_history_path()
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            _rotate_locked(path)
            # A damaged last line (crash mid-write) has no newline; start a fresh line.
            lead = "\n" if _needs_newline(path) else ""
            with open(path, "a", encoding="utf-8", newline="\n") as fh:
                fh.write(lead + line + "\n")
                fh.flush()
                os.fsync(fh.fileno())
        except OSError:
            log.warning("ACL-2292: publish history was not written", exc_info=True)
            return False
        _written.add(job_id)
        if len(_written) > 5000:
            _written.clear()
        return True


def _needs_newline(path: str) -> bool:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            if fh.tell() == 0:
                return False
            fh.seek(-1, os.SEEK_END)
            return fh.read(1) != b"\n"
    except OSError:
        return False


def _read_entries(path: str, tail_bytes: int | None = None) -> list[dict[str, Any]]:
    """Entries of one file, oldest first; broken lines are skipped."""
    try:
        with open(path, "rb") as fh:
            partial = False
            if tail_bytes is not None:
                size = fh.seek(0, os.SEEK_END)
                if size > tail_bytes:
                    fh.seek(size - tail_bytes)
                    partial = True
                else:
                    fh.seek(0)
            else:
                fh.seek(0)
            raw = fh.read()
    except OSError:
        return []
    lines = raw.split(b"\n")
    if partial and lines:
        lines = lines[1:]   # first line is cut in the middle
    out: list[dict[str, Any]] = []
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        try:
            obj = json.loads(ln.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            continue
        if isinstance(obj, dict) and obj.get("job_id"):
            out.append(obj)
    return out


def _files_newest_first(path: str) -> list[str]:
    return [p for p in [path] + [_old(path, n) for n in range(1, KEEP_OLD + 1)] if os.path.exists(p)]


def find_job(job_id: str) -> dict[str, Any] | None:
    """The stored result of `job_id` in job-status shape, or None."""
    job_id = str(job_id or "")
    if not job_id:
        return None
    with _lock:
        for path in _files_newest_first(get_publish_history_path()):
            for entry in reversed(_read_entries(path)):
                if entry.get("job_id") == job_id:
                    return _as_status(entry)
    return None


def _as_status(entry: dict[str, Any]) -> dict[str, Any]:
    """Same keys the live `/publish-jobs/<id>` answer has, plus `from_history`."""
    out = dict(entry)
    out["results"] = [{**r, "steps_completed": list(r.get("steps") or [])} for r in entry.get("results") or []]
    out["campaign_names"] = [r.get("campaign_name", "") for r in out["results"]]
    out["creative_notices"] = []
    out["from_history"] = True
    return out


def recent(limit: int = RECENT_LIMIT) -> list[dict[str, Any]]:
    """Last `limit` finished jobs, newest first, as short summaries."""
    limit = max(1, min(int(limit or RECENT_LIMIT), 200))
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    with _lock:
        for i, path in enumerate(_files_newest_first(get_publish_history_path())):
            for entry in reversed(_read_entries(path, tail_bytes=_TAIL_BYTES if i == 0 else None)):
                jid = entry.get("job_id")
                if jid in seen:
                    continue
                seen.add(jid)
                found.append(_summary(entry))
                if len(found) >= limit:
                    return found
    return found


def _summary(entry: dict[str, Any]) -> dict[str, Any]:
    results = entry.get("results") or []
    return {
        "job_id": entry.get("job_id"),
        "account_label": entry.get("account_label"),
        "country": entry.get("country"),
        "status": entry.get("status"),
        "started_at": entry.get("started_at") or entry.get("created_at"),
        "finished_at": entry.get("finished_at"),
        "build": entry.get("build"),
        "publishable": bool(entry.get("publishable")),
        "total": entry.get("total", 0),
        "campaigns": [
            {"name": r.get("campaign_name"), "status": r.get("status"),
             "campaign_id": r.get("campaign_id")} for r in results[:20]],
        "errors": list(entry.get("errors") or [])[:3],
    }
