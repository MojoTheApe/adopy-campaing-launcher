"""Orchestrator Agent.

Owns the content lifecycle state machine. Delegates work to specialist agents
and blocks transitions until all policy gates are satisfied.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Sequence

from src.content_machine.agents import calendar_agent, compliance_agent, editor_agent
from src.content_machine.agents import post_writer, research_agent, sales_copy
from src.content_machine.config import ContentMachineConfig
from src.content_machine.db import ContentDB
from src.content_machine.policy.disclosures import DISCLOSURE_SEPARATOR, strip_disclosures_block
from src.content_machine.schema import ContentItem, ContentStatus, ContentType

log = logging.getLogger(__name__)

_SALES_TYPES = {
    ContentType.SALES.value,
    ContentType.COPYTRADING_OFFER.value,
    ContentType.TESTIMONIAL.value,
}


class Orchestrator:
    """Drives a ContentItem through the full pipeline."""

    def __init__(self, config: ContentMachineConfig, db: ContentDB) -> None:
        self.cfg = config
        self.db = db

    # ── High-level workflows ─────────────────────────────────────────────

    def generate_weekly_plan(
        self,
        start_date: date | None = None,
        context: str = "",
    ) -> list[ContentItem]:
        """Ask calendar agent for a week of briefs, persist them, return list."""
        items = calendar_agent.generate_weekly_plan(
            self.cfg.llm, start_date=start_date, context=context,
        )
        for item in items:
            self.db.save(item)
        log.info("Orchestrator: saved %d calendar briefs", len(items))
        return items

    def process_brief(self, item: ContentItem) -> ContentItem:
        """Take a BRIEF through research → draft → compliance → editor → review."""
        item.body = strip_disclosures_block(item.body or "")
        item.compliance = None
        # 1. Research
        research = research_agent.enrich_brief(self.cfg.llm, item)

        # 2. Draft
        if item.content_type in _SALES_TYPES:
            item = sales_copy.write_sales_draft(self.cfg.llm, item, research)
        else:
            item = post_writer.write_draft(self.cfg.llm, item, research)

        # 3. Editor pass
        item = editor_agent.edit_draft(self.cfg.llm, item)

        # 4. Compliance: classify draft, append disclosures, re-scan on final body
        pre = item.body or ""
        item = compliance_agent.scan_body(item, pre)
        if item.status == ContentStatus.CANCELLED.value:
            self.db.save(item)
            return item
        if item.compliance and item.compliance.required_disclosures:
            block = "\n".join(item.compliance.required_disclosures)
            if block and block not in pre:
                item.body = pre.rstrip() + DISCLOSURE_SEPARATOR + block

        final_body = item.body or ""
        item = compliance_agent.scan_body(item, final_body)

        self.db.save(item)
        return item

    def process_all_briefs(self) -> list[ContentItem]:
        """Process every BRIEF-status item in the DB."""
        briefs = self.db.list_by_status(ContentStatus.BRIEF.value)
        results: list[ContentItem] = []
        for item in briefs:
            try:
                processed = self.process_brief(item)
                results.append(processed)
            except Exception:
                log.exception("Failed to process brief %s", item.id)
        return results

    # ── Approval handling ────────────────────────────────────────────────

    def approve(
        self,
        item_id: str,
        role: str,
        approved_by: str,
        notes: str = "",
    ) -> ContentItem | None:
        """Record an approval and advance status if fully approved."""
        from datetime import datetime
        from src.content_machine.schema import ApprovalEntry

        item = self.db.get(item_id)
        if item is None:
            log.error("Item %s not found", item_id)
            return None

        entry = ApprovalEntry(
            role=role,
            approved_by=approved_by,
            approved_at=datetime.utcnow().isoformat(),
            version=item.version,
            notes=notes,
        )
        item.approvals.append(entry)

        if item.is_fully_approved():
            item.status = ContentStatus.APPROVED.value
            log.info("Item %s fully approved → APPROVED", item_id)
        else:
            log.info("Item %s approved by %s (%s), still needs: %s",
                      item_id, approved_by, role,
                      set(item.required_approvers) - {a.role for a in item.approvals})

        self.db.save(item)
        return item

    def reject(self, item_id: str, reason: str = "") -> ContentItem | None:
        """Reject an item — sends it back to BRIEF for regeneration."""
        item = self.db.get(item_id)
        if item is None:
            return None
        item.status = ContentStatus.BRIEF.value
        item.body = ""
        item.approvals.clear()
        item.compliance = None
        self.db.save(item)
        log.info("Item %s rejected → BRIEF (reason: %s)", item_id, reason or "none")
        return item

    def reset_to_brief(self, item_id: str) -> ContentItem | None:
        """Clear draft and compliance, move item back to BRIEF (for relocalize / rebuild)."""
        item = self.db.get(item_id)
        if item is None:
            return None
        item.status = ContentStatus.BRIEF.value
        item.body = ""
        item.approvals.clear()
        item.compliance = None
        self.db.save(item)
        return item

    def relocalize_status(self, status: str) -> int:
        """Move items in *status* back to BRIEF with empty body. Returns count."""
        items = self.db.list_by_status(status)
        n = 0
        for it in items:
            it.status = ContentStatus.BRIEF.value
            it.body = ""
            it.approvals.clear()
            it.compliance = None
            self.db.save(it)
            n += 1
        log.info("Relocalize: moved %d items from %s → BRIEF", n, status)
        return n

    def cancel(self, item_id: str) -> None:
        self.db.update_status(item_id, ContentStatus.CANCELLED.value)
