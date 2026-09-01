"""Ad copy ("ad texts") lookup — the Google Sheet is the canonical source,
replacing the old Excel "Ad copy" sheet (build_upload_excel.lookup_ad_copy).

Same spreadsheet as the KB targets sync (GSHEET_TARGETS_ID), different tab.
Columns: Ad Title | Headline | Body | Description | Asset | Language.
"Ad Title" is the lookup key (played by "Ad copy name" in the old Excel sheet);
Headline/Body/Description map to AdDraft.ad_title/ad_body/ad_link_description.
"""
from __future__ import annotations

import os
import time
from typing import Any, Dict, Optional

_CACHE_TTL_SECONDS = 300  # small dataset (~280 rows) — cheap to refresh every 5 min
_cache: Optional[Dict[str, Dict[str, str]]] = None
_cache_time: float = 0.0


def _build_service():
    from config.paths import get_google_oauth_client_path, get_google_oauth_token_path
    from src.analysis_agent.gsheet_accounts import _build_sheets_service_oauth

    client_path = get_google_oauth_client_path()
    token_path = get_google_oauth_token_path()
    return _build_sheets_service_oauth(client_path, token_path)


def _fetch_rows() -> list[list[str]]:
    # Same spreadsheet as GSHEET_TARGETS_ID by default — different tab (GSHEET_ADCOPY_SHEET).
    sheet_id = (os.environ.get("GSHEET_ADCOPY_ID") or os.environ.get("GSHEET_TARGETS_ID") or "").strip()
    sheet_name = os.environ.get("GSHEET_ADCOPY_SHEET", "ad texts").strip()
    if not sheet_id:
        return []
    service = _build_service()
    if service is None:
        return []
    result = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=sheet_id, range=f"{sheet_name}!A:F")
        .execute()
    )
    return result.get("values", [])


def reload_cache() -> None:
    global _cache, _cache_time
    _cache = None
    _cache_time = 0.0


def load_ad_copy() -> Dict[str, Dict[str, str]]:
    """Return {ad_title: {headline, body, description, asset, language}}, cached ~5 min."""
    global _cache, _cache_time
    now = time.time()
    if _cache is not None and (now - _cache_time) < _CACHE_TTL_SECONDS:
        return _cache

    try:
        rows = _fetch_rows()
    except Exception:
        # Network/auth hiccup — keep serving the stale cache rather than blanking it out.
        return _cache if _cache is not None else {}

    out: Dict[str, Dict[str, str]] = {}
    if rows:
        header = [str(h).strip().lower() for h in rows[0]]

        def col(name: str) -> int:
            return header.index(name) if name in header else -1

        idx_title = col("ad title")
        idx_headline = col("headline")
        idx_body = col("body")
        idx_desc = col("description")
        idx_asset = col("asset")
        idx_lang = col("language")

        def cell(row: list, i: int) -> str:
            return str(row[i]).strip() if 0 <= i < len(row) and row[i] else ""

        for row in rows[1:]:
            key = cell(row, idx_title)
            if not key:
                continue
            out[key] = {
                "headline": cell(row, idx_headline),
                "body": cell(row, idx_body),
                "description": cell(row, idx_desc),
                "asset": cell(row, idx_asset),
                "language": cell(row, idx_lang),
            }

    _cache = out
    _cache_time = now
    return out


def lookup_ad_copy_by_title(ad_title: Optional[str]) -> Dict[str, str]:
    """Return {headline, body, description} for an Ad Title — exact match first,
    then substring either direction (mirrors the old Excel lookup_ad_copy()'s
    matching behavior so callers don't need to change their expectations)."""
    key = (ad_title or "").strip()
    if not key:
        return {"headline": "", "body": "", "description": ""}

    data = load_ad_copy()
    entry = data.get(key)
    if entry is None:
        for name, candidate in data.items():
            if key in name or name in key:
                entry = candidate
                break
    if entry is None:
        return {"headline": key, "body": "", "description": ""}
    return {"headline": entry["headline"], "body": entry["body"], "description": entry["description"]}
