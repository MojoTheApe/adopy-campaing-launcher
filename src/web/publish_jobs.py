"""Background publish job store and runner.

Keeps an in-memory dict of running/completed publish jobs.
Each job runs in a daemon thread so the web server stays responsive.
Progress is streamed by appending to job["results"] as campaigns complete.
"""
from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone
from typing import Any

from src.campaign_bridge.dashboard_publisher import slots_to_publish_jobs
from src.meta_campaign_agent.campaign_builder import PublishJob
from src.meta_campaign_agent.creative_uploader import CreativeUploader
from src.meta_campaign_agent.meta_client import MetaGraphError, meta_client_from_env
from src.meta_campaign_agent.meta_publisher import MetaPublisher, PublishResult
from src.meta_campaign_agent.multilogin_launcher import MultiloginLauncherError
from src.meta_campaign_agent.multilogin_session import MultiloginSession
from src.meta_campaign_agent.ad_account_map import load_ad_account_map, resolve_account_entry
from config.paths import get_ad_account_map_path

# ── In-memory job store ────────────────────────────────────────────────
_jobs: dict[str, dict[str, Any]] = {}
_jobs_lock = threading.Lock()

_LOG_PATH = "output/dashboard_publish_log.jsonl"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _job_result_from(job: PublishJob, result: PublishResult) -> dict[str, Any]:
    return {
        "campaign_name": job.campaign_name,
        "success": result.success,
        "campaign_id": result.campaign_id,
        "adset_id": result.adset_id,
        "creative_id": result.creative_id,
        "ad_id": result.ad_id,
        "error": result.error,
        "ts": _now_iso(),
    }


def create_job(
    account_label: str,
    country: str,
    slots: list[dict[str, Any]],
    *,
    via_multilogin: bool = True,
    activate: bool = False,
) -> str:
    """Create a new publish job, store it, return its job_id."""
    job_id = str(uuid.uuid4())
    with _jobs_lock:
        _jobs[job_id] = {
            "job_id": job_id,
            "account_label": account_label,
            "country": country,
            "via_multilogin": via_multilogin,
            "activate": activate,
            "status": "pending",  # pending → running → done | failed
            "created_at": _now_iso(),
            "started_at": None,
            "finished_at": None,
            "results": [],          # appended as each campaign completes
            "errors": [],           # fatal errors (e.g. ML profile failed)
            "total": len(slots),
            "done_count": 0,
        }
    return job_id


def get_job(job_id: str) -> dict[str, Any] | None:
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None:
            return None
        return dict(job)  # shallow copy is fine for reads


def _run_job(
    job_id: str,
    slots: list[dict[str, Any]],
    *,
    account_label: str,
    country: str,
    via_multilogin: bool,
    activate: bool,
) -> None:
    """Runs in a daemon thread. Updates _jobs[job_id] as it progresses."""
    with _jobs_lock:
        _jobs[job_id]["status"] = "running"
        _jobs[job_id]["started_at"] = _now_iso()

    def _append_result(res: dict[str, Any]) -> None:
        with _jobs_lock:
            _jobs[job_id]["results"].append(res)
            _jobs[job_id]["done_count"] += 1

    def _fatal(msg: str) -> None:
        with _jobs_lock:
            _jobs[job_id]["errors"].append(msg)
            _jobs[job_id]["status"] = "done"
            _jobs[job_id]["finished_at"] = _now_iso()

    try:
        jobs = slots_to_publish_jobs(
            slots,
            account_label=account_label,
            country=country,
            activate=activate,
        )
    except Exception as e:
        _fatal(f"Failed to build publish jobs: {e}")
        return

    if not jobs:
        _fatal("No campaigns to publish.")
        return

    # Filter jobs with pre-validation errors (still report them)
    ready = []
    for job in jobs:
        if job.errors:
            _append_result(
                {
                    "campaign_name": job.campaign_name,
                    "success": False,
                    "campaign_id": None,
                    "adset_id": None,
                    "creative_id": None,
                    "ad_id": None,
                    "error": "; ".join(job.errors),
                    "ts": _now_iso(),
                }
            )
        else:
            ready.append(job)

    if not ready:
        with _jobs_lock:
            _jobs[job_id]["status"] = "done"
            _jobs[job_id]["finished_at"] = _now_iso()
        return

    if via_multilogin:
        _run_via_multilogin(job_id, ready, account_label, _append_result)
    else:
        _run_direct(job_id, ready, _append_result)

    with _jobs_lock:
        _jobs[job_id]["status"] = "done"
        _jobs[job_id]["finished_at"] = _now_iso()


def _run_direct(
    job_id: str,
    jobs: list[PublishJob],
    append_result: Any,
) -> None:
    """Publish all jobs using a single Meta client (no Multilogin)."""
    try:
        meta = meta_client_from_env()
    except MetaGraphError as e:
        with _jobs_lock:
            _jobs[job_id]["errors"].append(str(e))
        return

    uploader = CreativeUploader(meta)
    publisher = MetaPublisher(meta, uploader, log_path=_LOG_PATH)
    for job in jobs:
        result = publisher.publish_job(job)
        append_result(_job_result_from(job, result))


def _run_via_multilogin(
    job_id: str,
    jobs: list[PublishJob],
    account_label: str,
    append_result: Any,
) -> None:
    """Publish jobs: start ML profile → Meta API via its proxy → stop profile."""
    map_path = get_ad_account_map_path()
    account_map = load_ad_account_map(map_path)
    entry = resolve_account_entry(account_map, account_label)

    if not entry:
        with _jobs_lock:
            _jobs[job_id]["errors"].append(
                f"Account {account_label!r} not found in ad_account_map.csv"
            )
        return

    try:
        with MultiloginSession(entry, account_label) as ctx:
            meta = meta_client_from_env(proxies=ctx.proxy.to_requests_proxies())
            uploader = CreativeUploader(meta)
            publisher = MetaPublisher(meta, uploader, log_path=_LOG_PATH)
            for job in jobs:
                result = publisher.publish_job(job)
                append_result(_job_result_from(job, result))
    except MultiloginLauncherError as e:
        err_msg = str(e)
        with _jobs_lock:
            _jobs[job_id]["errors"].append(f"Multilogin error: {err_msg}")
        # Mark remaining jobs as failed
        done = len(_jobs[job_id]["results"]) if job_id in _jobs else 0
        remaining = jobs[done:]
        for job in remaining:
            append_result(
                {
                    "campaign_name": job.campaign_name,
                    "success": False,
                    "error": err_msg,
                    "ts": _now_iso(),
                }
            )


def start_job(
    account_label: str,
    country: str,
    slots: list[dict[str, Any]],
    *,
    via_multilogin: bool = True,
    activate: bool = False,
) -> str:
    """Create a job and launch it in a background daemon thread. Returns job_id."""
    job_id = create_job(
        account_label, country, slots,
        via_multilogin=via_multilogin,
        activate=activate,
    )
    t = threading.Thread(
        target=_run_job,
        args=(job_id, slots),
        kwargs={
            "account_label": account_label,
            "country": country,
            "via_multilogin": via_multilogin,
            "activate": activate,
        },
        daemon=True,
        name=f"publish-{job_id[:8]}",
    )
    t.start()
    return job_id
