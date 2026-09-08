"""Background publish job store and runner.

Keeps an in-memory dict of running/completed publish jobs.
Each job runs in a daemon thread so the web server stays responsive.
Progress is streamed by appending to job["results"] as campaigns complete.

The campaign-manager dashboard is draft-only and uses RPA (Playwright + Ads
Manager UI).  The former Marketing API backend is intentionally not reachable
from this module: it could create live ACTIVE objects from client-controlled
form fields and violated the product's manual-final-publish boundary.
"""
from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone
from typing import Any

from config.paths import get_draft_status_path
from src.fb_ads_rpa.config import use_rpa_publish
from src.fb_ads_rpa.draft import RpaPublishResult
from src.fb_ads_rpa.rpa_publisher import RpaPublisher
from src.fb_ads_rpa.operations import browser_operation

# ── In-memory job store ────────────────────────────────────────────────
_jobs: dict[str, dict[str, Any]] = {}
_jobs_lock = threading.Lock()
# Serialises actual publishing across jobs/accounts — see _run_job.
_publish_serial = browser_operation
_active_publishers: dict[str, RpaPublisher] = {}

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _job_result_from_rpa(result: RpaPublishResult) -> dict[str, Any]:
    d = result.to_job_dict()
    d["ts"] = _now_iso()
    return d


def create_job(
    account_label: str,
    country: str,
    slots: list[dict[str, Any]],
    *,
    via_multilogin: bool = True,
    activate: bool = False,
    dry_run: bool = False,
    hand_off: bool = False,
    total: int | None = None,
) -> str:
    """Create a new publish job, store it, return its job_id."""
    if activate:
        raise ValueError("ACL is draft-only: live activation is disabled")
    if not use_rpa_publish():
        raise RuntimeError(
            "The legacy Meta API backend is disabled in the campaign-manager dashboard"
        )
    # Campaign-manager jobs always leave the verified draft open for a human.
    hand_off = True
    job_id = str(uuid.uuid4())
    with _jobs_lock:
        _jobs[job_id] = {
            "job_id": job_id,
            "account_label": account_label,
            "country": country,
            "via_multilogin": via_multilogin,
            "activate": activate,
            "dry_run": dry_run,
            "hand_off": hand_off,
            "backend": "rpa" if use_rpa_publish() else "api",
            "status": "pending",
            "created_at": _now_iso(),
            "started_at": None,
            "finished_at": None,
            "results": [],
            "errors": [],
            "total": total if total is not None else len(slots),
            "done_count": 0,
            "cancel_requested": False,
        }
    return job_id


def cancel_job(job_id: str) -> dict[str, Any]:
    """Request cancellation of a running/pending job.

    Stops RPA and disconnects CDP; Multilogin window stays open for FB login check.
    """
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None:
            return {"ok": False, "error": "Job not found"}
        if job["status"] in ("done", "cancelled"):
            return {"ok": False, "error": f"Job already {job['status']}"}
        job["cancel_requested"] = True
        # Surface immediate feedback in the poll UI before the worker finishes.
        if job["status"] in ("pending", "running"):
            job["status"] = "cancelling"
        publisher = _active_publishers.get(job_id)

    if publisher is not None:
        try:
            publisher.cancel()
        except Exception:
            pass

    return {"ok": True, "job_id": job_id}


def active_job_count() -> int:
    """Jobs still working. An ACL update restarts the server, and a restart in
    the middle of a run leaves a half-built campaign in Ads Manager with nobody
    watching — so the update button asks this first."""
    with _jobs_lock:
        return sum(
            1 for job in _jobs.values()
            if job.get("status") in {"pending", "running", "cancelling"}
        )


def _is_cancelled(job_id: str) -> bool:
    with _jobs_lock:
        job = _jobs.get(job_id)
        return bool(job and job.get("cancel_requested"))


def get_job(job_id: str) -> dict[str, Any] | None:
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None:
            return None
        # Deep-copy mutable lists so callers see a stable snapshot
        snap = dict(job)
        snap["results"] = list(job["results"])
        snap["errors"] = list(job["errors"])
        expected = int(snap.get("total") or 0)
        snap["publishable"] = (
            snap.get("status") == "done"
            and expected > 0
            and int(snap.get("done_count") or 0) == expected
            and len(snap["results"]) == expected
            and not snap["errors"]
            and all(_result_is_publishable(result) for result in snap["results"])
        )
        return snap


