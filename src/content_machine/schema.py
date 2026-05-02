"""Content item data model and enums.

Every piece of content flows through a well-defined status lifecycle.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


# ── Enums ────────────────────────────────────────────────────────────────────

class ContentStatus(str, Enum):
    IDEA = "idea"
    BRIEF = "brief"
    DRAFT = "draft"
    COMPLIANCE_REVIEW = "compliance_review"
    NEEDS_REVIEW = "needs_review"
    APPROVED = "approved"
    SCHEDULED = "scheduled"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ContentType(str, Enum):
    EDUCATION = "education"
    SIGNAL_PROOF = "signal_proof"
    RESULT_RECAP = "result_recap"
    SALES = "sales"
    TESTIMONIAL = "testimonial"
    COPYTRADING_OFFER = "copytrading_offer"
    SHORT_VIDEO = "short_video"
    COMMUNITY = "community"
    MARKET_COMMENTARY = "market_commentary"


class CampaignGoal(str, Enum):
    TRUST = "trust"
    ACTIVATION = "activation"
    CONVERSION = "conversion"
    RETENTION = "retention"
    REACTIVATION = "reactivation"


class AudienceSegment(str, Enum):
    FREE_SUBSCRIBERS = "free_subscribers"
    WARM_LEADS = "warm_leads"
    PAID_CLIENTS = "paid_clients"
    COPYTRADING_PROSPECTS = "copytrading_prospects"


# ── Core data objects ────────────────────────────────────────────────────────

@dataclass
class ApprovalEntry:
    role: str
    approved_by: str
    approved_at: str  # ISO timestamp
    version: int
    notes: str = ""


@dataclass
class ComplianceScan:
    risk_tier: str
    flags: list[str] = field(default_factory=list)
    required_disclosures: list[str] = field(default_factory=list)
    scanned_at: str = ""  # ISO timestamp


@dataclass
class PublishMetadata:
    scheduled_time: str = ""  # ISO timestamp
    telegram_chat_id: str = ""
    telegram_message_id: str = ""
    media_ids: list[str] = field(default_factory=list)
    retries: int = 0
    last_error: str = ""


@dataclass
class PostMetrics:
    views: int = 0
    reactions: int = 0
    comments: int = 0
    forwards: int = 0
    link_clicks: int = 0
    leads: int = 0
    conversions: int = 0
    collected_at: str = ""  # ISO timestamp


@dataclass
class ContentItem:
    """Central content object — tracks a post from idea through publication."""

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    updated_at: str = ""

    status: str = ContentStatus.IDEA.value
    content_type: str = ContentType.EDUCATION.value
    campaign_goal: str = CampaignGoal.TRUST.value
    audience_segment: str = AudienceSegment.FREE_SUBSCRIBERS.value

    # Brief / calendar slot
    brief: str = ""
    topic: str = ""
    calendar_slot: str = ""  # ISO date of intended publish day

    # Draft content
    title: str = ""
    body: str = ""
    media_paths: list[str] = field(default_factory=list)
    version: int = 1

    # Compliance
    compliance: ComplianceScan | None = None

    # Approval chain
    approvals: list[ApprovalEntry] = field(default_factory=list)
    required_approvers: list[str] = field(default_factory=list)

    # Publishing
    publish: PublishMetadata = field(default_factory=PublishMetadata)

    # Post-publish analytics
    metrics: PostMetrics = field(default_factory=PostMetrics)

    # Prompt/model provenance
    prompt_hash: str = ""
    model_used: str = ""

    def is_fully_approved(self) -> bool:
        approved_roles = {a.role for a in self.approvals if a.version == self.version}
        return all(r in approved_roles for r in self.required_approvers)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a flat dict suitable for JSON/SQLite storage."""
        import json

        def _ser(obj: Any) -> Any:
            if hasattr(obj, "__dataclass_fields__"):
                return {k: _ser(v) for k, v in obj.__dict__.items()}
            if isinstance(obj, list):
                return [_ser(i) for i in obj]
            return obj

        d = _ser(self)
        for key in ("compliance", "approvals", "publish", "metrics", "media_paths",
                     "required_approvers"):
            if key in d and not isinstance(d[key], str):
                d[key] = json.dumps(d[key], ensure_ascii=False)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ContentItem":
        """Deserialize from a flat dict (e.g. SQLite row)."""
        import json

        item = cls()
        for key, val in d.items():
            if key in ("compliance", "approvals", "publish", "metrics", "media_paths",
                        "required_approvers") and isinstance(val, str):
                val = json.loads(val) if val else None
            if hasattr(item, key):
                if key == "compliance" and isinstance(val, dict):
                    val = ComplianceScan(**val)
                elif key == "approvals" and isinstance(val, list):
                    val = [ApprovalEntry(**a) if isinstance(a, dict) else a for a in val]
                elif key == "publish" and isinstance(val, dict):
                    val = PublishMetadata(**val)
                elif key == "metrics" and isinstance(val, dict):
                    val = PostMetrics(**val)
                setattr(item, key, val)
        return item
