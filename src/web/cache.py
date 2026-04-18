"""mtime-keyed in-process cache for the morning report.

Single-user, single-uvicorn-worker tool. The cache holds one entry — the
last successfully built (report_dict, enriched_df) — so:

  - GET /  : if Excel file mtime hasn't changed since last build, return
             the cached pair instantly. Otherwise rebuild.
  - POST /refresh : force-evict and rebuild on next /.
  - GET /details : always uses the cached enriched_df. If the cache is
             empty (e.g. the user hit /details before /), trigger a build.

Known race (per design 7C, accepted): if /refresh swaps the cache while a
GET / render is mid-flight, the in-flight render may see partially-stale
state. Single-user, sub-second renders, low blast radius. If/when this
tool gets shared, wrap _CACHE access in an asyncio.Lock.
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
    "key": None,            # (excel_path, mtime) tuple — None means empty
    "report": None,         # dict
    "df": None,             # pd.DataFrame
    "warnings": [],         # list[str]
    "built_at": None,       # ISO datetime string
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
    when force_rebuild is set. Re-raises FileNotFoundError / ValueError
    from morning_service if the build fails — the caller (route handler)
    decides how to render the error.
    """
    excel_path = _resolve_excel_path(None)
    expected_key = _current_key(excel_path)

    if not force_rebuild and _CACHE["key"] is not None and _CACHE["key"] == expected_key:
        return _CACHE["report"], _CACHE["df"], _CACHE["warnings"]

    report, df, warnings = load_morning_report()

    _CACHE["key"] = expected_key
    _CACHE["report"] = report
    _CACHE["df"] = df
    _CACHE["warnings"] = warnings
    from datetime import datetime
    _CACHE["built_at"] = datetime.now().isoformat(timespec="seconds")
    return report, df, warnings


def cache_built_at() -> Optional[str]:
    return _CACHE["built_at"]


def cache_clear() -> None:
    _CACHE["key"] = None
    _CACHE["report"] = None
    _CACHE["df"] = None
    _CACHE["warnings"] = []
    _CACHE["built_at"] = None
