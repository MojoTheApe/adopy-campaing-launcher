"""Dry-run preview for CampaignDraft — compatible with dashboard preview UI."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config.paths import get_creatives_root
from src.fb_ads_rpa.draft import CampaignDraft


def _creative_rel_path(creative_path: str | None) -> str | None:
    """Strip the creatives root off an absolute path so the client only ever
    sees a relative path (passed to GET /creative-file, never a local FS path).
    """
    if not creative_path:
        return None
    try:
        return str(Path(creative_path).relative_to(get_creatives_root())).replace("\\", "/")
    except ValueError:
        return Path(creative_path).name


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
            "Link": draft.link or "—",
            "Language": draft.language or "—",
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

        ads_raw: list[dict[str, Any]] = []
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
            if ad.ad_body2:
                # ACL-1344: второй вариант основного текста — строка только
                # у объявлений, где он выбран.
                row[f"{prefix} — Body 2"] = ad.ad_body2[:80] + ("…" if len(ad.ad_body2) > 80 else "")
            raw = {
                "creative_type": ad.creative_type,
                "creative_rel_path": _creative_rel_path(ad.creative_path),
                "ad_title": ad.ad_title or "",
                "ad_body": ad.ad_body or "",
                "ad_link_description": ad.ad_link_description or "",
            }
            if ad.ad_body2:
                raw["ad_body2"] = ad.ad_body2
            ads_raw.append(raw)
        # Untruncated per-ad fields for the visual Feed/Reels preview — additive,
        # the flat "Ad N — ..." keys above stay unchanged for the diagnostic table.
        row["_ads_raw"] = ads_raw

        result.append(row)
    return result
