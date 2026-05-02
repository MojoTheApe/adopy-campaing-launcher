"""Compliance Agent.

Scans draft content for policy violations and assigns risk tier,
required disclosures, and required approvers.
"""

from __future__ import annotations

import logging
from datetime import datetime

from src.content_machine.policy.approval_matrix import get_required_approvers
from src.content_machine.policy.compliance import classify_risk
from src.content_machine.policy.disclosures import get_required_disclosures
from src.content_machine.schema import ComplianceScan, ContentItem, ContentStatus

log = logging.getLogger(__name__)


def scan(item: ContentItem) -> ContentItem:
    """Run compliance checks on a draft ContentItem.

    Updates item.compliance, item.required_approvers, and item.status in-place.
    Does NOT call an LLM — this is rule-based for speed and determinism.
    """
    return scan_body(item, item.body)


def scan_body(item: ContentItem, body: str) -> ContentItem:
    """Like `scan`, but classify risk for an arbitrary body (e.g. pre-disclosure draft)."""
    risk_tier, flags = classify_risk(item.content_type, body)
    disclosures = get_required_disclosures(item.content_type)
    approvers = get_required_approvers(item.content_type, risk_tier)

    item.compliance = ComplianceScan(
        risk_tier=risk_tier,
        flags=flags,
        required_disclosures=disclosures,
        scanned_at=datetime.utcnow().isoformat(),
    )
    item.required_approvers = approvers

    if risk_tier == "blocked":
        item.status = ContentStatus.CANCELLED.value
        log.warning("Item %s BLOCKED by compliance: %s", item.id, flags)
    else:
        item.status = ContentStatus.NEEDS_REVIEW.value
        log.info(
            "Item %s passed compliance: risk=%s, flags=%d, approvers=%s",
            item.id, risk_tier, len(flags), approvers,
        )

    return item
