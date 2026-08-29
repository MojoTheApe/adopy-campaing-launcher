"""Campaign tree for RPA publish: Campaign → Ad Set(s) → Ad(s)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# Dimensions, not creative numbers: "1080x1080", "1080x1920", and the same
# with a Cyrillic "х" — which really occurs in the folder
# ("text5_img2_1080х1920.png"), so both letters have to be stripped.
_DIMENSIONS_RE = re.compile(r"\d{3,4}\s*[xх×]\s*\d{3,4}", re.I)
# Creative numbers observed across the real folder (4717 files) are always
# four digits: CAC-1253, cac-2285, 1_eslat_2243, 2175_ADN3, "1328 - 26s".
# The boundaries keep it from matching inside longer runs like
# "516594629_1264482105091024".
_CREATIVE_NUM_RE = re.compile(r"(?<!\d)(\d{4})(?!\d)")


def _creative_number(filename: str) -> str:
    """Pull the creative number out of a file name ("…_2243_square.mp4" ->
    "2243"). Returns "" when the name carries no number to speak of — a
    plain "coffee.mp4" or a pure-dimensions "1080x1080-17.png"."""
    stem = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if "." in stem:
        stem = stem.rsplit(".", 1)[0]
    m = _CREATIVE_NUM_RE.search(_DIMENSIONS_RE.sub(" ", stem))
    return m.group(1) if m else ""


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
            creative = _creative_number(self.creative_filename)
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
    # Ad-level steps that failed while the run as a whole still reached the
    # end. `success` stays True because the draft really was created — but a
    # draft whose ad has no creative must not be published, and until now the
    # only trace of that was a telemetry line nobody reads before clicking
    # Publish (run 58c3cb9b53c6: ad3 had no media, result said success).
    warnings: list[str] = field(default_factory=list)
    # Steps whose failure makes the draft unsafe to publish, as opposed to
    # merely imperfect. Separate from `warnings` because the operator needs to
    # know which of the two they are looking at without reading the text.
    blocking_failures: list[str] = field(default_factory=list)

    @property
    def publishable(self) -> bool:
        """Did the run finish AND produce a draft a human may publish?

        `success` answers only the first half — the automation reached the end
        and a draft exists. Treating it as the second half is how a draft whose
        ad had no creative came back green. Keep the two apart: `success` is
        about execution, `publishable` is about the campaign.
        """
        return bool(self.success) and not self.blocking_failures

    def to_job_dict(self) -> dict[str, Any]:
        return {
            "campaign_name": self.campaign_name,
            "success": self.success,
            # Everything the operator needs in order NOT to publish a broken
            # draft. `warnings` existed on the object but was never serialized,
            # so the layer that shows results to a human never received it.
            "publishable": self.publishable,
            "warnings": self.warnings,
            "blocking_failures": self.blocking_failures,
            "campaign_id": self.campaign_id,
            "adset_id": self.adset_id,
            "creative_id": None,
            "ad_id": self.ad_ids[0] if self.ad_ids else None,
            "ad_ids": self.ad_ids,
            "error": self.error,
            "steps_completed": self.steps_completed,
            "screenshots": self.screenshot_paths,
        }
