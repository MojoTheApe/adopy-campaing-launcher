"""Post-generation validation for calendar agent output."""

from __future__ import annotations

import logging

from src.content_machine.schema import (
    AudienceSegment,
    CampaignGoal,
    ContentItem,
    ContentType,
)

log = logging.getLogger(__name__)

_OFFER_TYPES = frozenset(
    {
        ContentType.SALES.value,
        ContentType.COPYTRADING_OFFER.value,
    },
)

_ALLOWED_TYPES = frozenset(e.value for e in ContentType)
_ALLOWED_GOALS = frozenset(e.value for e in CampaignGoal)
_ALLOWED_AUDS = frozenset(e.value for e in AudienceSegment)


def coerce_enums(item: ContentItem) -> list[str]:
    """Normalize enums; return warning strings for fixes."""
    warnings: list[str] = []
    if item.content_type not in _ALLOWED_TYPES:
        old = item.content_type
        item.content_type = ContentType.EDUCATION.value
        warnings.append(f"invalid content_type {old!r} → education")
    if item.campaign_goal not in _ALLOWED_GOALS:
        old = item.campaign_goal
        item.campaign_goal = CampaignGoal.TRUST.value
        warnings.append(f"invalid campaign_goal {old!r} → trust")
    if item.audience_segment not in _ALLOWED_AUDS:
        old = item.audience_segment
        item.audience_segment = AudienceSegment.FREE_SUBSCRIBERS.value
        warnings.append(f"invalid audience_segment {old!r} → free_subscribers")
    return warnings


def validate_plan_entries(
    day_iso: str,
    items: list[ContentItem],
    *,
    max_offer_posts_per_day: int | None = None,
) -> list[str]:
    """Return human-readable validation issues for one calendar day."""
    issues: list[str] = []

    if not items:
        issues.append("no posts for day (empty plan array)")
        return issues

    for i, item in enumerate(items):
        ws = coerce_enums(item)
        for w in ws:
            issues.append(f"slot[{i}] {w}")
        topic = (item.topic or "").strip()
        brief = (item.brief or "").strip()
        if not topic:
            issues.append(f"slot[{i}] empty topic")
        if not brief:
            issues.append(f"slot[{i}] empty brief")
        slot = (item.calendar_slot or "").strip()
        if slot and slot != day_iso.split("T")[0]:
            issues.append(f"slot[{i}] calendar_slot {slot!r} mismatches planned day {day_iso!r}")

    # Adjacent aggressive offers (sales / copytrading_offer back-to-back in plan order).
    types_in_order = [it.content_type for it in items]
    for pair_idx in range(len(types_in_order) - 1):
        a, b = types_in_order[pair_idx], types_in_order[pair_idx + 1]
        if a in _OFFER_TYPES and b in _OFFER_TYPES:
            issues.append(
                f"slots {pair_idx}/{pair_idx+1}: consecutive offer types ({a}, {b}) — avoid back-to-back",
            )

    if max_offer_posts_per_day is not None:
        offers = sum(1 for t in types_in_order if t in _OFFER_TYPES)
        if offers > max_offer_posts_per_day:
            issues.append(
                f"too many commercial posts ({offers} > max {max_offer_posts_per_day} per day)",
            )

    return issues


def validate_week_offer_cap(
    all_items: list[ContentItem],
    max_offer_posts_per_week: int | None,
) -> list[str]:
    if max_offer_posts_per_week is None:
        return []
    offers = sum(1 for it in all_items if it.content_type in _OFFER_TYPES)
    if offers > max_offer_posts_per_week:
        return [
            f"week-wide: commercial posts ({offers}) exceed weekly cap ({max_offer_posts_per_week}); "
            "adjust plan manually or rerun with stricter prompting",
        ]
    return []


def log_week_warnings(messages: list[str]) -> None:
    for msg in messages:
        log.warning("calendar plan constraint: %s", msg)
