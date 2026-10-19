"""Correlate a creative's interest + text_segment with best-performing combos in Excel.

Loads the enriched DataFrame from the morning pipeline and returns:
  - top combos matching the same interest
  - top combos matching the same text_segment
  - best overall CPA / deposits for this interest × text_segment cell
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

_PROJ_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJ_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJ_ROOT))

import pandas as pd

from config.paths import get_excel_path
from src.analysis_agent.analyze_fb_stats import compute_metrics, enrich_structure
from src.analysis_agent.stats_workbook import read_stats_workbook
from src.analysis_agent.morning_report import (
    _safe_float,
    _safe_int,
    _display_value,
)


def _load_df(excel_path: Optional[str] = None) -> Optional[pd.DataFrame]:
    path = (excel_path or "").strip() or get_excel_path()
    if not path or not Path(path).is_file():
        return None
    try:
        df_raw = read_stats_workbook(path)
        df = compute_metrics(df_raw)
        df = enrich_structure(df)
        return df
    except Exception:
        return None


def _combo_dict(r) -> Dict[str, Any]:
    return {
        "country": _display_value(r.get("Country Final")),
        "offer": _display_value(r.get("Offer")),
        "interest": _display_value(r.get("Interest")),
        "text_segment": _display_value(r.get("TextSegment")),
        "device": _display_value(r.get("Device"), "all"),
        "age": _display_value(r.get("Age")),
        "creative": _display_value(r.get("Creative")),
        "deposits": _safe_int(r.get("Deposits") if "Deposits" in r.index else r.get("# FTDs")),
        "leads": _safe_int(r.get("Leads") if "Leads" in r.index else r.get("# Leads")),
        "spend": _safe_float(r.get("Spend") if "Spend" in r.index else r.get("$ Spend")),
        "cpa": _safe_float(r.get("CPA")),
        "cpl": _safe_float(r.get("CPL")),
        "cr": _safe_float(r.get("CR")),
    }


def correlate(
    interest: str,
    text_segment: str,
    country_code: str = "",
    *,
    df: Optional[pd.DataFrame] = None,
    excel_path: Optional[str] = None,
    top_n: int = 5,
) -> Dict[str, Any]:
    """Return stats correlation for interest + text_segment.

    Pass ``df`` (already-loaded enriched DataFrame) to skip Excel re-read.

    Returns:
        {
          "by_interest":    top combos sharing same interest (sorted by deposits),
          "by_text_segment": top combos sharing same text_segment (sorted by deposits),
          "cell":           aggregate for interest × text_segment,
          "best_creative":  single best performing creative name for this interest,
          "excel_found":    bool,
        }
    """
    empty: Dict[str, Any] = {
        "by_interest": [],
        "by_text_segment": [],
        "cell": {},
        "best_creative": "",
        "excel_found": False,
    }

    if df is None:
        df = _load_df(excel_path)
    if df is None or df.empty:
        return empty

    interest_lower = str(interest or "").strip().lower()
    segment_lower = str(text_segment or "").strip().lower()

    # Filter by interest (case-insensitive)
    if interest_lower and "Interest" in df.columns:
        mask_int = df["Interest"].fillna("").astype(str).str.strip().str.lower() == interest_lower
    else:
        mask_int = pd.Series([True] * len(df), index=df.index)

    # Filter by text_segment
    if segment_lower and "TextSegment" in df.columns:
        mask_seg = df["TextSegment"].fillna("").astype(str).str.strip().str.lower() == segment_lower
    else:
        mask_seg = pd.Series([True] * len(df), index=df.index)

    # --- by_interest: aggregate by creative within this interest ---
    df_int = df[mask_int].copy()
    by_interest: List[Dict[str, Any]] = []
    if not df_int.empty:
        agg = (
            df_int.groupby(["Interest", "TextSegment", "Creative", "Offer"], dropna=False)
            .agg(
                Deposits=("# FTDs", "sum"),
                Leads=("# Leads", "sum"),
                Spend=("$ Spend", "sum"),
            )
            .reset_index()
        )
        agg["CPA"] = agg["Spend"] / agg["Deposits"].replace(0, pd.NA)
        agg["CPL"] = agg["Spend"] / agg["Leads"].replace(0, pd.NA)
        agg["CR"] = agg["Deposits"] / agg["Leads"].replace(0, pd.NA) * 100
        top = agg[agg["Deposits"] > 0].sort_values(
            ["Deposits", "Spend"], ascending=[False, False]
        ).head(top_n)
        for _, r in top.iterrows():
            by_interest.append(_combo_dict(r))

    # --- by_text_segment: aggregate by creative within this text_segment ---
    df_seg = df[mask_seg].copy()
    by_text_segment: List[Dict[str, Any]] = []
    if not df_seg.empty:
        agg2 = (
            df_seg.groupby(["Interest", "TextSegment", "Creative", "Offer"], dropna=False)
            .agg(
                Deposits=("# FTDs", "sum"),
                Leads=("# Leads", "sum"),
                Spend=("$ Spend", "sum"),
            )
            .reset_index()
        )
        agg2["CPA"] = agg2["Spend"] / agg2["Deposits"].replace(0, pd.NA)
        agg2["CPL"] = agg2["Spend"] / agg2["Leads"].replace(0, pd.NA)
        agg2["CR"] = agg2["Deposits"] / agg2["Leads"].replace(0, pd.NA) * 100
        top2 = agg2[agg2["Deposits"] > 0].sort_values(
            ["Deposits", "Spend"], ascending=[False, False]
        ).head(top_n)
        for _, r in top2.iterrows():
            by_text_segment.append(_combo_dict(r))

    # --- cell: aggregate for interest × text_segment ---
    df_cell = df[mask_int & mask_seg].copy()
    cell: Dict[str, Any] = {}
    if not df_cell.empty:
        deps = int(df_cell["# FTDs"].sum())
        leads = int(df_cell["# Leads"].sum())
        spend = float(df_cell["$ Spend"].sum())
        cell = {
            "deposits": deps,
            "leads": leads,
            "spend": spend,
            "cpa": spend / deps if deps else None,
            "cpl": spend / leads if leads else None,
        }

    # --- best_creative: top creative name by deposits for this interest ---
    best_creative = ""
    if by_interest:
        best_creative = str(by_interest[0].get("creative") or "")

    return {
        "by_interest": by_interest,
        "by_text_segment": by_text_segment,
        "cell": cell,
        "best_creative": best_creative,
        "excel_found": True,
    }
