"""Campaign tree for RPA publish: Campaign → Ad Set(s) → Ad(s)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class AdDraft:
    creative_type: str = "Image"
    creative_id: str = ""
    creative_filename: str = ""
    creative_path: str | None = None
    copy_name: str = ""
    ad_title: str = ""
    ad_body: str = ""
    ad_link_description: str = ""

    def ad_name(self) -> str:
        """Name for the ad in Ads Manager: "<creative> - <text>".

        The stats sheet stores both halves in one Ad Title cell in exactly
        this shape ("2046 1 - Text6"), and the dashboard splits it back
        apart (`split_ad_title`). Publishing only the text half — which is
        what happened until 2026-08-05, ads landed named just "Text4" —
        meant the creative could never be attributed in the reports.

        The creative half is `creative_id` when the builder supplied one,
        otherwise the file's own name without its extension.
        """
        creative = (self.creative_id or "").strip()
        if not creative and self.creative_filename:
            creative = self.creative_filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
            if "." in creative:
                creative = creative.rsplit(".", 1)[0]
        text = (self.copy_name or "").strip()
        if creative and text:
            return f"{creative} - {text}"
        return text or creative

    def to_preview_dict(self) -> dict[str, Any]:
        return {
            "creative_type": self.creative_type,
            "creative_id": self.creative_id,
            "creative_file": self.creative_filename or "—",
            "creative_path": self.creative_path or "—",
            "copy_name": self.copy_name or "—",
            "ad_title": self.ad_title or "—",
            "ad_body": (self.ad_body or "")[:80],
        }


@dataclass
class AdSetDraft:
    name: str
    targeting_label: str = ""
    os: str = "All"
    age_min: int = 18
    age_max: int = 65
    daily_budget: float | None = None
    ads: list[AdDraft] = field(default_factory=list)

    def to_preview_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "targeting": self.targeting_label or "—",
            "os": self.os,
            "age": f"{self.age_min}-{self.age_max}",
            "daily_budget": self.daily_budget,
            "ads": [a.to_preview_dict() for a in self.ads],
        }


@dataclass
class CampaignDraft:
    """One campaign to create in Ads Manager UI.

    status values:
      "DRAFT"  — fill wizard fully, do NOT click Publish (default)
      "ACTIVE" — fill wizard, click Publish → live campaign
    """

    name: str
    country: str
    account_label: str
    objective: str = "Leads"
    status: str = "DRAFT"
    link: str = ""
    pixel_id: str = ""
    language: str = ""
    page_id: str | None = None
    ad_account_id: str | None = None
    ad_sets: list[AdSetDraft] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ad_count(self) -> int:
        return sum(len(ad_set.ads) for ad_set in self.ad_sets)

    def to_preview_dict(self) -> dict[str, Any]:
        return {
            "Campaign Name": self.name,
            "Countries": self.country,
            "Account": self.account_label,
            "Ad Account ID": self.ad_account_id or "—",
            "Page ID": self.page_id or "—",
            "Status": self.status,
            "Objective": self.objective,
            "Link": self.link or "—",
            "Pixel": self.pixel_id or "—",
            "Language": self.language or "—",
            "ad_sets": [a.to_preview_dict() for a in self.ad_sets],
            "errors": self.errors,
            "warnings": self.warnings,
        }


@dataclass
class RpaPublishResult:
    campaign_name: str
    success: bool = False
    error: str | None = None
    steps_completed: list[str] = field(default_factory=list)
    screenshot_paths: list[str] = field(default_factory=list)
    campaign_id: str | None = None
    adset_id: str | None = None
    ad_ids: list[str] = field(default_factory=list)

    def to_job_dict(self) -> dict[str, Any]:
        return {
            "campaign_name": self.campaign_name,
            "success": self.success,
            "campaign_id": self.campaign_id,
            "adset_id": self.adset_id,
            "creative_id": None,
            "ad_id": self.ad_ids[0] if self.ad_ids else None,
            "ad_ids": self.ad_ids,
            "error": self.error,
            "steps_completed": self.steps_completed,
            "screenshots": self.screenshot_paths,
        }
