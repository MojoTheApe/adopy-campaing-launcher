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
            filtered = _aggregate_cm_drill_rows(filtered)
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

    if section == "cm_comparison":
        return [_cm_row_to_dict(r) for _, r in filtered.iterrows()]
    return [_row_to_dict(r) for _, r in filtered.iterrows()]


def _cm_row_to_dict(row: pd.Series) -> Dict[str, Any]:
    """Convert an aggregated CM drill row (one adset) to a display dict."""
    ftds = _to_native(row.get("# FTDs")) or 0
    leads = _to_native(row.get("# Leads")) or 0
    spend = _to_native(row.get("$ Spend")) or 0.0
    cpa = _to_native(row.get("CPA"))
    cpl = (spend / leads) if leads > 0 else None
    return {
        "account": _to_native(row.get("Account Alias")),
        "adset": _to_native(row.get("Adset Name")),
        "ad_asset": _to_native(row.get("Ad Asset")),
        "ftds": ftds,
        "leads": leads,
        "spend": spend,
        "cpa": cpa,
        "cpl": cpl,
        "cm": _to_native(row.get("CM")),
        "date": _to_native(row.get("Date")),
        "ad_title": None,
        # [{"name": "vid_es_lp", "ftds": 4}, ...] sorted by ftds desc;
        # untagged rows (pre-2026-07-30 data) surface as name "—".
        # NB: .map() leaves NaN for adsets without breakdown — isinstance
        # guards against that (NaN is truthy, `or []` wouldn't catch it).
        "variants": row["_variants"] if isinstance(row.get("_variants"), list) else [],
    }


def _aggregate_cm_drill_rows(filtered: pd.DataFrame) -> pd.DataFrame:
    """One drill row per Adset Name for a CM; only adsets with FTDs > 0, sorted by FTDs desc."""
    if filtered.empty:
        return filtered

    grouped = (
        filtered.groupby("Adset Name", dropna=False)
        .agg(
            **{
                "# FTDs": ("# FTDs", "sum"),
                "# Leads": ("# Leads", "sum"),
                "$ Spend": ("$ Spend", "sum"),
                "Account Alias": ("Account Alias", "first"),
                "CM": ("CM", "first"),
                "Ad Asset": ("Ad Asset", "first"),
                "Date": ("Date", "max"),
            }
        )
        .reset_index()
    )
    # Keep only adsets with at least 1 deposit
    grouped = grouped[grouped["# FTDs"] > 0].copy()
    grouped["CPA"] = grouped["$ Spend"] / grouped["# FTDs"].replace(0, pd.NA)
    grouped = grouped.sort_values("# FTDs", ascending=False)

    # Landing-page (Variant) deposit breakdown per adset, for the chips in
    # the CM drill: [{"name": ..., "ftds": n}, ...] sorted by ftds desc.
    if "Variant" in filtered.columns:
        vg = (
            filtered.groupby(["Adset Name", "Variant"], dropna=False)["# FTDs"]
            .sum()
            .reset_index()
        )
        vg = vg[vg["# FTDs"] > 0]
        by_adset: Dict[Any, List[Dict[str, Any]]] = {}
        for _, r in vg.iterrows():
            name = str(r["Variant"] or "").strip() or "—"
            by_adset.setdefault(r["Adset Name"], []).append(
                {"name": name, "ftds": int(r["# FTDs"])}
            )
        for chips in by_adset.values():
            chips.sort(key=lambda c: -c["ftds"])
        grouped["_variants"] = grouped["Adset Name"].map(by_adset)
    return grouped


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
