"""Map §11 open recommendations + user edits to Agent 8 plan rows."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from src.upload_agent.build_upload_excel import (
    PLAN_ACCOUNT,
    PLAN_AGE,
    PLAN_ASSET,
    PLAN_COPY,
    PLAN_COUNTRY,
    PLAN_CREATIVE,
    PLAN_CREATIVE_NUMBER,
    PLAN_DATE,
    PLAN_OS,
    PLAN_TARGETING,
)

_ADS_PER_STRATEGY = 3


def parse_creative_from_ad_title(ad_title: str) -> tuple[str, str]:
    """Return (Image|Video, creative_number digits) from ad title."""
    if not ad_title or not isinstance(ad_title, str):
        return "Image", ""
    s = ad_title.strip().lower().replace(" ", "")
    creative_type = "Image"
    if "video" in s:
        creative_type = "Video"
    elif "image" in s or "images" in s:
        creative_type = "Image"
    digits = "".join(c for c in s if c.isdigit())
    return creative_type, digits


def _normalize_os(device: str) -> str:
    d = (device or "All").strip()
    upper = d.upper()
    if upper in ("IOS", "ANDROID"):
        return "IOS" if upper == "IOS" else "Android"
    return "All"


def _normalize_age(age: str) -> str:
    a = (age or "").strip()
    if not a or a.upper() == "ALL":
        return "35"
    digits = re.sub(r"[^\d]", "", a)
    return digits or "35"


def _today_ddmm() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%d.%m")


def build_campaign_name_from_slot(slot: dict[str, Any]) -> str:
    """Generate campaign name: Country_Asset_OS_CreativeType_Targeting_Age_DD.MM"""
    parts = [
        (slot.get("country") or "").strip(),
        re.sub(r"\s+", "", (slot.get("asset") or "").strip()),
        (slot.get("os") or "All").strip(),
        (slot.get("creative_type") or "Image").strip(),
        (slot.get("targeting") or "").strip(),
        (slot.get("age") or "35").strip(),
        _today_ddmm(),
    ]
    return "_".join(p for p in parts if p)


def slot_to_plan_row(
    slot: dict[str, Any],
    *,
    account_label: str,
    country: str,
) -> dict[str, str]:
    """
    Convert a campaign builder UI slot (from the dashboard form) to an Agent 8 plan row dict.

    slot keys: name, creative_type, creative_id, creative_file, copy,
               targeting, os, age, budget, asset
    """
    creative_type = (slot.get("creative_type") or "Image").strip()
    creative_id = (slot.get("creative_id") or "").strip()
    copy = (slot.get("copy") or "").strip()
    targeting = (slot.get("targeting") or "").strip()
    os_val = _normalize_os(str(slot.get("os") or "All"))
    age = _normalize_age(str(slot.get("age") or "35"))
    asset = (slot.get("asset") or "").strip()
    budget = (slot.get("budget") or "").strip()
    name = (slot.get("name") or "").strip()

    row: dict[str, str] = {
        PLAN_ACCOUNT: account_label,
        PLAN_COUNTRY: country,
        PLAN_ASSET: asset,
        PLAN_OS: os_val,
        PLAN_CREATIVE: creative_type,
        PLAN_TARGETING: targeting,
        PLAN_AGE: age,
        PLAN_CREATIVE_NUMBER: creative_id,
        PLAN_DATE: _today_ddmm(),
        PLAN_COPY: copy,
        "campaign_name": name,
        "ad_set_name": name,
    }
    if budget:
        row["campaign_daily_budget"] = budget
    return row


def open_rec_to_plan_rows(
    rec: dict[str, Any],
    *,
    account_label: str,
    country: str,
    campaign_name: str,
    ad_titles: list[str],
) -> list[dict[str, str]]:
    """One selected strategy → up to 3 plan dicts sharing campaign/ad set name."""
    rows: list[dict[str, str]] = []
    name = (campaign_name or rec.get("campaign_name") or "").strip()
    titles = list(ad_titles[:_ADS_PER_STRATEGY])
    while len(titles) < _ADS_PER_STRATEGY:
        titles.append("")

    for title in titles:
        title = (title or "").strip()
        if not title:
            continue
        creative_type, creative_number = parse_creative_from_ad_title(title)
        rows.append(
            {
                PLAN_ACCOUNT: account_label,
                PLAN_COUNTRY: country,
                PLAN_ASSET: (rec.get("offer") or "").strip(),
                PLAN_OS: _normalize_os(str(rec.get("device") or "All")),
                PLAN_CREATIVE: creative_type,
                PLAN_TARGETING: (rec.get("targeting") or "").strip(),
                PLAN_AGE: _normalize_age(str(rec.get("age") or "35")),
                PLAN_CREATIVE_NUMBER: creative_number,
                PLAN_DATE: "",
                PLAN_COPY: title,
                "campaign_name": name,
                "ad_set_name": name,
            }
        )
    return rows