def _clear_inherited_asyncio_loop() -> None:
    """Clear asyncio running-loop marks before sync Playwright in a worker thread.

    Also stop any leaked sync Playwright driver from a prior failed CDP attach
    in this thread (that leaves get_running_loop() returning a running loop and
    triggers 'Playwright Sync API inside the asyncio loop' on the next start).
    """
    try:
        from src.fb_ads_rpa.browser import _reset_asyncio_running_loop, _stop_thread_playwright

        _stop_thread_playwright()
        _reset_asyncio_running_loop()
    except Exception:
        try:
            import asyncio
            import asyncio.events as _ae

            _rl = getattr(_ae, "_running_loop", None)
            if _rl is not None and hasattr(_rl, "set"):
                _rl.set(None)
            elif _rl is not None and hasattr(_rl, "loop_pid"):
                _rl.loop_pid = (None, None)
            setter = getattr(_ae, "_set_running_loop", None)
            if callable(setter):
                setter(None)
        except Exception:
            pass


def _run_job(
    job_id: str,
    slots: list[dict[str, Any]],
    *,
    account_label: str,
    country: str,
    via_multilogin: bool,
    activate: bool,
    dry_run: bool = False,
    hand_off: bool = False,
) -> None:
    """Runs in a daemon thread. Updates _jobs[job_id] as it progresses."""
    # Must be first: clear any asyncio event loop inherited from the parent
    # async handler (Python 3.14+ ContextVar propagation via threading.Thread).
    _clear_inherited_asyncio_loop()

    # One publish at a time, account-agnostic. Multi-account support
    # (2026-07-31, user request) works by QUEUEING: launching builders for
    # two accounts starts two jobs, and the second waits here in "queued"
    # until the first finishes — never two Multilogin browsers driven at
    # once. Parallel profiles are a later, deliberate step, not a default.
    # The semaphore is held for the WHOLE run, not just the status flip.
    with _jobs_lock:
        _jobs[job_id]["status"] = "queued"
    with _publish_serial:
        if _is_cancelled(job_id):
            _finish(job_id, cancelled=True)
            return
        with _jobs_lock:
            _jobs[job_id]["status"] = "running"
            _jobs[job_id]["started_at"] = _now_iso()

        if activate or not use_rpa_publish():
            _fatal(job_id, "ACL safety gate: only draft-only RPA jobs are allowed")
            return
        _run_rpa(
            job_id,
            slots,
            account_label=account_label,
            country=country,
            activate=False,
            dry_run=dry_run,
            hand_off=True,
        )
        return


def _append_result(job_id: str, res: dict[str, Any]) -> None:
    with _jobs_lock:
        _jobs[job_id]["results"].append(res)
        _jobs[job_id]["done_count"] += 1


def _fatal(job_id: str, msg: str) -> None:
    with _jobs_lock:
        _jobs[job_id]["errors"].append(msg)
        _jobs[job_id]["status"] = "done"
        _jobs[job_id]["finished_at"] = _now_iso()
        account_label = _jobs[job_id].get("account_label", "")
        results = list(_jobs[job_id].get("results", []))
        errors = list(_jobs[job_id].get("errors", []))
        expected_total = int(_jobs[job_id].get("total") or 0)
    _write_draft_status(account_label, results, errors, expected_total=expected_total)


def _result_is_publishable(result: dict[str, Any]) -> bool:
    """Fail closed when a producer omitted the explicit publishability verdict."""
    return (
        result.get("success") is True
        and result.get("publishable") is True
        and not (result.get("blocking_failures") or [])
    )


def _summarize_results(
    results: list[dict],
    errors: list[str] | None = None,
    *,
    expected_total: int | None = None,
) -> dict[str, Any]:
    """Build the one persistent verdict the dashboard may display after restart."""
    errors = [str(e) for e in (errors or []) if str(e).strip()]
    publishable = [r for r in results if _result_is_publishable(r)]
    blocked = [r for r in results if r.get("success") is True and not _result_is_publishable(r)]
    failed = [r for r in results if r.get("success") is not True]
    expected_matches = expected_total is None or (
        expected_total > 0 and len(results) == expected_total
    )
    ready = (
        bool(results)
        and expected_matches
        and len(publishable) == len(results)
        and not errors
    )
    blocking_failures = [
        str(failure)
        for result in blocked
        for failure in (result.get("blocking_failures") or [])
    ]
    return {
        "state": "ready" if ready else "blocked",
        "ts": _now_iso(),
        "count": len(results),
        "publishable_count": len(publishable),
        "blocked_count": len(blocked),
        "failed_count": len(failed),
        "expected_count": expected_total,
        "campaign_names": [str(r.get("campaign_name") or "?") for r in results],
        "blocking_failures": blocking_failures,
        "errors": errors,
    }


