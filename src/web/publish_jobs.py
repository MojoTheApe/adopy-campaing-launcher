"""Background publish job store and runner.

Keeps an in-memory dict of running/completed publish jobs.
Each job runs in a daemon thread so the web server stays responsive.
Progress is streamed by appending to job["results"] as campaigns complete.

Default backend: RPA (Playwright + Ads Manager UI).
Set FB_PUBLISH_BACKEND=api for legacy Marketing API path.
"""
from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone
from typing import Any

from config.paths import get_ad_account_map_path
from src.campaign_bridge.dashboard_publisher import slots_to_publish_jobs
from src.fb_ads_rpa.config import use_rpa_publish
from src.fb_ads_rpa.draft import RpaPublishResult
from src.fb_ads_rpa.rpa_publisher import RpaPublisher
from src.meta_campaign_agent.ad_account_map import load_ad_account_map, resolve_account_entry
from src.meta_campaign_agent.campaign_builder import PublishJob
from src.meta_campaign_agent.creative_uploader import CreativeUploader
from src.meta_campaign_agent.meta_client import MetaGraphError, meta_client_from_env
from src.meta_campaign_agent.meta_publisher import MetaPublisher, PublishResult
from src.meta_campaign_agent.multilogin_launcher import MultiloginLauncherError
from src.meta_campaign_agent.multilogin_session import MultiloginSession

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
    total: int | None = None,
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
            "backend": "rpa" if use_rpa_publish() else "api",
            "status": "pending",
            "created_at": _now_iso(),
            "started_at": None,
            "finished_at": None,
            "results": [],
            "errors": [],
            "total": total if total is not None else len(slots),
            "done_count": 0,
        }
    return job_id


def get_job(job_id: str) -> dict[str, Any] | None:
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None:
            return None
        return dict(job)


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

    if use_rpa_publish():
        _run_rpa(job_id, slots, account_label=account_label, country=country, activate=activate)
        return

    _run_api(job_id, slots, account_label=account_label, country=country, via_multilogin=via_multilogin, activate=activate)


def _append_result(job_id: str, res: dict[str, Any]) -> None:
    with _jobs_lock:
        _jobs[job_id]["results"].append(res)
        _jobs[job_id]["done_count"] += 1


def _fatal(job_id: str, msg: str) -> None:
    with _jobs_lock:
        _jobs[job_id]["errors"].append(msg)
        _jobs[job_id]["status"] = "done"
        _jobs[job_id]["finished_at"] = _now_iso()


def _finish(job_id: str) -> None:
    with _jobs_lock:
        _jobs[job_id]["status"] = "done"
        _jobs[job_id]["finished_at"] = _now_iso()


def _run_rpa(
    job_id: str,
    slots: list[dict[str, Any]],
    *,
    account_label: str,
    country: str,
    activate: bool,
) -> None:
    publisher = RpaPublisher(dry_run=False)
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
        return

    with _jobs_lock:
        _jobs[job_id]["total"] = len(drafts)

    if not drafts:
        _fatal(job_id, "No campaigns to publish.")
        return

    ready: list[Any] = []
    for draft in drafts:
        if draft.errors:
            _append_result(
                _job_result_from_rpa(
                    RpaPublishResult(
                        campaign_name=draft.name,
                        success=False,
                        error="; ".join(draft.errors),
                    )
                )
            )
        else:
            ready.append(draft)

    if not ready:
        _finish(job_id)
        return

    try:
        for draft in ready:
            result = publisher.publish_draft(draft)
            _append_result(_job_result_from_rpa(result))
    except Exception as e:
        with _jobs_lock:
            _jobs[job_id]["errors"].append(f"RPA error: {e}")
    finally:
        publisher.close()
        _finish(job_id)


def _run_api(
    job_id: str,
    slots: list[dict[str, Any]],
    *,
    account_label: str,
    country: str,
    via_multilogin: bool,
    activate: bool,
) -> None:
    try:
        jobs = slots_to_publish_jobs(
            slots,
            account_label=account_label,
            country=country,
            activate=activate,
        )
    except Exception as e:
        _fatal(job_id, f"Failed to build publish jobs: {e}")
        return

    if not jobs:
        _fatal(job_id, "No campaigns to publish.")
        return

    with _jobs_lock:
        _jobs[job_id]["total"] = len(jobs)

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
        _finish(job_id)
        return

    if via_multilogin:
        _run_via_multilogin(job_id, ready, account_label, lambda r: _append_result(job_id, r))
    else:
        _run_direct(job_id, ready, lambda r: _append_result(job_id, r))

    _finish(job_id)


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
    account_map = load_ad_account_map(get_ad_account_map_path())
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
        done = len(_jobs[job_id]["results"]) if job_id in _jobs else 0
        for job in jobs[done:]:
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
