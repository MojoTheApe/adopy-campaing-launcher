"""Approval matrix — who must approve content before it can be scheduled.

Roles:
  - editor   : default reviewer for all content
  - founder  : named human for high-risk and sales content
  - legal    : reserved for content with regulatory implications
"""

from __future__ import annotations

from src.content_machine.policy.compliance import RiskTier

# Maps (risk_tier) → list of required approver roles
APPROVAL_MATRIX: dict[RiskTier, list[str]] = {
    RiskTier.LOW: ["editor"],
    RiskTier.MEDIUM: ["editor"],
    RiskTier.HIGH: ["editor", "founder"],
    RiskTier.BLOCKED: [],  # blocked content cannot be approved at all
}

# Content types that always require founder approval regardless of risk scan
_FOUNDER_REQUIRED_TYPES: set[str] = {
    "copytrading_offer",
    "testimonial",
    "sales",
}


def get_required_approvers(content_type: str, risk_tier: RiskTier) -> list[str]:
    """Return the list of approver roles needed for this content.

    Returns empty list for BLOCKED content — it must be rewritten first.
    """
    if risk_tier == RiskTier.BLOCKED:
        return []

    approvers = list(APPROVAL_MATRIX.get(risk_tier, ["editor"]))
    if content_type in _FOUNDER_REQUIRED_TYPES and "founder" not in approvers:
        approvers.append("founder")
    return approvers
