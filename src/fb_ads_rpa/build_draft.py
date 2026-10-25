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


def parse_adset_index(raw: Any, count: int | None = None) -> int | None:
    """Номер набора строго: целое в [0, count) (без `count` - в [0, MAX_AD_SETS)).

    Всё остальное ("abc", "-1", "1.5", "", None, 7 при двух наборах) - None: такой номер
    не угадывается и не прячется в набор 0 (ACL-1768, ревью).
    """
    from src.fb_ads_rpa.draft import MAX_AD_SETS

    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, int):
        k = raw
    elif isinstance(raw, str) and raw.strip().isascii() and raw.strip().isdigit():
        k = int(raw.strip())
    else:
        return None
    limit = MAX_AD_SETS if count is None else min(count, MAX_AD_SETS)
    return k if 0 <= k < limit else None


def bad_adset_message(ad_no: int, raw: Any) -> str:
    """Отказ вслух: объявление указывает на набор, которого нет. Номер набора - как в билдере (с 1)."""
    shown = raw
    if isinstance(raw, (int, str)) and not isinstance(raw, bool) and str(raw).strip().lstrip("-").isdigit():
        shown = int(str(raw).strip()) + 1
    return f"Ad {ad_no} points to ad set {shown}, which does not exist. Nothing was created."


def slots_to_campaign_drafts(
    slots: list[dict[str, Any]],
    *,
    account_label: str,
    country: str,
    activate: bool = False,
) -> list["CampaignDraft"]:
    """Слоты → кампании по имени; внутри кампании наборы по явному `adset_index` (ACL-1768).

    Имя склеивает слоты только в КАМПАНИЮ (ключ статистики и метки API). В один набор
    слоты склеивает лишь одинаковый `adset_index`; слот без ключа (старый черновик) - набор 0,
    так что прежняя форма «одна кампания = один набор с N объявлениями» читается как раньше.
    """
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
        by_set: dict[int, list[dict[str, Any]]] = {}
        slot_errors: list[str] = []
        for pos, slot in enumerate(group, start=1):
            for msg in slot.get("form_errors") or []:
                if msg not in slot_errors:
                    slot_errors.append(msg)
            # Нет ключа - набор 0 (старые формы и черновики); есть, но не целое в [0, число наборов) - отказ.
            if "adset_index" not in slot:
                k = 0
            else:
                count = slot.get("adset_count")
                k = parse_adset_index(slot.get("adset_index"),
                                      count if isinstance(count, int) and not isinstance(count, bool) else None)
            if k is None:
                msg = bad_adset_message(pos, slot.get("adset_index"))
                if msg not in slot_errors:
                    slot_errors.append(msg)
                continue
            by_set.setdefault(k, []).append(slot)
        order = sorted(by_set)
        first_campaign = (by_set[order[0]] if order else group)[0]
        # Бюджет - на кампании (CBO): берётся у первого слота и общий для всех наборов.
        budget = _parse_budget(str(first_campaign.get("budget") or ""))

        ad_sets: list[AdSetDraft] = []
        for idx in order:
            sgroup = by_set[idx]
            first = sgroup[0]
            targeting = (first.get("targeting") or "").strip()
            os_val = (first.get("os") or "All").strip()
            age_min, age_max = _parse_age(str(first.get("age") or "35"))
            for slot in sgroup[1:]:
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
                    lead_form_template=((s.get("lead_form_template") or "").strip()
                                        if destination == "instant_form" else ""),
                )
                for s in sgroup if not s.get("no_ad")
            ]
            # Имя набора: своё, иначе имя кампании (набор 0, как всегда) или «<кампания> - Ad set N».
            own = (first.get("adset_name") or "").strip()
            default = campaign_name if idx == 0 else f"{campaign_name} - Ad set {idx + 1}"
            ad_sets.append(AdSetDraft(
                name=own or default,
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
                adset_index=idx,
                # Язык набора 0 - язык кампании; у остальных свой, пусто = как у кампании.
                language=(first.get("language") or "").strip() if idx != order[0] else "",
                pixel_id=(first.get("pixel_id") or "").strip(),
            ))

        tracking_link = (first_campaign.get("tracking_link") or "").strip()
        # Preserve explicit emptiness; only omitted fields use legacy defaults.
        language = (first_campaign.get("language") or "").strip()
        draft = CampaignDraft(
            name=campaign_name,
            country=country,
            account_label=account_label,
            status=status,
            link=tracking_link,
            language=language,
            link_provided=first_campaign.get("tracking_link_provided", "tracking_link" in first_campaign),
            language_provided=first_campaign.get("language_provided", "language" in first_campaign),
            page_id=str(first_campaign.get("page_id") or "").strip() or None,
            page_provided="page_id" in first_campaign,
            ad_sets=ad_sets,
            warnings=warnings,
        )
        draft.errors.extend(slot_errors)
        if not ad_sets:
            draft.errors.append("No ads in campaign")
        for ad_set in ad_sets:
            if not ad_set.ads:
                draft.errors.append("No ads in campaign" if len(ad_sets) == 1
                                    else f"No ads in ad set {ad_set.name!r}")
        if any(str(s.get("page_id") or "").strip() != str(first_campaign.get("page_id") or "").strip() for s in group):
            draft.errors.append("Campaigns with the same name have different Facebook Page choices. Give each campaign a distinct name.")
        if budget is None:
            draft.errors.append("Daily budget missing")
        drafts.append(draft)

    return drafts
