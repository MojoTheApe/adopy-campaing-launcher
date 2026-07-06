from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from src.meta_campaign_agent.excel_reader import UploadRow


def _int_or(val: str, default: int) -> int:
    try:
        return int(float(val))
    except (TypeError, ValueError):
        return default


def _float_or(val: str, default: float | None) -> float | None:
    if not val:
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


@dataclass
class PublishJob:
    account_name: str
    ad_account_id: str | None
    row_number: int
    campaign_name: str
    creative_type: str
    creative_filename: str
    creative_path: str | None
    link: str
    pixel_id: str
    status: str
    objective: str
    daily_budget: float | None
    age_min: int
    age_max: int
    country_code: str
    targeting_name: str
    targeting_spec: dict[str, Any] | None
    ad_title: str
    ad_body: str
    ad_link_description: str
    page_id: str | None = None
    optimization_goal: str = "OFFSITE_CONVERSIONS"
    bid_strategy: str = "LOWEST_COST_WITHOUT_CAP"
    billing_event: str = "IMPRESSIONS"
    destination_type: str = "WEBSITE"
    api_preview: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _parse_interests_json(raw: str) -> dict[str, Any] | None:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


def build_targeting_spec(
    row: UploadRow,
    interests_json: str | None = None,
) -> dict[str, Any]:
    country = row.get("Countries", "Campaign name - country")
    age_min = _int_or(row.get("Age Min", "Age"), 18)
    age_max = _int_or(row.get("Age Max", "Age"), 65)

    spec: dict[str, Any] = {
        "geo_locations": {"countries": [country] if country else []},
        "age_min": age_min,
        "age_max": age_max,
    }

    flex = _parse_interests_json(interests_json or "")
    if flex:
        spec["flexible_spec"] = flex.get("flexible_spec", flex)
    return spec


def build_publish_job(
    row: UploadRow,
    *,
    ad_account_id: str | None,
    creative_path: str | None,
    interests_json: str | None = None,
    activate: bool = False,
    ad_copy: dict[str, str] | None = None,
    page_id: str | None = None,
) -> PublishJob:
    creative_type = row.get("Creative") or "Image"
    is_video = creative_type.lower() == "video"
    creative_filename = row.get("Video File name", "Video File Name", "Video file name") if is_video else row.get(
        "Image File name", "Image FIle name", "Image file name"
    )

    campaign_name = row.get("Campaign Name")
    status = "ACTIVE" if activate else "PAUSED"
    excel_status = row.get("Campaign Status")
    if excel_status.upper() in ("ACTIVE", "PAUSED"):
        status = excel_status.upper() if activate else "PAUSED"

    objective = row.get("Campaign Objective") or "OUTCOME_LEADS"
    daily_budget = _float_or(row.get("Campaign Daily Budget"), None)
    age_min = _int_or(row.get("Age Min", "Age"), 18)
    age_max = _int_or(row.get("Age Max", "Age"), 65)
    country = row.get("Countries", "Campaign name - country")
    targeting_name = row.get("Targeting")
    targeting_spec = build_targeting_spec(row, interests_json)

    copy = ad_copy or {}
    title = copy.get("title") or row.get("Copy")
    body = copy.get("body") or ""
    link_desc = copy.get("link_description") or ""
    page_from_row = row.get("Link Object ID", "Page ID", "page_id")
    resolved_page = (page_id or page_from_row or "").strip() or None
    opt_goal_raw = (row.get("Optimized Event") or "LEAD").strip().upper()
    optimization_goal = "OFFSITE_CONVERSIONS" if opt_goal_raw in ("LEAD", "OFFSITE_CONVERSIONS") else opt_goal_raw
    bid_strategy = row.get("Campaign Bid Strategy") or "LOWEST_COST_WITHOUT_CAP"
    billing_event = "IMPRESSIONS"
    destination_type = row.get("Destination Type") or "WEBSITE"

    job = PublishJob(
        account_name=row.sheet_account,
        ad_account_id=ad_account_id,
        row_number=row.row_number,
        campaign_name=campaign_name,
        creative_type=creative_type,
        creative_filename=creative_filename,
        creative_path=creative_path,
        link=row.get("Link"),
        pixel_id=row.get("Pixel"),
        status=status,
        objective=objective,
        daily_budget=daily_budget,
        age_min=age_min,
        age_max=age_max,
        country_code=country,
        targeting_name=targeting_name,
        targeting_spec=targeting_spec,
        ad_title=title,
        ad_body=body,
        ad_link_description=link_desc,
        page_id=resolved_page,
        optimization_goal=optimization_goal,
        bid_strategy=bid_strategy,
        billing_event=billing_event,
        destination_type=destination_type,
    )

    if not ad_account_id:
        job.errors.append("ad_account_id not mapped (config/ad_account_map.csv)")
    if not creative_filename:
        job.errors.append("creative filename missing in Excel row")
    elif not creative_path:
        job.errors.append(f"creative file not found: {creative_filename}")
    if not job.link:
        job.errors.append("Link is empty (required for live publish)")
    if not job.pixel_id:
        job.warnings.append("Pixel is empty")
    if not resolved_page:
        job.errors.append("page_id missing (Link Object ID in Excel or page_id in ad_account_map.csv / META_PAGE_ID)")
    if job.daily_budget is None or job.daily_budget <= 0:
        job.errors.append("Campaign Daily Budget missing or invalid")
    if interests_json is None and targeting_name:
        job.warnings.append(
            f"Interests JSON not resolved for targeting {targeting_name!r} (pass --upload-excel Upluad FB.xlsx)"
        )

    job.api_preview = {
        "campaign": {
            "name": campaign_name,
            "objective": objective,
            "status": status,
            "special_ad_categories": [],
        },
        "ad_set": {
            "name": campaign_name,
            "optimization_goal": optimization_goal,
            "billing_event": billing_event,
            "bid_strategy": bid_strategy,
            "daily_budget": daily_budget,
            "status": status,
            "targeting": targeting_spec,
            "promoted_object": {"pixel_id": job.pixel_id, "custom_event_type": "LEAD"},
            "destination_type": destination_type,
        },
        "ad_creative": {
            "title": title,
            "body": body,
            "link_description": link_desc,
            "link": job.link,
            "creative_file": creative_path,
            "creative_type": "video" if is_video else "image",
            "page_id": resolved_page,
        },
        "ad": {
            "name": campaign_name,
            "status": status,
        },
    }
    return job
