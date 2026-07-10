"""Resolve Upload FB + creatives folder into CampaignDraft fields."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from config.paths import get_ad_account_map_path, get_creatives_root, get_upload_fb_excel_path
from src.meta_campaign_agent.ad_account_map import load_ad_account_map, resolve_account_entry
from src.meta_campaign_agent.creative_finder import AmbiguousCreativeError, CreativeNotFoundError, find_creative_file
from src.upload_agent.build_upload_excel import get_account_info, load_reference_data, lookup_ad_copy, lookup_creative_file

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
        if not entry.folder_id or not entry.profile_id:
            missing_fields = ", ".join(f for f in ("folder_id", "profile_id") if not getattr(entry, f, None))
            if preview_mode:
                draft.warnings.append(f"{missing_fields} missing in ad_account_map.csv (set via ML panel before publishing)")
            else:
                draft.errors.append(f"{missing_fields} missing in ad_account_map.csv (browser session)")

        for ad_set in draft.ad_sets:
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

                if ref:
                    copy = lookup_ad_copy(ref, ad.copy_name) if ad.copy_name else {}
                    ad.ad_title = (copy.get("title") or ad.copy_name or "").strip()
                    ad.ad_body = (copy.get("body") or "").strip()
                    ad.ad_link_description = (copy.get("link_description") or "").strip()

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