def _write_draft_status(
    account_label: str,
    results: list[dict],
    errors: list[str] | None = None,
    *,
    expected_total: int | None = None,
) -> None:
    """Persist a truthful verdict in durable machine-local state."""
    import json as _json
    from pathlib import Path as _Path

    if not account_label:
        return
    path = _Path(get_draft_status_path())
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        existing: dict = {}
        if path.is_file():
            try:
                with open(path, encoding="utf-8") as f:
                    existing = _json.load(f)
            except Exception:
                existing = {}
        existing[account_label] = _summarize_results(
            results,
            errors,
            expected_total=expected_total,
        )
        temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        with open(temp_path, "w", encoding="utf-8") as f:
            _json.dump(existing, f, ensure_ascii=False, indent=2)
            f.flush()
            import os as _os

            _os.fsync(f.fileno())
        temp_path.replace(path)
    except Exception:
        pass  # Non-fatal


def _finish(job_id: str, *, cancelled: bool = False) -> None:
    with _jobs_lock:
        _jobs[job_id]["status"] = "cancelled" if cancelled else "done"
        _jobs[job_id]["finished_at"] = _now_iso()
        account_label = _jobs[job_id].get("account_label", "")
        results = list(_jobs[job_id].get("results", []))
        errors = list(_jobs[job_id].get("errors", []))
        expected_total = int(_jobs[job_id].get("total") or 0)
        _active_publishers.pop(job_id, None)
    if not cancelled:
        _write_draft_status(
            account_label,
            results,
            errors,
            expected_total=expected_total,
        )


def _run_rpa(
    job_id: str,
    slots: list[dict[str, Any]],
    *,
    account_label: str,
    country: str,
    activate: bool,
    dry_run: bool = False,
    hand_off: bool = False,
) -> None:
    publisher = RpaPublisher(dry_run=dry_run, hand_off=hand_off)
    with _jobs_lock:
        _active_publishers[job_id] = publisher
    try:
        drafts = publisher.build_drafts(
            slots,
            account_label=account_label,
            country=country,
            activate=activate,
            preview_mode=False,
        )
    except Exception as e:
        _fatal(job_id, f"Failed to build campaign drafts: {e}")
        with _jobs_lock:
            _active_publishers.pop(job_id, None)
        return

    with _jobs_lock:
        _jobs[job_id]["total"] = len(drafts)

    if not drafts:
        _fatal(job_id, "No campaigns to publish.")
        with _jobs_lock:
            _active_publishers.pop(job_id, None)
        return

    ready: list[Any] = []
    for draft in drafts:
        if _is_cancelled(job_id):
            break
        if draft.errors:
            _append_result(
                job_id,
                _job_result_from_rpa(
                    RpaPublishResult(
                        campaign_name=draft.name,
                        success=False,
                        error="; ".join(draft.errors),
                    )
                ),
            )
        else:
            ready.append(draft)

    if _is_cancelled(job_id):
        with _jobs_lock:
            _jobs[job_id]["errors"].append("Остановлено пользователем")
        # Never stop Multilogin — user needs the window to verify FB login.
        publisher.close(force=False)
        _finish(job_id, cancelled=True)
        return

    if not ready:
        _finish(job_id)
        return

    cancelled = False
    try:
        for draft in ready:
            if _is_cancelled(job_id):
                cancelled = True
                break
            result = publisher.publish_draft(draft)
            _append_result(job_id, _job_result_from_rpa(result))
    except Exception as e:
        if _is_cancelled(job_id):
            cancelled = True
            with _jobs_lock:
                _jobs[job_id]["errors"].append("Остановлено пользователем")
        else:
            with _jobs_lock:
                _jobs[job_id]["errors"].append(f"RPA error: {e}")
    finally:
        # Disconnect CDP only; Multilogin window stays open for FB login check.
        publisher.close(force=False)
        _finish(job_id, cancelled=cancelled or _is_cancelled(job_id))


def start_job(
    account_label: str,
    country: str,
    slots: list[dict[str, Any]],
    *,
    via_multilogin: bool = True,
    activate: bool = False,
    dry_run: bool = False,
    hand_off: bool = False,
) -> str:
    """Create a job and launch it in a background daemon thread. Returns job_id."""
    if activate:
        raise ValueError("ACL is draft-only: live activation is disabled")
    if not use_rpa_publish():
        raise RuntimeError(
            "The legacy Meta API backend is disabled in the campaign-manager dashboard"
        )
    hand_off = True
    from src.fb_ads_rpa.build_draft import slots_to_campaign_drafts

    total = len(
        slots_to_campaign_drafts(
            slots,
            account_label=account_label,
            country=country,
            activate=False,
        )
    )
    job_id = create_job(
        account_label, country, slots,
        via_multilogin=via_multilogin,
        activate=activate,
        dry_run=dry_run,
        hand_off=hand_off,
        total=total,
    )
    t = threading.Thread(
        target=_run_job,
        args=(job_id, slots),
        kwargs={
            "account_label": account_label,
            "country": country,
            "via_multilogin": via_multilogin,
            "activate": activate,
            "dry_run": dry_run,
            "hand_off": hand_off,
        },
        daemon=True,
        name=f"publish-{job_id[:8]}",
    )
    t.start()
    return job_id
