from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from config.paths import get_ad_account_map_path, get_creatives_root, get_upload_fb_excel_path
from src.meta_campaign_agent.ad_account_map import AccountMapEntry, load_ad_account_map, resolve_account_entry
from src.meta_campaign_agent.campaign_builder import PublishJob, build_publish_job
from src.meta_campaign_agent.creative_finder import AmbiguousCreativeError, CreativeNotFoundError, find_creative_file
from src.meta_campaign_agent.creative_uploader import CreativeUploader
from src.meta_campaign_agent.excel_reader import read_upload_workbook
from src.meta_campaign_agent.meta_client import MetaClient, meta_client_from_env
from src.meta_campaign_agent.meta_publisher import LivePublishReport, MetaPublisher, PublishResult
from src.meta_campaign_agent.multilogin_launcher import MultiloginLauncherError
from src.meta_campaign_agent.multilogin_session import MultiloginSession
from src.upload_agent.build_upload_excel import load_reference_data, lookup_ad_copy, lookup_interest


@dataclass
class DryRunReport:
    input_path: str
    creatives_root: str
    jobs: list[PublishJob] = field(default_factory=list)

    @property
    def ready_count(self) -> int:
        return sum(1 for j in self.jobs if not j.errors)

    @property
    def error_count(self) -> int:
        return sum(1 for j in self.jobs if j.errors)


def _default_page_id() -> str | None:
    val = (os.environ.get("META_PAGE_ID") or "").strip()
    return val or None


def _collect_jobs(
    xlsx_path: str,
    *,
    account_filter: str | None = None,
    creatives_root: str | None = None,
    ad_account_map_path: str | None = None,
    upload_fb_excel: str | None = None,
    activate: bool = False,
) -> tuple[list[PublishJob], str]:
    wb = read_upload_workbook(xlsx_path)
    root = creatives_root or get_creatives_root()
    account_map = load_ad_account_map(ad_account_map_path or get_ad_account_map_path())
    fallback_page = _default_page_id()

    ref: dict | None = None
    ref_path = upload_fb_excel or get_upload_fb_excel_path()
    if Path(ref_path).is_file():
        ref = load_reference_data(ref_path)

    jobs: list[PublishJob] = []
    filter_name = (account_filter or "").strip().lower()

    for sheet_name, rows in wb.sheets.items():
        if filter_name and filter_name not in sheet_name.lower():
            continue
        entry = resolve_account_entry(account_map, sheet_name)
        ad_account_id = entry.ad_account_id if entry else None
        page_id = (entry.page_id if entry else None) or fallback_page

        for row in rows:
            creative_type = row.get("Creative") or "Image"
            is_video = creative_type.lower() == "video"
            filename = row.get("Video File name", "Video File Name", "Video file name") if is_video else row.get(
                "Image File name", "Image FIle name", "Image file name"
            )

            creative_path: str | None = None
            path_err: str | None = None
            if filename:
                try:
                    creative_path = str(find_creative_file(root, filename, fresh=True))
                except CreativeNotFoundError as e:
                    path_err = str(e)
                except AmbiguousCreativeError as e:
                    path_err = str(e)

            interests_json: str | None = None
            ad_copy: dict[str, str] | None = None
            if ref:
                targeting = row.get("Targeting")
                if targeting:
                    interests_json = lookup_interest(ref, targeting) or None
                copy_key = row.get("Copy")
                if copy_key:
                    ad_copy = lookup_ad_copy(ref, copy_key)

            job = build_publish_job(
                row,
                ad_account_id=ad_account_id,
                creative_path=creative_path,
                interests_json=interests_json,
                activate=activate,
                ad_copy=ad_copy,
                page_id=page_id,
            )
            if path_err and not creative_path:
                job.errors.append(path_err)
            jobs.append(job)

    return jobs, root


def build_dry_run_report(
    xlsx_path: str,
    *,
    account_filter: str | None = None,
    creatives_root: str | None = None,
    ad_account_map_path: str | None = None,
    upload_fb_excel: str | None = None,
    activate: bool = False,
) -> DryRunReport:
    jobs, root = _collect_jobs(
        xlsx_path,
        account_filter=account_filter,
        creatives_root=creatives_root,
        ad_account_map_path=ad_account_map_path,
        upload_fb_excel=upload_fb_excel,
        activate=activate,
    )
    return DryRunReport(input_path=xlsx_path, creatives_root=root, jobs=jobs)


