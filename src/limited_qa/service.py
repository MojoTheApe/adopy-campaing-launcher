"""Submit, persist, reconcile and poll Limited_QA requests.

One manager submission keeps one request_id / idempotency_key / body for its
whole life. The record (with the exact body) is saved *before* the first POST;
after a lost response we GET first and re-POST the identical body only on 404.
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config.paths import get_local_config_dir, machine_file_lock
from src.limited_qa import client

STORE_NAME = "limited_qa_requests.json"
TERMINAL = {"succeeded", "mixed", "failed", "rejected"}
POLL_INTERVAL_S = 20
POLL_MAX_BACKOFF_S = 120
_FIELDS = ("status", "jira_status", "dispatch_state", "result_urls", "result_items", "error",
           "updated_at", "completed_at", "jira_issue_key", "jira_issue_url")

_lock = threading.RLock()
_poller: threading.Thread | None = None
_stop = threading.Event()


def _path() -> Path:
    return Path(get_local_config_dir()) / STORE_NAME


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load() -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(data: dict[str, dict[str, Any]]) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def _update(request_id: str, **changes: Any) -> dict[str, Any]:
    with _lock, machine_file_lock(_path().with_suffix(".lock")):
        data = _load()
        rec = data[request_id]
        rec.update(changes)
        rec["local_updated_at"] = _now()
        _save(data)
        return dict(rec)


def get(request_id: str) -> dict[str, Any] | None:
    rec = _load().get(request_id)
    return dict(rec) if rec else None


def list_requests() -> list[dict[str, Any]]:
    return sorted(_load().values(), key=lambda r: r.get("created_local", ""), reverse=True)


def _merge_remote(request_id: str, body: dict[str, Any]) -> dict[str, Any]:
    changes = {k: body[k] for k in _FIELDS if k in body}
    if not changes.get("status"):
        changes.pop("status", None)
    return _update(request_id, **changes)


# ------------------------------------------------------------------ submit --

def submit(source_urls: Any, target_domain: Any, country: Any, facebook_account: Any = None) -> dict[str, Any]:
    """Validate, persist, then POST. Raises ValidationError / NotConfigured before any record exists."""
    if not client.is_configured():
        raise client.NotConfigured("Limited_QA is not configured on this machine.")
    rid = client.new_request_id()
    body = client.build_body(rid, source_urls, target_domain, country, facebook_account)
    with _lock, machine_file_lock(_path().with_suffix(".lock")):
        data = _load()
        data[rid] = {
            "request_id": rid, "idempotency_key": body["idempotency_key"], "body": body,
            "status": "submitting", "result_urls": [], "result_items": [], "error": None,
            "created_local": _now(), "local_updated_at": _now(),
        }
        _save(data)
    return _send(rid)


def _send(request_id: str) -> dict[str, Any]:
    rec = get(request_id)
    try:
        reply = client.post_request(rec["body"])
    except client.Unreachable:
        return _update(request_id, error="Could not reach the service; will retry safely.")
    return _after_post(request_id, reply)


def _after_post(request_id: str, reply: client.Reply) -> dict[str, Any]:
    if reply.http in (200, 202) and reply.body.get("status"):
        return _merge_remote(request_id, {**reply.body, "error": reply.body.get("error")})
    if reply.http == 400:
        return _update(request_id, status="rejected", error="The service rejected this request. Check the input.")
    if reply.http == 502:
        return _update(request_id, status="failed", error="The service could not create the task.",
                       completed_at=_now())
    # 503 and anything unexpected: same identity, reconcile later.
    return _update(request_id, error="The service is temporarily unavailable; will retry safely.")


# ----------------------------------------------------------------- refresh --

def refresh(request_id: str) -> dict[str, Any] | None:
    """GET status. A 404 on a request we could not confirm re-POSTs the same body."""
    rec = get(request_id)
    if not rec or rec.get("status") in TERMINAL:
        return rec
    try:
        reply = client.get_status(request_id)
    except client.Unreachable:
        return _update(request_id, error="Could not reach the service; will retry.")
    if reply.http == 200:
        return _merge_remote(request_id, reply.body)
    if reply.http == 404:
        if rec.get("status") == "submitting":
            return _send(request_id)
        return _update(request_id, error="The service has no record of this request.")
    if reply.http == 400:
        return _update(request_id, error="The service refused the status check (credentials?).")
    return _update(request_id, error="The service is temporarily unavailable; will retry.")


# ----------------------------------------------------------------- polling --

def _poll_loop() -> None:
    delay = POLL_INTERVAL_S
    while not _stop.wait(delay):
        pending = [r["request_id"] for r in list_requests() if r.get("status") not in TERMINAL]
        failed = False
        for rid in pending:
            try:
                rec = refresh(rid)
            except Exception:  # one bad record must not stop the poller
                failed = True
                continue
            if rec and rec.get("error") and rec.get("status") not in TERMINAL and "reach" in rec["error"]:
                failed = True
        delay = min(delay * 2, POLL_MAX_BACKOFF_S) if failed else POLL_INTERVAL_S


def start_poller() -> None:
    global _poller
    with _lock:
        if _poller and _poller.is_alive():
            return
        _stop.clear()
        _poller = threading.Thread(target=_poll_loop, name="limited-qa-poller", daemon=True)
        _poller.start()


def stop_poller() -> None:
    _stop.set()
