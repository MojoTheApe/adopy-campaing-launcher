"""Dry-run preview for CampaignDraft — compatible with dashboard preview UI."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.fb_ads_rpa.draft import CampaignDraft


def preview_campaign_drafts(drafts: list[CampaignDraft]) -> list[dict[str, Any]]:
    today = datetime.now(timezone.utc).strftime("%d.%m.%Y")
    result: list[dict[str, Any]] = []

    for draft in drafts:
        ad_set = draft.ad_sets[0] if draft.ad_sets else None
        budget = ad_set.daily_budget if ad_set else None
        row: dict[str, Any] = {
            "Campaign Name": draft.name,
            "Ad Set Name": ad_set.name if ad_set else draft.name,
            "Campaign Status": draft.status,
            "Campaign Objective": draft.objective,
            "Campaign Daily Budget": f"${budget}/day" if budget else "—",
            "Countries": draft.country,
            "Ad Account ID": draft.ad_account_id or "—",
            "Link Object ID (Page)": draft.page_id or "—",
            "Pixel": draft.pixel_id or "—",
            "Link": draft.link or "—",
            "Ads in ad set": draft.ad_count,
            "Publish backend": "RPA (Ads Manager UI)",
            "Date": today,
            "errors": list(draft.errors),
            "warnings": list(draft.warnings),
        }

        if ad_set:
            row["Targeting"] = ad_set.targeting_label or "—"
            row["Age Min"] = ad_set.age_min
            row["Age Max"] = ad_set.age_max
            row["OS"] = ad_set.os

        for idx, ad in enumerate((ad_set.ads if ad_set else []), start=1):
            prefix = f"Ad {idx}"
            creative_file = ad.creative_filename or "—"
            path_name = Path(ad.creative_path).name if ad.creative_path else "❌ file not mapped"
            row[f"{prefix} — Creative"] = ad.creative_type
            row[f"{prefix} — File"] = creative_file
            row[f"{prefix} — Path"] = path_name
            row[f"{prefix} — Title"] = ad.ad_title or "—"
            row[f"{prefix} — Body"] = (ad.ad_body or "")[:80] + (
                "…" if len(ad.ad_body or "") > 80 else ""
            )

        result.append(row)
    return result
