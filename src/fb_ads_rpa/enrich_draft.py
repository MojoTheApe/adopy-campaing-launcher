"""Resolve Upload FB + creatives folder into CampaignDraft fields."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from config.paths import get_ad_account_map_path, get_creatives_root, get_upload_fb_excel_path
from src.meta_campaign_agent.ad_account_map import load_ad_account_map, resolve_account_entry
from src.meta_campaign_agent.creative_finder import AmbiguousCreativeError, CreativeNotFoundError, find_creative_file
from src.upload_agent.build_upload_excel import (
    get_account_info,
    load_reference_data,
    lookup_country,
    lookup_creative_file,
    lookup_interest,
)
from src.web.ad_copy_gsheet import lookup_ad_copy_by_title


def _interest_names_from_targeting(ref: dict, targeting_name: str) -> str:
    """Resolve a named targeting profile (e.g. "Day") to a comma-separated
    list of real Meta interest/behavior names, for typing one at a time into
    the browser's Detailed Targeting search box.

    "targeting" in the slot/plan data is a KEY into the Upload FB.xlsx
    "Interests" sheet (see lookup_interest), not literal text to type —
    confirmed live 2026-07-25: "Day" resolves to a JSON blob of real
    interests like "Swing trading"/"Day trading"/"Stock market index". The
    RPA browser path needs plain names (it types each one into a search
    box and picks an autocomplete suggestion), unlike the Graph-API publish
    path which can pass this same JSON directly as a structured spec.
    """
    targeting_name = (targeting_name or "").strip()
    if not targeting_name:
        return ""
    raw = lookup_interest(ref, targeting_name)
    if not raw:
        return targeting_name
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return targeting_name
    names: list[str] = []
    groups = parsed if isinstance(parsed, list) else [parsed]
    for group in groups:
        if not isinstance(group, dict):
            continue
        for bucket in ("interests", "behaviors"):
            for item in group.get(bucket) or []:
                name = (item or {}).get("name")
                if name:
                    names.append(str(name).strip())
    return ", ".join(names) if names else targeting_name

_GCC_ARABIC_COUNTRIES = {"KW", "SA", "AE", "BH", "OM", "QA"}
_LATAM_SPANISH_COUNTRIES = {
    "CL", "CO", "EC", "MX", "CR", "PE", "AR", "PY", "SV", "GT", "BO", "DO", "PA", "UY",
}


def _geo_language_for_country(country_code: str) -> str:
    """Business rule: GCC/Arabic countries -> Arabic, LatAm -> Spanish (all).

    Confirmed with user (2026-07-27): this account only ever runs these two
    campaign families, so the country code alone reliably determines the
    ad's audience language — used as the fallback when the Upload FB.xlsx
    "Countries" sheet doesn't have an explicit language value for that row.
    """
    # Pass the base name only — fill_audience_languages() already searches
    # for a "<name> (all)" suggestion first and falls back to the first
    # match if there isn't one (confirmed live: Arabic has no such variant).
    c = (country_code or "").strip().upper()
    if c in _GCC_ARABIC_COUNTRIES:
        return "Arabic"
    if c in _LATAM_SPANISH_COUNTRIES:
        return "Spanish"
    return ""


_ref_cache: dict | None = None
_ref_cache_mtime: float = -1.0
_ref_cache_lock = __import__("threading").Lock()


def _get_ref(excel_path: str) -> dict:
    global _ref_cache, _ref_cache_mtime
    try:
        mtime = os.path.getmtime(excel_path)
    except OSError:
        return {}
    with _ref_cache_lock:
        if _ref_cache is not None and mtime == _ref_cache_mtime:
            return _ref_cache
        try:
            ref = load_reference_data(excel_path)
        except Exception as exc:
            return {"_load_error": str(exc)}
        _ref_cache = ref
        _ref_cache_mtime = mtime
        return ref


def enrich_campaign_drafts(
    drafts: list[Any],
    *,
    account_label: str,
    upload_fb_path: str | None = None,
    ad_account_map_path: str | None = None,
    creatives_root: str | None = None,
    preview_mode: bool = False,
) -> list[Any]:
    """Fill link, pixel, page_id, creative paths, ad copy on each draft."""
    excel_path = (upload_fb_path or "").strip() or get_upload_fb_excel_path()
    map_path = (ad_account_map_path or "").strip() or get_ad_account_map_path()
    root = (creatives_root or "").strip() or get_creatives_root()

    ref: dict = {}
    if os.path.isfile(excel_path):
        ref = _get_ref(excel_path)

    account_map = load_ad_account_map(map_path)
    entry = resolve_account_entry(account_map, account_label)
    account_info = get_account_info(ref, account_label) if ref else {}

    for draft in drafts:
        draft.ad_account_id = entry.ad_account_id if entry else None
        draft.page_id = (entry.page_id if entry else None)
        draft.link = str(account_info.get("link") or "")
        draft.pixel_id = str(account_info.get("pixel") or "")
        # The GCC/LatAm rule is authoritative for this account's two known
        # campaign families (confirmed with user 2026-07-27) — checked
        # first so it can't be silently overridden by unexpected text in
        # the Upload FB.xlsx "Countries" sheet's language column (that
        # sheet's format isn't guaranteed to match what fill_audience_
        # languages() expects to type). Excel is only a fallback for
        # countries outside those two families.
        excel_language = str(lookup_country(ref, draft.country).get("language") or "").strip() if ref else ""
        draft.language = _geo_language_for_country(draft.country) or excel_language

        if ref.get("_load_error"):
            draft.warnings.append(f"Upload FB.xlsx not loaded: {ref['_load_error']}")
        if not entry:
            draft.errors.append("Account not in ad_account_map.csv — add row and re-run")
            # remaining checks require entry; skip to avoid duplicate noise
            continue
        if not entry.ad_account_id:
            draft.errors.append("ad_account_id not mapped in ad_account_map.csv")
        if not draft.link:
            draft.errors.append("Link empty in Upload FB Accounts sheet")
        # page_id is only required for ACTIVE campaigns; drafts publish without it
        if not draft.page_id:
            draft.warnings.append("page_id not set — required for ACTIVE campaigns only")
        if not entry.profile_id:
            if preview_mode:
                draft.warnings.append("profile_id missing in ad_account_map.csv (set via ML panel before publishing)")
            else:
                draft.errors.append("profile_id missing in ad_account_map.csv (browser session)")

        for ad_set in draft.ad_sets:
            if getattr(ad_set, "targeting_label", ""):
                # Primary source (user direction, 2026-07-30): the
                # "Detailed targeting" tab of the shared Google Sheet —
                # names come back already comma-separated, no JSON parsing.
                # The Excel "Interests" sheet remains the fallback so a
                # Sheets outage or a missing profile can't break publishing.
                resolved = ""
                try:
                    from src.analysis_agent.gsheet_targeting import lookup_targeting_interests

                    resolved = lookup_targeting_interests(ad_set.targeting_label)
                except Exception:
                    resolved = ""
                if resolved:
                    ad_set.targeting_label = resolved
                elif ref:
                    draft.warnings.append(
                        f"targeting {ad_set.targeting_label!r} not in Google Sheet "
                        "'Detailed targeting' — used Excel Interests fallback"
                    )
                    ad_set.targeting_label = _interest_names_from_targeting(ref, ad_set.targeting_label)

            for ad in ad_set.ads:
                # If creative_filename was set directly from folder scan — skip Excel lookup
                if not ad.creative_filename:
                    if ref:
                        ad.creative_filename = lookup_creative_file(ref, ad.creative_type, ad.creative_id) or ""
                    if not ad.creative_filename:
                        draft.errors.append(
                            f"Creative file not mapped for {ad.creative_id or '?'} "
                            f"(pick a file from the folder or set creative_id in Upload FB)"
                        )

                # "Ad Title" — Google Sheet is the canonical source (was the Excel
                # "Ad copy" sheet's "Ad copy name"; see fb_agents Obsidian diary,
                # 2026-07-28). Independent of `ref` (Excel), which only backs the
                # creative-file/interest/country lookups below.
                if ad.copy_name:
                    copy = lookup_ad_copy_by_title(ad.copy_name)
                    ad.ad_title = (copy.get("headline") or ad.copy_name or "").strip()
                    ad.ad_body = (copy.get("body") or "").strip()
                    ad.ad_link_description = (copy.get("description") or "").strip()

                if ad.creative_filename and not preview_mode:
                    try:
                        ad.creative_path = str(find_creative_file(root, ad.creative_filename))
                    except (CreativeNotFoundError, AmbiguousCreativeError, FileNotFoundError) as exc:
                        draft.errors.append(str(exc))
                elif ad.creative_filename:
                    ad.creative_path = str(Path(root) / ad.creative_filename)

                if not ad.ad_title:
                    draft.warnings.append(f"Ad title empty for creative {ad.creative_id or '?'}")

    return drafts
