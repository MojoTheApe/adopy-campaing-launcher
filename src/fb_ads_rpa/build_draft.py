"""Build CampaignDraft trees from dashboard slots (group ads under one ad set)."""
from __future__ import annotations

import re
from collections import OrderedDict
from typing import Any


def _parse_age(age: str) -> tuple[int, int]:
    raw = (age or "35").strip()
    nums = re.findall(r"\d+", raw)
    if len(nums) >= 2:
        return int(nums[0]), int(nums[1])
    if len(nums) == 1:
        n = int(nums[0])
        return n, 65
    return 18, 65


def _parse_budget(raw: str) -> float | None:
    text = (raw or "").strip().replace("$", "").replace(",", "")
    if not text:
        return None
    try:
        val = float(text)
        return val if val > 0 else None
    except ValueError:
        return None


def slots_to_campaign_drafts(
    slots: list[dict[str, Any]],
    *,
    account_label: str,
    country: str,
    activate: bool = False,
) -> list["CampaignDraft"]:
    """Group slots by campaign name → one ad set with N ads per campaign."""
    from src.fb_ads_rpa.draft import AdDraft, AdSetDraft, CampaignDraft

    grouped: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
    for slot in slots:
        name = (slot.get("name") or "").strip()
        if not name:
            continue
        grouped.setdefault(name, []).append(slot)

    status = "ACTIVE" if activate else "DRAFT"
    drafts: list[CampaignDraft] = []

    for campaign_name, group in grouped.items():
        warnings: list[str] = []
        first = group[0]
        targeting = (first.get("targeting") or "").strip()
        os_val = (first.get("os") or "All").strip()
        age_min, age_max = _parse_age(str(first.get("age") or "35"))
        budget = _parse_budget(str(first.get("budget") or ""))

        for slot in group[1:]:
            if (slot.get("targeting") or "").strip() != targeting:
                warnings.append(f"Mixed targeting in {campaign_name!r} — using first slot")
            if (slot.get("budget") or "").strip() and _parse_budget(str(slot.get("budget"))) != budget:
                warnings.append(f"Mixed budget in {campaign_name!r} — using first slot")

        # ACL-1775: Instant Form - ключи есть только у слотов с формой; старые слоты прежние.
        destination = (first.get("destination") or "website").strip()

        ads = [
            AdDraft(
                creative_type=(s.get("creative_type") or "Image").strip(),
                creative_id=(s.get("creative_id") or "").strip(),
                # creative_file: direct filename from folder scan (bypasses Excel lookup)
                creative_filename=(s.get("creative_file") or "").strip(),
                copy_name=(s.get("copy") or "").strip(),
                copy2_name=(s.get("copy2") or "").strip(),
                lead_form_id=(s.get("lead_form_id") or "").strip() if destination == "instant_form" else "",
                cta=(s.get("cta") or "").strip(),
                # ACL-1774: ключи есть в слоте только когда поле заполнено.
                more_copy_names=[str(x).strip() for x in (s.get("copy_more") or []) if str(x).strip()],
                vertical_filename=(s.get("creative_file_vertical") or "").strip(),
                lead_form_template=((s.get("lead_form_template") or "").strip()
                                    if destination == "instant_form" else ""),
            )
            for s in group
        ]

        ad_set = AdSetDraft(
            name=campaign_name,
            targeting_label=targeting,
            os=os_val,
            age_min=age_min,
            age_max=age_max,
            daily_budget=budget,
            ads=ads,
            destination=destination,
            # ACL-1767: ключи есть в слоте только когда поле заполнено.
            gender=(first.get("gender") or "").strip(),
            languages=[str(x).strip() for x in (first.get("languages") or []) if str(x).strip()],
            device=(first.get("device") or "").strip(),
            os_min=(first.get("os_min") or "").strip(),
            start_time=(first.get("start_time") or "").strip(),
        )

        tracking_link = (first.get("tracking_link") or "").strip()
        # Preserve explicit emptiness; only omitted fields use legacy defaults.
        language = (first.get("language") or "").strip()
        draft = CampaignDraft(
            name=campaign_name,
            country=country,
            account_label=account_label,
            status=status,
            link=tracking_link,
            language=language,
            link_provided=first.get("tracking_link_provided", "tracking_link" in first),
            language_provided=first.get("language_provided", "language" in first),
            ad_sets=[ad_set],
            warnings=warnings,
        )
        if not ads:
            draft.errors.append("No ads in campaign")
        if budget is None:
            draft.errors.append("Daily budget missing")
        drafts.append(draft)

    return drafts
