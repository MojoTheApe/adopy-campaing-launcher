"""Facebook Ads Manager RPA — Playwright + antidetect browser (no Marketing API token)."""

from src.fb_ads_rpa.draft import AdDraft, AdSetDraft, CampaignDraft, RpaPublishResult
from src.fb_ads_rpa.build_draft import slots_to_campaign_drafts

__all__ = [
    "AdDraft",
    "AdSetDraft",
    "CampaignDraft",
    "RpaPublishResult",
    "slots_to_campaign_drafts",
    "RpaPublisher",
]


def __getattr__(name: str):
    if name == "RpaPublisher":
        from src.fb_ads_rpa.rpa_publisher import RpaPublisher

        return RpaPublisher
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
