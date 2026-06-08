"""In-memory campaign bundle for Excel export and future API publish."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class CampaignBundle:
    account_sheet: str
    account_label: str
    country: str
    plan_rows: list[dict[str, Any]] = field(default_factory=list)

    def to_workbook_bytes(self, ref: dict) -> bytes:
        from src.campaign_bridge.assembly import workbook_bytes_from_plan

        return workbook_bytes_from_plan(ref, self.account_label, self.plan_rows)
