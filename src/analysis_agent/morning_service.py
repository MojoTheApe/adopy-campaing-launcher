"""Shared orchestration for the morning report.

Both the CLI (`run_morning.py`) and the web app (`src/web/app.py`) call
`load_morning_report` so env handling, GSheet fetching, error reporting,
and the call into `build_morning_report` live in one place.

Returns the report dict, the enriched DataFrame (needed by the web app's
drilldown endpoint), and a list of human-readable warnings (e.g. GSheet
unavailable). No exceptions raised for "soft" failures like a missing
GSheet credential — the caller decides how to surface warnings.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

_PROJ_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJ_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJ_ROOT))

from config.paths import get_excel_path
from src.analysis_agent.analyze_fb_stats import (
    compute_metrics,
    enrich_structure,
)
from src.analysis_agent.gsheet_accounts import fetch_account_rows
from src.analysis_agent.morning_report import build_morning_report


def _resolve_excel_path(excel_path: Optional[str]) -> str:
    """env var / arg / config default precedence — same as the CLI uses."""
    return (excel_path or "").strip() or get_excel_path()


def _resolve_my_cm(my_cm: Optional[str]) -> str:
    return (my_cm or "").strip() or os.environ.get("MY_CM_NAME", "Evgeni Sedin").strip()


def _load_enriched_df(excel_path: str) -> pd.DataFrame:
    """Read Excel + run the same enrichment pipeline.

    The web layer's drilldown filters need the enriched DataFrame
    (Country Final, Offer, Device, Interest, Age, Creative, TextSegment
    columns), not the raw Excel rows.
    """
    df_raw = pd.read_excel(excel_path)
    required_cols = [
        "Country", "Account Alias", "Adset Name", "Ad Title",
        "# Leads", "# FTDs", "$ Spend", "CM", "Ad Asset", "$ Net",
    ]
    missing = [c for c in required_cols if c not in df_raw.columns]
    if missing:
        raise ValueError(f"Missing columns in Excel: {missing}")
    df = compute_metrics(df_raw)
    df = enrich_structure(df)
    return df


def load_morning_report(
    *,
    excel_path: Optional[str] = None,
    my_cm: Optional[str] = None,
    trend_days: int = 7,
    country_filter: Optional[str] = None,
    skip_gsheet: bool = False,
) -> Tuple[Dict[str, Any], pd.DataFrame, List[str]]:
    """Build the full morning report from current Excel + GSheet state.

    Excel is parsed + enriched ONCE. The enriched DataFrame is returned
    alongside the report dict so the caller (web app drilldown) doesn't
    re-parse on every drill request.

    Returns:
        (report_dict, enriched_df, warnings)

    Raises:
        FileNotFoundError: if the Excel file is missing.
        ValueError: if the Excel is missing required columns.
    """
    warnings: List[str] = []

    resolved_excel = _resolve_excel_path(excel_path)
    if not os.path.isfile(resolved_excel):
        raise FileNotFoundError(
            f"Excel file not found: {resolved_excel}. "
            f"Set FB_STATS_EXCEL or pass excel_path."
        )

    resolved_cm = _resolve_my_cm(my_cm)

    account_rows: List[Dict[str, Any]] = []
    if skip_gsheet:
        warnings.append("GSheet skipped (skip_gsheet=True)")
    else:
        try:
            account_rows = fetch_account_rows(my_cm=resolved_cm)
        except Exception as e:  # noqa: BLE001 - graceful degradation
            warnings.append(
                f"GSheet unavailable: {type(e).__name__}: {e}. "
                f"Sections 6-9 will be empty or limited."
            )

    enriched_df = _load_enriched_df(resolved_excel)

    report = build_morning_report(
        df=enriched_df,
        account_rows=account_rows,
        my_cm=resolved_cm,
        trend_days=trend_days,
        country_filter=country_filter,
    )
    # Provide the source Excel path on the report for the renderer header.
    report["source_excel"] = resolved_excel
    return report, enriched_df, warnings
