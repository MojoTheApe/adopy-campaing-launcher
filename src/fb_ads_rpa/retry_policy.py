"""Explicit retry contract for the recorded Meta Ads Manager flow.

There are two very different recovery scopes in this flow:

* Before the editor reaches a usable ad, a failed campaign shell can be
  abandoned and rebuilt from the campaigns list.  Those stages are listed in
  ``CAMPAIGN_RETRY_STEPS`` and get at most ``MAX_CAMPAIGN_ATTEMPTS`` attempts.
* After audience or ad creation has started, replaying the whole campaign can
  create duplicate drafts or ads.  Those failures are returned for human
  review and make the draft non-publishable; the individual DOM helpers own
  any safe local retries.

Unknown stages never become retryable by accident.  They fail closed and are
blocked for review until somebody adds an explicit policy entry and test.
"""
from __future__ import annotations

from dataclasses import dataclass

RETRY_CAMPAIGN = "retry_campaign"
BLOCK_AND_REVIEW = "block_and_review"
MAX_CAMPAIGN_ATTEMPTS = 3

# Every non-empty hard-failure string currently returned by
# meta_dom.run_recorded_campaign_flow.  A drift test reads that function's AST
# so a new return value cannot silently lose its retry decision.
CAMPAIGN_RETRY_STEPS = frozenset(
    {
        "create",
        "objective_leads",
        "objective_continue",
        "campaign_name",
        "daily_budget",
        "next_to_adset",
        "adset_name",
        "conversion_location_open",
        "conversion_website",
        "conversion_website_verify",
        "further_limit_reach",
        "further_limit_reach_radio",
        "further_limit_reach_continue",
        "next_to_ad",
    }
)

# Canonical late-stage failures emitted through soft_fails_out.  They are
# "soft" only in the sense that execution continues far enough to preserve a
# useful draft.  The publication verdict is blocking for every one of them.
BLOCKING_REVIEW_STEPS = frozenset(
    {
        "conversion_event",
        "audience_age",
        "audience_location",
        "languages",
        "targeting_open",
        "targeting_none_added",
        "targeting",
        "placements",
        "devices_ios",
        "ad_create",
        "ad_name",
        "ad_creative_upload",
        "ad_copy",
        "ad_destination",
        "destination_sweep",
    }
)


@dataclass(frozen=True)
class RetryDisposition:
    action: str
    max_attempts: int
    rationale: str


def disposition(step: str) -> RetryDisposition:
    """Return the recovery scope for a recorded-flow failure."""
    name = (step or "").split(":", 1)[0].strip()
    if name in CAMPAIGN_RETRY_STEPS:
        return RetryDisposition(
            RETRY_CAMPAIGN,
            MAX_CAMPAIGN_ATTEMPTS,
            "the flow has not produced a trustworthy ad yet, so rebuilding "
            "the campaign shell is bounded and safe",
        )
    return RetryDisposition(
        BLOCK_AND_REVIEW,
        1,
        "a whole-campaign replay could create duplicate drafts or ads; keep "
        "the partial draft blocked for human review",
    )


def should_retry_campaign(step: str, *, completed_attempts: int) -> bool:
    policy = disposition(step)
    return policy.action == RETRY_CAMPAIGN and completed_attempts < policy.max_attempts
