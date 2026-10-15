"""Convert campaign builder dashboard slots → PublishJob objects.

Each UI slot (from the form) maps to exactly one PublishJob.
Requires:
  - Upload FB.xlsx (for link, pixel, interests, ad copy, creative filename)
  - config/ad_account_map.csv (for ad_account_id, page_id)
  - creatives/ folder (for finding the actual file)
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from config.paths import get_ad_account_map_path, get_creatives_root, get_upload_fb_excel_path
from src.meta_campaign_agent.ad_account_map import load_ad_account_map, resolve_account_entry
from src.meta_campaign_agent.campaign_builder import PublishJob, build_publish_job
from src.meta_campaign_agent.creative_finder import AmbiguousCreativeError, CreativeNotFoundError, find_creative_file
from src.meta_campaign_agent.excel_reader import UploadRow
from src.upload_agent.build_upload_excel import (
    get_account_info,
    load_reference_data,
    lookup_ad_copy,
    lookup_creative_file,
    lookup_interest,
)

# ── Reference data cache (avoid re-reading xlsx on every preview) ──────
_ref_cache: dict | None = None
_ref_cache_mtime: float = -1.0


def _get_ref(excel_path: str) -> dict:
    """Return cached ref data; reload only when file mtime changes."""
    global _ref_cache, _ref_cache_mtime
    try:
        mtime = os.path.getmtime(excel_path)
    except OSError:
        return {}
    if _ref_cache is not None and mtime == _ref_cache_mtime:
        return _ref_cache
    try:
        ref = load_reference_data(excel_path)
    except Exception as e:
        return {"_load_error": str(e)}
    _ref_cache = ref
    _ref_cache_mtime = mtime
    return ref


def _make_fake_row(slot: dict[str, Any], account_label: str, country: str, ref: dict) -> UploadRow:
    """Build a minimal UploadRow from a dashboard slot so we can reuse build_publish_job()."""
    creative_type = (slot.get("creative_type") or "Image").strip()
    creative_id = (slot.get("creative_id") or "").strip()
    copy_name = (slot.get("copy") or "").strip()
    targeting = (slot.get("targeting") or "").strip()
    os_val = (slot.get("os") or "All").strip()
    age = (slot.get("age") or "35").strip()
    budget = (slot.get("budget") or "").strip()
    asset = (slot.get("asset") or "").strip()
    campaign_name = (slot.get("name") or "").strip()

    # Look up creative filename from ref
    creative_filename = lookup_creative_file(ref, creative_type, creative_id)

    # Look up link + pixel from Accounts sheet
    account_info = get_account_info(ref, account_label)
    link = str(account_info.get("link") or "")
    pixel = str(account_info.get("pixel") or "")

    cells: dict[str, str] = {
        "Campaign Name": campaign_name,
        "Campaign name - country": country,
        "Countries": country,
        "Creative": creative_type,
        "Image File name": creative_filename if creative_type.lower() == "image" else "",
        "Video File name": creative_filename if creative_type.lower() == "video" else "",
        "Copy": copy_name,
        "Targeting": targeting,
        "OS": os_val,
        "Age": age,
        "Age Min": age,
        "Age Max": age,
        "Link": link,
        "Pixel": pixel,
        "Campaign Daily Budget": budget,
        "Campaign Objective": "OUTCOME_LEADS",
        "Optimized Event": "LEAD",
        "Campaign Bid Strategy": "LOWEST_COST_WITHOUT_CAP",
        "Destination Type": "WEBSITE",
        "Campaign Status": "PAUSED",
        "Asset": asset,
    }
    return UploadRow(sheet_account=account_label, row_number=0, cells=cells)


def slots_to_publish_jobs(
    slots: list[dict[str, Any]],
    *,
    account_label: str,
    country: str,
    activate: bool = False,
    upload_fb_path: str | None = None,
    ad_account_map_path: str | None = None,
    creatives_root: str | None = None,
    preview_mode: bool = False,
) -> list[PublishJob]:
    """Convert dashboard form slots to PublishJob objects (with validation errors attached).

    preview_mode=True skips the slow recursive file search on disk.
    """
    excel_path = (upload_fb_path or "").strip() or get_upload_fb_excel_path()
    map_path = (ad_account_map_path or "").strip() or get_ad_account_map_path()
    root = (creatives_root or "").strip() or get_creatives_root()

    ref: dict = {}
    if os.path.isfile(excel_path):
        ref = _get_ref(excel_path)

    account_map = load_ad_account_map(map_path)
    entry = resolve_account_entry(account_map, account_label)
    ad_account_id = entry.ad_account_id if entry else None
    page_id = (entry.page_id if entry else None)

    jobs: list[PublishJob] = []
    for slot in slots:
        copy_name = (slot.get("copy") or "").strip()
        targeting = (slot.get("targeting") or "").strip()

        # Resolve interests JSON
        interests_json: str | None = None
        if targeting and ref:
            raw = lookup_interest(ref, targeting)
            interests_json = raw or None

        # Resolve ad copy
        ad_copy: dict[str, Any] | None = None
        if copy_name and ref:
            ad_copy = lookup_ad_copy(ref, copy_name)

        # Build fake UploadRow
        row = _make_fake_row(slot, account_label, country, ref)

        # Resolve creative file path
        creative_type = (slot.get("creative_type") or "Image").strip()
        creative_id = (slot.get("creative_id") or "").strip()
        creative_filename = lookup_creative_file(ref, creative_type, creative_id) if ref else ""
        creative_path: str | None = None
        path_err: str | None = None
        if creative_filename and not preview_mode:
            try:
                creative_path = str(find_creative_file(root, creative_filename, fresh=True))
            except (CreativeNotFoundError, AmbiguousCreativeError, FileNotFoundError) as e:
                path_err = str(e)
        elif creative_filename and preview_mode:
            # In preview mode: synthesise an expected path without hitting the disk
            creative_path = str(Path(root) / creative_filename)

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
        if ref.get("_load_error"):
            job.warnings.append(f"Upload FB.xlsx not loaded: {ref['_load_error']}")

        jobs.append(job)

    return jobs


def preview_jobs(
    slots: list[dict[str, Any]],
    *,
    account_label: str,
    country: str,
    **kwargs: Any,
) -> list[dict[str, Any]]:
    """Return a full dry-run preview — all Upload FB.xlsx columns that are auto-filled."""
    from datetime import datetime, timezone

    jobs = slots_to_publish_jobs(
        slots, account_label=account_label, country=country, preview_mode=True, **kwargs
    )
    today = datetime.now(timezone.utc).strftime("%d.%m.%Y")
    result = []
    for job in jobs:
        creative_filename = Path(job.creative_path).name if job.creative_path else (job.creative_filename or "—")
        targeting_spec_str = ""
        if job.targeting_spec:
            flex = job.targeting_spec.get("flexible_spec")
            if flex:
                targeting_spec_str = "✓ interests JSON resolved"
            else:
                targeting_spec_str = "geo only"

        result.append(
            {
                # ── Campaign ───────────────────────────────────────
                "Campaign Name": job.campaign_name,
                "Ad Set Name": job.campaign_name,
                "Campaign Status": job.status,
                "Campaign Objective": job.objective,
                "Campaign Daily Budget": f"${job.daily_budget}/day" if job.daily_budget else "—",
                "Campaign Bid Strategy": job.bid_strategy,
                "Buying Type": "AUCTION",
                "Optimized Event": job.optimization_goal,
                "Destination Type": job.destination_type,
                # ── Targeting ──────────────────────────────────────
                "Countries": job.country_code,
                "Age Min": job.age_min,
                "Age Max": job.age_max,
                "Gender": "all",
                "Location Types": "home",
                "Targeting": job.targeting_name or "—",
                "Interests JSON": targeting_spec_str,
                # ── Account ────────────────────────────────────────
                "Ad Account ID": job.ad_account_id or "—",
                "Link Object ID (Page)": job.page_id or "—",
                "Pixel": job.pixel_id or "—",
                "Link": job.link or "—",
                # ── Creative ───────────────────────────────────────
                "Creative": job.creative_type,
                "Creative Number": job.creative_filename or "—",
                "Image File name" if job.creative_type.lower() != "video" else "Video File name": creative_filename,
                "Creative Path": (Path(job.creative_path).name if job.creative_path else "❌ file not mapped"),
                # ── Ad copy ────────────────────────────────────────
                "Copy (name)": job.ad_title or "—",
                "Ad Title": job.ad_title or "—",
                "Ad Body": (job.ad_body or "")[:80] + ("…" if len(job.ad_body or "") > 80 else ""),
                "Ad Link Description": job.ad_link_description or "—",
                # ── Meta ───────────────────────────────────────────
                "Date": today,
                # ── Validation ─────────────────────────────────────
                "errors": job.errors,
                "warnings": job.warnings,
            }
        )
    return result
