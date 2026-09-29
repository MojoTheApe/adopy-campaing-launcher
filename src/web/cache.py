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

Threads (ACL-1380): the report is built in worker threads (routes use
`asyncio.to_thread`, the startup warm-up has its own thread). One build runs
at a time (`_BUILD_LOCK`); a second caller waits for it and takes its result
instead of reading the workbook again. The cache contents are swapped under
a short `_STATE_LOCK`, so a reader never sees a report from one build next to
the DataFrame of another.
"""
from __future__ import annotations

import os
import threading
from itertools import count
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from src.analysis_agent.morning_service import (
    _resolve_excel_path,
    load_morning_report,
)


# Reports are cached PER campaign manager: the enriched DataFrame is the
# expensive part and is the same for everyone, while the report assembled on
# top of it is per-CM. Keying only by the Excel file meant switching buyer
# would have thrown away the parsed workbook and re-read ~71k rows each time.
_REVISIONS = count(1)
_CACHE: Dict[str, Any] = {
    "key": None,            # (excel_path, mtime) for full report — None means empty
    "reports": {},          # cm -> report dict
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


# Одна сборка за раз: холодная сборка — десятки секунд, и вторая такая же
# рядом (прогрев + открытая вкладка) только удвоила бы ожидание обеих.
_BUILD_LOCK = threading.Lock()
# Короткий замок на само содержимое _CACHE: чтение тройки и её замена.
_STATE_LOCK = threading.Lock()


def _lookup(expected_key, cm_key: str):
    with _STATE_LOCK:
        if (
            _CACHE["key"] is not None
            and _CACHE["key"] == expected_key
            and cm_key in _CACHE["reports"]
        ):
            return _CACHE["reports"][cm_key], _CACHE["df"], _CACHE["warnings"]
    return None


def is_warm(*, my_cm: Optional[str] = None) -> bool:
    """Отчёт для текущей книги и менеджера уже собран — открытие будет быстрым."""
    excel_path = _resolve_excel_path(None)
    if my_cm is None:
        from src.web.buyer import get_selected_cm

        my_cm = get_selected_cm()
    return _lookup(_current_key(excel_path), (my_cm or "").strip()) is not None


def get_cached_report(
    *, force_rebuild: bool = False, my_cm: Optional[str] = None
) -> Tuple[Dict[str, Any], pd.DataFrame, List[str]]:
    """Return the latest (report, df, warnings).

    Rebuilds when the Excel file's mtime differs from the cached entry, or
    when force_rebuild is set. On force_rebuild, reuses the enriched DataFrame
    if Excel mtime is unchanged (only GSheet + report assembly re-run).

    Thread-safe: concurrent callers share one build (see module docstring).
    """
    excel_path = _resolve_excel_path(None)

    if my_cm is None:
        from src.web.buyer import get_selected_cm

        my_cm = get_selected_cm()
    cm_key = (my_cm or "").strip()

    if not force_rebuild:
        hit = _lookup(_current_key(excel_path), cm_key)
        if hit is not None:
            return hit

    with _BUILD_LOCK:
        # Пока ждали замок, этот же отчёт мог собрать другой поток — тогда
        # берём его. Ключ перечитываем: книга могла смениться за ожидание.
        expected_key = _current_key(excel_path)
        if not force_rebuild:
            hit = _lookup(expected_key, cm_key)
            if hit is not None:
                return hit

        reused_df = None
        with _STATE_LOCK:
            if (
                expected_key is not None
                and _CACHE.get("excel_key") == expected_key
                and _CACHE.get("excel_df") is not None
            ):
                reused_df = _CACHE["excel_df"]

        report, df, warnings = load_morning_report(enriched_df=reused_df, my_cm=cm_key or None)
        # Refresh can change values or account rows without changing the DF length.
        # Derived period reports must belong to this exact rebuild.
        report["_cache_revision"] = next(_REVISIONS)

        from datetime import datetime

        with _STATE_LOCK:
            # A new workbook invalidates every buyer's report, not just this one.
            if _CACHE["key"] != expected_key:
                _CACHE["reports"] = {}
            _CACHE["key"] = expected_key
            _CACHE["reports"][cm_key] = report
            _CACHE["df"] = df
            _CACHE["warnings"] = warnings
            _CACHE["excel_key"] = expected_key
            _CACHE["excel_df"] = df
            _CACHE["built_at"] = datetime.now().isoformat(timespec="seconds")
        return report, df, warnings


def cache_built_at() -> Optional[str]:
    return _CACHE["built_at"]


def cache_clear(*, keep_excel_df: bool = False) -> None:
    """Evict report cache. Optionally keep enriched Excel DF for a fast rebuild."""
    with _STATE_LOCK:
        excel_key = _CACHE.get("excel_key") if keep_excel_df else None
        excel_df = _CACHE.get("excel_df") if keep_excel_df else None
        _CACHE["key"] = None
        _CACHE["reports"] = {}
        _CACHE["df"] = None
        _CACHE["warnings"] = []
        _CACHE["built_at"] = None
        _CACHE["excel_key"] = excel_key
        _CACHE["excel_df"] = excel_df