def publish_live(
    xlsx_path: str,
    *,
    account_filter: str | None = None,
    creatives_root: str | None = None,
    ad_account_map_path: str | None = None,
    upload_fb_excel: str | None = None,
    activate: bool = False,
    limit: int | None = None,
    client: MetaClient | None = None,
    log_path: str = "output/publish_log.jsonl",
    via_multilogin: bool = False,
) -> LivePublishReport:
    if via_multilogin:
        return publish_live_via_multilogin(
            xlsx_path,
            account_filter=account_filter,
            creatives_root=creatives_root,
            ad_account_map_path=ad_account_map_path,
            upload_fb_excel=upload_fb_excel,
            activate=activate,
            limit=limit,
            log_path=log_path,
        )

    jobs, _ = _collect_jobs(
        xlsx_path,
        account_filter=account_filter,
        creatives_root=creatives_root,
        ad_account_map_path=ad_account_map_path,
        upload_fb_excel=upload_fb_excel,
        activate=activate,
    )
    ready = [j for j in jobs if not j.errors]
    if limit is not None and limit > 0:
        ready = ready[:limit]

    meta = client or meta_client_from_env()
    uploader = CreativeUploader(meta)
    publisher = MetaPublisher(meta, uploader, log_path=log_path)
    return publisher.publish_jobs(ready)


def _group_jobs_by_account(jobs: list[PublishJob]) -> dict[str, list[PublishJob]]:
    grouped: dict[str, list[PublishJob]] = {}
    for job in jobs:
        grouped.setdefault(job.account_name, []).append(job)
    return grouped


def publish_live_via_multilogin(
    xlsx_path: str,
    *,
    account_filter: str | None = None,
    creatives_root: str | None = None,
    ad_account_map_path: str | None = None,
    upload_fb_excel: str | None = None,
    activate: bool = False,
    limit: int | None = None,
    log_path: str = "output/publish_log.jsonl",
) -> LivePublishReport:
    """
    For each account (Excel sheet): start Multilogin profile → Meta API via profile proxy → stop profile.
    One profile / one proxy / one account per iteration; never parallel.
    """
    jobs, _ = _collect_jobs(
        xlsx_path,
        account_filter=account_filter,
        creatives_root=creatives_root,
        ad_account_map_path=ad_account_map_path,
        upload_fb_excel=upload_fb_excel,
        activate=activate,
    )
    account_map = load_ad_account_map(ad_account_map_path or get_ad_account_map_path())
    grouped = _group_jobs_by_account([j for j in jobs if not j.errors])

    report = LivePublishReport(log_path=log_path)
    published_rows = 0

    for account_name, account_jobs in grouped.items():
        if limit is not None and published_rows >= limit:
            break

        entry = resolve_account_entry(account_map, account_name)
        if not entry:
            for job in account_jobs:
                report.results.append(
                    PublishResult(job=job, error=f"Account {account_name!r} not in ad_account_map.csv")
                )
            continue

        batch = account_jobs
        if limit is not None and limit > 0:
            remaining = limit - published_rows
            batch = batch[:remaining]

        try:
            with MultiloginSession(entry, account_name) as ctx:
                meta = meta_client_from_env(proxies=ctx.proxy.to_requests_proxies())
                uploader = CreativeUploader(meta)
                publisher = MetaPublisher(meta, uploader, log_path=log_path)
                batch_report = publisher.publish_jobs(batch)
                report.results.extend(batch_report.results)
                published_rows += len(batch)
        except MultiloginLauncherError as e:
            for job in batch:
                report.results.append(PublishResult(job=job, error=str(e)))

    for job in jobs:
        if job.errors:
            report.results.append(PublishResult(job=job, error="; ".join(job.errors)))

    return report


def report_to_json(report: DryRunReport) -> str:
    payload = {
        "input": report.input_path,
        "creatives_root": report.creatives_root,
        "summary": {
            "total": len(report.jobs),
            "ready": report.ready_count,
            "with_errors": report.error_count,
        },
        "jobs": [
            {
                "account": j.account_name,
                "ad_account_id": j.ad_account_id,
                "row": j.row_number,
                "campaign_name": j.campaign_name,
                "status": j.status,
                "creative_file": j.creative_path,
                "errors": j.errors,
                "warnings": j.warnings,
                "api_preview": j.api_preview,
            }
            for j in report.jobs
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def live_report_to_json(report: LivePublishReport) -> str:
    payload = {
        "summary": {
            "total": len(report.results),
            "success": report.success_count,
            "failed": report.failure_count,
        },
        "log_path": report.log_path,
        "results": [
            {
                "account": r.job.account_name,
                "row": r.job.row_number,
                "campaign_name": r.job.campaign_name,
                "success": r.success,
                "campaign_id": r.campaign_id,
                "adset_id": r.adset_id,
                "creative_id": r.creative_id,
                "ad_id": r.ad_id,
                "error": r.error,
            }
            for r in report.results
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)
