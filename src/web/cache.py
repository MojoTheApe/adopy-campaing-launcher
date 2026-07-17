"""mtime-keyed in-process cache for the morning report.

Single-user, single-uvicorn-worker tool. The cache holds:

  - enriched Excel DataFrame (keyed by excel path + mtime) — expensive to rebuild
  - full report dict (GSheet + Excel) — Refresh reuses the DF when Excel unchanged

  - GET /  : if report cache matches Excel mtime, return instantly.
  - POST /refresh : force-rebuild report (reuse Excel DF if mtime unchanged).
  - GET /details : uses cached enriched_df; builds if empty.

Known race (per design 7C, accepted): if /refresh swaps the cache while a
GET / render is mid-flight, the in-flight render may see partially-stale
state. Single-user, sub-second renders, low blast radius.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from src.analysis_agent.morning_service import (
    _resolve_excel_path,
    load_morning_report,
)


_CACHE: Dict[str, Any] = {
    "key": None,            # (excel_path, mtime) for full report — None means empty
    "report": None,         # dict
    "df": None,             # pd.DataFrame (same as excel_df when warm)
    "warnings": [],         # list[str]
    "built_at": None,       # ISO datetime string
    "excel_key": None,      # (excel_path, mtime) for reused DF
    "excel_df": None,       # pd.DataFrame — survives report-only refresh
}


def _current_key(excel_path: str) -> Optional[Tuple[str, float]]:
    if not os.path.isfile(excel_path):
        return None
    return (excel_path, os.path.getmtime(excel_path))


def get_cached_report(
    *, force_rebuild: bool = False
) -> Tuple[Dict[str, Any], pd.DataFrame, List[str]]:
    """Return the latest (report, df, warnings).

    Rebuilds when the Excel file's mtime differs from the cached entry, or
    when force_rebuild is set. On force_rebuild, reuses the enriched DataFrame
    if Excel mtime is unchanged (only GSheet + report assembly re-run).
    """
    excel_path = _resolve_excel_path(None)
    expected_key = _current_key(excel_path)

    if (
        not force_rebuild
        and _CACHE["key"] is not None
        and _CACHE["key"] == expected_key
        and _CACHE["report"] is not None
    ):
        return _CACHE["report"], _CACHE["df"], _CACHE["warnings"]

    reused_df = None
    if (
        expected_key is not None
        and _CACHE.get("excel_key") == expected_key
        and _CACHE.get("excel_df") is not None
    ):
        reused_df = _CACHE["excel_df"]

    report, df, warnings = load_morning_report(enriched_df=reused_df)

    _CACHE["key"] = expected_key
    _CACHE["report"] = report
    _CACHE["df"] = df
    _CACHE["warnings"] = warnings
    _CACHE["excel_key"] = expected_key
    _CACHE["excel_df"] = df
    from datetime import datetime
    _CACHE["built_at"] = datetime.now().isoformat(timespec="seconds")
    return report, df, warnings


def cache_built_at() -> Optional[str]:
    return _CACHE["built_at"]


def cache_clear(*, keep_excel_df: bool = False) -> None:
    """Evict report cache. Optionally keep enriched Excel DF for a fast rebuild."""
    excel_key = _CACHE.get("excel_key") if keep_excel_df else None
    excel_df = _CACHE.get("excel_df") if keep_excel_df else None
    _CACHE["key"] = None
    _CACHE["report"] = None
    _CACHE["df"] = None
    _CACHE["warnings"] = []
    _CACHE["built_at"] = None
    _CACHE["excel_key"] = excel_key
    _CACHE["excel_df"] = excel_df
