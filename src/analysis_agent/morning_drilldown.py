"""Drill-down lookup: aggregate row -> underlying ad-level rows.

Single dispatch entry point used by the web layer's `/details` endpoint.
Filters route through `section_filters` so the drilled rows always match
what the aggregator displays.
"""
from __future__ import annotations

from typing import Any, Dict, List

import pandas as pd

from src.analysis_agent.section_filters import (
    filter_cm_rows,
    filter_combo_rows,
    filter_creative_rows,
    filter_optimization_rows,
    filter_targeting_rows,
    filter_trend_rows,
)


# Fixed column set returned for any drill (matches the design's contract).
DRILL_COLUMNS: List[str] = [
    "account",
    "ad_title",
    "ad_asset",
    "adset",
    "spend",
    "leads",
    "ftds",
    "cpl",
    "cpa",
    "cm",
    "date",
]


def _to_native(value: Any) -> Any:
    """Coerce pandas / numpy scalars to JSON-friendly Python types."""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, pd.Timestamp):
        return value.strftime("%Y-%m-%d")
    if hasattr(value, "item"):  # numpy scalar
        return value.item()
    return value


def _row_to_dict(row: pd.Series) -> Dict[str, Any]:
    spend = _to_native(row.get("$ Spend"))
    leads = _to_native(row.get("# Leads"))
    ftds = _to_native(row.get("# FTDs"))
    cpl = None
    cpa = None
    if isinstance(spend, (int, float)) and isinstance(leads, (int, float)) and leads > 0:
        cpl = spend / leads
    if isinstance(spend, (int, float)) and isinstance(ftds, (int, float)) and ftds > 0:
        cpa = spend / ftds
    return {
        "account": _to_native(row.get("Account Alias")),
        "ad_title": _to_native(row.get("Ad Title")),
        "ad_asset": _to_native(row.get("Ad Asset")),
        "adset": _to_native(row.get("Adset Name")),
        "spend": spend,
        "leads": leads,
        "ftds": ftds,
        "cpl": cpl,
        "cpa": cpa,
        "cm": _to_native(row.get("CM")),
        "date": _to_native(row.get("Date")),
    }


def get_underlying_rows(
    df: pd.DataFrame,
    *,
    section: str,
    country: str = "",
    **kwargs: Any,
) -> List[Dict[str, Any]]:
    """Return the ad-level rows that compose the aggregate identified by
    (section, country, **kwargs). Unknown section / missing required kwarg /
    no match all return [].
    """
    try:
        if section == "cm_comparison":
            filtered = filter_cm_rows(df, country, kwargs["cm"])
        elif section == "trend":
            filtered = filter_trend_rows(
                df, country,
                offer=kwargs["offer"],
                device=kwargs["device"],
                interest=kwargs["interest"],
                creative=kwargs["creative"],
            )
        elif section == "top_combos":
            filtered = filter_combo_rows(
                df, country,
                offer=kwargs["offer"],
                device=kwargs["device"],
                interest=kwargs["interest"],
                age=kwargs["age"],
                creative=kwargs["creative"],
                text_segment=kwargs["text_segment"],
            )
        elif section == "targeting":
            filtered = filter_targeting_rows(df, country, kwargs["bucket"])
        elif section == "creatives":
            filtered = filter_creative_rows(df, country, kwargs["creative"])
        elif section == "optimization":
            filtered = filter_optimization_rows(
                df, country, kwargs["account"], kwargs["adset"],
            )
            filtered = _aggregate_optimization_drill_rows(filtered)
        else:
            return []
    except KeyError:
        return []

    return [_row_to_dict(r) for _, r in filtered.iterrows()]


def _aggregate_optimization_drill_rows(filtered: pd.DataFrame) -> pd.DataFrame:
    """One drill row per (Ad Title, Ad Asset); sum metrics across duplicate Excel lines."""
    if filtered.empty:
        return filtered
    grouped = (
        filtered.groupby(["Ad Title", "Ad Asset"], dropna=False)
        .agg(
            **{
                "# Leads": ("# Leads", "sum"),
                "# FTDs": ("# FTDs", "sum"),
                "$ Spend": ("$ Spend", "sum"),
                "Account Alias": ("Account Alias", "first"),
                "Adset Name": ("Adset Name", "first"),
                "CM": ("CM", "first"),
                "Date": ("Date", "max"),
            }
        )
        .reset_index()
    )
    return grouped.sort_values("$ Spend", ascending=False)
