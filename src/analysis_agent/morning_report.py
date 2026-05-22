"""Build the MorningReport data model — payloads per country for `morning_markdown`.

Calls existing analysis pipeline (compute_metrics / enrich_structure / aggregate_combos)
and aggregates trend, targeting breakdown, creatives (by Ad Title / §4 in output),
top combos, account statuses, close recs, and open recs.

The result is a plain dict — no external rendering dependencies.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

_PROJ_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJ_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJ_ROOT))

from src.analysis_agent.analyze_fb_stats import (
    aggregate_combos,
    compute_metrics,
    enrich_structure,
)
from src.analysis_agent.gsheet_accounts import (
    format_account_status_todo_cell,
    is_omitted_from_account_statuses_table,
)
from src.analysis_agent.section_filters import NA_SENTINEL
from src.knowledge_agent.knowledge_base import format_target_context_line, get_all_targets
from config.benchmarks import compute_country_medians, get_benchmark


def _key_value(v: Any) -> str:
    """Coerce a row dimension value into the aggregate_key URL form.

    Empty / NaN / 'nan' string become the NA_SENTINEL so drills can
    round-trip back to the same group that pandas put NaN values in.
    """
    if v is None:
        return NA_SENTINEL
    try:
        if pd.isna(v):
            return NA_SENTINEL
    except (TypeError, ValueError):
        pass
    s = str(v).strip()
    if not s or s.lower() in ("nan", "none"):
        return NA_SENTINEL
    return s


def _display_value(v: Any, default: str = "") -> str:
    """Coerce row dimension values for human-readable report labels."""
    if v is None:
        return default
    try:
        if pd.isna(v):
            return default
    except (TypeError, ValueError):
        pass
    s = str(v).strip()
    if not s or s.lower() in ("nan", "none"):
        return default
    return s


def _combo_label(*parts: Any) -> str:
    return " + ".join(p for p in (_display_value(part) for part in parts) if p)


# ── helpers ────────────────────────────────────────────────────────────────────

def _safe_float(v) -> float:
    try:
        f = float(v)
        return 0.0 if pd.isna(f) else f
    except (TypeError, ValueError):
        return 0.0


def _safe_int(v) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _fmt(v, fmt: str = ".2f") -> str:
    f = _safe_float(v)
    return format(f, fmt)


# Excel ``Country Final`` (lower case) → canonical slug for matching GSheet col B.
_EXCEL_COUNTRY_TO_GSHEET_CODE: Dict[str, str] = {
    "mexico": "mx",
    "guatemala": "gt",
    "brazil": "br",
    "chile": "cl",
    "colombia": "co",
    "peru": "pe",
    "argentina": "ar",
    "ecuador": "ec",
    "venezuela": "ve",
    "bolivia": "bo",
    "paraguay": "py",
    "uruguay": "uy",
    "bahrain": "bh",
    "uae": "uae",
    "united arab emirates": "uae",
    "qatar": "qa",
    "oman": "om",
    "kuwait": "kw",
    "saudi arabia": "sa",
    "nigeria": "ng",
    "kenya": "ke",
    "ghana": "gh",
    "south africa": "za",
    "albania": "al",
    "morocco": "ma",
    "egypt": "eg",
}

# Col B sometimes uses ISO alpha-2 instead of our slug (e.g. AE vs UAE).
_GSHEET_COUNTRY_ISO2_ALIASES: Dict[str, str] = {
    "ae": "uae",
}


def canonical_gsheet_country_code(raw: str) -> str:
    """Normalize GSheet column B so it matches ``_EXCEL_COUNTRY_TO_GSHEET_CODE`` values."""
    k = str(raw or "").strip().lower()
    if not k:
        return ""
    if k in _GSHEET_COUNTRY_ISO2_ALIASES:
        return _GSHEET_COUNTRY_ISO2_ALIASES[k]
    if k in _EXCEL_COUNTRY_TO_GSHEET_CODE:
        return _EXCEL_COUNTRY_TO_GSHEET_CODE[k]
    return k


# ── Section 1: CM comparison ───────────────────────────────────────────────────

def build_cm_comparison(df: pd.DataFrame, country: str, my_cm: str) -> List[Dict[str, Any]]:
    """Aggregate per CM for this country. Returns list of row dicts, my_cm first."""
    df_c = df[df["Country Final"] == country]
    if df_c.empty:
        return []

    grouped = (
        df_c.groupby("CM", dropna=False)
        .agg(
            Deposits=("# FTDs", "sum"),
            Leads=("# Leads", "sum"),
            Spend=("$ Spend", "sum"),
            Net=("$ Net", "sum"),
        )
        .reset_index()
    )
    grouped["CPA"] = grouped["Spend"] / grouped["Deposits"].replace(0, pd.NA)
    grouped["ROI"] = grouped["Net"] / grouped["Spend"].replace(0, pd.NA)
    grouped = grouped.sort_values(by=["Net", "Deposits"], ascending=[False, False])

    rows = []
    for _, r in grouped.iterrows():
        cm_name = str(r.get("CM") or "")
        rows.append(
            {
                "cm": cm_name,
                "is_me": cm_name.strip().lower() == my_cm.strip().lower(),
                "deposits": _safe_int(r.get("Deposits")),
                "leads": _safe_int(r.get("Leads")),
                "spend": _safe_float(r.get("Spend")),
                "net": _safe_float(r.get("Net")),
                "cpa": _safe_float(r.get("CPA")),
                "roi": _safe_float(r.get("ROI")),
                "aggregate_key": {
                    "section": "cm_comparison",
                    "country": country,
                    "cm": _key_value(r.get("CM")),
                },
            }
        )
    # Put my row first
    rows.sort(key=lambda x: (not x["is_me"], -x["net"]))
    return rows


# ── Section 1b: Asset (offer) summary ──────────────────────────────────────────

def build_asset_summary(df: pd.DataFrame, country: str) -> List[Dict[str, Any]]:
    """Short aggregate by Ad Asset (offer) for this country."""
    df_c = df[df["Country Final"] == country]
    if df_c.empty or "Offer" not in df_c.columns:
        return []

    grouped = (
        df_c.groupby("Offer", dropna=False)
        .agg(
            Deposits=("# FTDs", "sum"),
            Leads=("# Leads", "sum"),
            Spend=("$ Spend", "sum"),
            Net=("$ Net", "sum"),
        )
        .reset_index()
    )
    grouped["CPL"] = grouped["Spend"] / grouped["Leads"].replace(0, pd.NA)
    grouped["CPA"] = grouped["Spend"] / grouped["Deposits"].replace(0, pd.NA)
    grouped["ROI"] = grouped["Net"] / grouped["Spend"].replace(0, pd.NA)
    grouped = grouped.sort_values(["Deposits", "Spend"], ascending=[False, False])

    rows = []
    for _, r in grouped.iterrows():
        offer = _display_offer(r.get("Offer"), country)
        if not offer:
            continue
        rows.append({
            "offer": offer,
            "deposits": _safe_int(r.get("Deposits")),
            "leads": _safe_int(r.get("Leads")),
            "spend": _safe_float(r.get("Spend")),
            "cpl": _safe_float(r.get("CPL")),
            "cpa": _safe_float(r.get("CPA")),
            "roi": _safe_float(r.get("ROI")),
        })
    return rows


# ── Section 2: Recent trend ────────────────────────────────────────────────────

def build_trend(
    df: pd.DataFrame, country: str, trend_days: int = 7
) -> Dict[str, Any]:
    """Compare last `trend_days` days vs the whole period for this country.

    Returns:
        {
          "has_date": bool,
          "period_label": str,
          "trending_up": [...],   # gained deposits recently
          "trending_down": [...], # lost deposits recently
          "stable": [...],        # consistently depositing
        }
    """
    result: Dict[str, Any] = {
        "has_date": False,
        "period_label": "",
        "trending_up": [],
        "trending_down": [],
        "stable": [],
    }

    df_c = df[df["Country Final"] == country].copy()
    if df_c.empty or "Date" not in df_c.columns:
        return result

    df_c["Date"] = pd.to_datetime(df_c["Date"], errors="coerce")
    df_c = df_c.dropna(subset=["Date"])
    if df_c.empty:
        return result

    result["has_date"] = True
    max_date = df_c["Date"].max()
    cutoff = max_date - timedelta(days=trend_days - 1)
    result["period_label"] = (
        f"{cutoff.strftime('%b %d')} – {max_date.strftime('%b %d, %Y')} ({trend_days}d)"
    )

    combo_cols = ["Offer", "Device", "Interest", "Creative"]

    def _agg(sub: pd.DataFrame) -> pd.DataFrame:
        return (
            sub.groupby(combo_cols, dropna=False)
            .agg(Deposits=("# FTDs", "sum"), Spend=("$ Spend", "sum"))
            .reset_index()
        )

    recent = _agg(df_c[df_c["Date"] >= cutoff])
    overall = _agg(df_c)

    merged = overall.merge(
        recent.rename(columns={"Deposits": "RecentDep", "Spend": "RecentSpend"}),
        on=combo_cols,
        how="left",
    )
    merged["RecentDep"] = merged["RecentDep"].fillna(0).astype(int)

    def _row_to_dict(r) -> Dict[str, Any]:
        full_creative = _display_value(r.get("Creative"))
        return {
            "offer": _display_offer(r.get("Offer"), country),
            "device": _display_value(r.get("Device"), "all"),
            "interest": _display_value(r.get("Interest")),
            "creative": full_creative[:40],  # display: truncated to 40 chars
            "deposits_total": _safe_int(r.get("Deposits")),
            "deposits_recent": _safe_int(r.get("RecentDep")),
            "aggregate_key": {
                "section": "trend",
                "country": country,
                "offer": _key_value(r.get("Offer")),
                "device": _key_value(r.get("Device")),
                "interest": _key_value(r.get("Interest")),
                "creative": _key_value(full_creative),  # FULL creative for drill match
            },
        }

    for _, r in merged.iterrows():
        total = _safe_int(r.get("Deposits"))
        recent_dep = _safe_int(r.get("RecentDep"))
        if total == 0:
            continue

        entry = _row_to_dict(r)
        if recent_dep > 0 and recent_dep == total:
            result["trending_up"].append(entry)       # ALL deposits from recent window
        elif recent_dep > 0:
            result["stable"].append(entry)             # deposits in both periods
        else:
            if total >= 1:
                result["trending_down"].append(entry)  # had deposits, none recently

    # Sort each list by deposits desc
    for key in ("trending_up", "stable", "trending_down"):
        result[key].sort(key=lambda x: -x["deposits_total"])

    return result


def _creative_matches_trend_row(truncated: str, creative_full: str) -> bool:
    """Trend rows store Creative truncated (~40 chars); match safely to full name."""
    a = str(truncated or "").strip()
    b = str(creative_full or "").strip()
    if not a or not b:
        return False
    return b.startswith(a) or a.startswith(b[:40]) or a in b


def _trend_entry_matches_combo(
    entry: Dict[str, Any],
    offer: str,
    interest: str,
    creative: str,
) -> bool:
    if str(entry.get("offer") or "").strip() != str(offer or "").strip():
        return False
    if str(entry.get("interest") or "").strip() != str(interest or "").strip():
        return False
    return _creative_matches_trend_row(str(entry.get("creative") or ""), creative)


def _interest_trend_boost(trend: Optional[Dict[str, Any]], interest: str) -> int:
    """Sort boost so §10 prefers interests that are strong in §2 (recent window)."""
    if not trend or not trend.get("has_date"):
        return 0
    inter = str(interest or "").strip()
    b = 0
    for e in trend.get("stable", []):
        if str(e.get("interest") or "").strip() == inter:
            b += 50 + int(e.get("deposits_recent", 0) or 0) * 5
    for e in trend.get("trending_up", []):
        if str(e.get("interest") or "").strip() == inter:
            b += 80 + int(e.get("deposits_recent", 0) or 0) * 8
    for e in trend.get("trending_down", []):
        if str(e.get("interest") or "").strip() == inter:
            b -= 40
    return b


def _interest_trend_tags(trend: Optional[Dict[str, Any]], interest: str) -> List[str]:
    """Short labels for §10 header, mirroring §2 buckets for this interest."""
    if not trend or not trend.get("has_date"):
        return []
    inter = str(interest or "").strip()
    tags: List[str] = []
    if any(str(e.get("interest") or "").strip() == inter for e in trend.get("stable", [])):
        tags.append("stable")
    if any(str(e.get("interest") or "").strip() == inter for e in trend.get("trending_up", [])):
        tags.append("heating up")
    if any(str(e.get("interest") or "").strip() == inter for e in trend.get("trending_down", [])):
        tags.append("stalled")
    return tags


def _creative_trend_note(
    trend: Optional[Dict[str, Any]],
    offer: str,
    interest: str,
    creative: str,
) -> str:
    """Append to creative line when this combo appears in §2 trend lists."""
    if not trend or not trend.get("has_date"):
        return ""
    for label, key in (
        ("stable + recent", "stable"),
        ("all recent dep", "trending_up"),
        ("stalled (recovery)", "trending_down"),
    ):
        for e in trend.get(key, []):
            if _trend_entry_matches_combo(e, offer, interest, creative):
                return f"trend: {label}"
    return ""


# ── Section 3: Top combos (from aggregate_combos output) ──────────────────────

def _check_quality(combo: Dict[str, Any], bench: Dict[str, Any]) -> str:
    """Return 'ok' if combo meets all benchmarks, 'warn' otherwise."""
    cpl = combo.get("cpl", 0.0)
    cpa = combo.get("cpa", 0.0)
    cr = combo.get("cr", 0.0)

    max_cpl = bench.get("max_cpl")
    max_cpa = bench.get("max_cpa")
    min_cr = bench.get("min_cr")

    if max_cpl is not None and cpl > 0 and cpl > max_cpl:
        return "warn"
    if max_cpa is not None and cpa > 0 and cpa > max_cpa:
        return "warn"
    if min_cr is not None and cr < min_cr:
        return "warn"
    return "ok"


def build_top_combos(combos: pd.DataFrame, country: str, top_n: int = 5) -> List[Dict[str, Any]]:
    """Return top N combos for country (Strong first, then Interesting)."""
    df_c = combos[combos["Country Final"] == country].copy()
    if df_c.empty:
        return []

    strong = df_c[df_c["Category"] == "Strong"].sort_values(
        ["Deposits", "CR", "ROI"], ascending=[False, False, False]
    )
    interesting = df_c[df_c["Category"] == "Interesting"].sort_values(
        ["CR", "ROI"], ascending=[False, False]
    )
    top = pd.concat([strong, interesting]).head(top_n)

    result = []
    for _, r in top.iterrows():
        offer_str = _display_offer(r.get("Offer"), country)
        bench = get_benchmark(country, offer_str)
        combo = {
            "offer": offer_str,
            "device": _display_value(r.get("Device"), "all"),
            "interest": _display_value(r.get("Interest")),
            "age": _display_value(r.get("Age")),
            "creative": _display_value(r.get("Creative")),
            "category": str(r.get("Category", "Strong")),
            "deposits": _safe_int(r.get("Deposits")),
            "leads": _safe_int(r.get("Leads")),
            "spend": _safe_float(r.get("Spend")),
            "cpl": _safe_float(r.get("CPL")),
            "cpa": _safe_float(r.get("CPA")),
            "cr": _safe_float(r.get("CR")),
            "roi": _safe_float(r.get("ROI")),
            "top_account": _display_value(r.get("TopAccount")),
            "aggregate_key": {
                "section": "top_combos",
                "country": country,
                "offer": _key_value(r.get("Offer")),
                "device": _key_value(r.get("Device")),
                "interest": _key_value(r.get("Interest")),
                "age": _key_value(r.get("Age")),
                "creative": _key_value(r.get("Creative")),
                "text_segment": _key_value(r.get("TextSegment")),
            },
        }
        combo["quality"] = _check_quality(combo, bench)
        result.append(combo)
    return result


# ── Section 4: Targeting breakdown ────────────────────────────────────────────

def build_targeting_breakdown(df: pd.DataFrame, country: str) -> List[Dict[str, Any]]:
    """Aggregate by Interest for this country."""
    df_c = df[df["Country Final"] == country].copy()
    if df_c.empty or "Interest" not in df_c.columns:
        return []

    grouped = (
        df_c.groupby("Interest", dropna=False)
        .agg(
            Deposits=("# FTDs", "sum"),
            Leads=("# Leads", "sum"),
            Spend=("$ Spend", "sum"),
        )
        .reset_index()
    )
    grouped["CPA"] = grouped["Spend"] / grouped["Deposits"].replace(0, pd.NA)
    grouped = grouped.sort_values(["Deposits", "Spend"], ascending=[False, False])

    rows = []
    for _, r in grouped.iterrows():
        interest_raw = r.get("Interest")
        interest = str(interest_raw or "").strip()
        if not interest or interest.lower() == "nan":
            interest = "(unknown)"
        rows.append(
            {
                "interest": interest,
                "deposits": _safe_int(r.get("Deposits")),
                "leads": _safe_int(r.get("Leads")),
                "spend": _safe_float(r.get("Spend")),
                "cpa": _safe_float(r.get("CPA")),
                "aggregate_key": {
                    "section": "targeting",
                    "country": country,
                    "bucket": _key_value(interest_raw),
                },
            }
        )
    return rows


# ── Section 5: Creative breakdown ─────────────────────────────────────────────

def build_creative_breakdown(
    df: pd.DataFrame, country: str, top_n: int = 20
) -> List[Dict[str, Any]]:
    """Aggregate by Ad Title for this country. Returns top_n by deposits then spend."""
    df_c = df[df["Country Final"] == country].copy()
    if df_c.empty or "Ad Title" not in df_c.columns:
        return []

    grouped = (
        df_c.groupby("Ad Title", dropna=False)
        .agg(
            Deposits=("# FTDs", "sum"),
            Leads=("# Leads", "sum"),
            Spend=("$ Spend", "sum"),
        )
        .reset_index()
    )
    grouped["CPA"] = grouped["Spend"] / grouped["Deposits"].replace(0, pd.NA)
    grouped = grouped.sort_values(["Deposits", "Spend"], ascending=[False, False]).head(top_n)

    rows = []
    for _, r in grouped.iterrows():
        title_raw = r.get("Ad Title")
        title = str(title_raw or "").strip()
        if not title or title.lower() == "nan":
            title = "(no title)"
        rows.append(
            {
                "ad_title": title,
                "deposits": _safe_int(r.get("Deposits")),
                "leads": _safe_int(r.get("Leads")),
                "spend": _safe_float(r.get("Spend")),
                "cpa": _safe_float(r.get("CPA")),
                "aggregate_key": {
                    "section": "creatives",
                    "country": country,
                    "creative": _key_value(title_raw),
                },
            }
        )
    return rows


# ── Sections 6 & 7: Context blocks for AI ─────────────────────────────────────

def build_ai_context_block(
    country: str, top_combos: List[Dict[str, Any]]
) -> str:
    """Return a pre-formatted paste block for 'explain why these combos work' (section 6)."""
    if not top_combos:
        return "_No qualifying combos — nothing to explain._"

    lines = [
        f"Country: {country}",
        "",
        "Top combos (paste into Cursor chat and ask: 'Explain why these combos work and what makes them strong'):",
        "",
    ]
    for i, c in enumerate(top_combos[:3], 1):
        lines.append(
            f"{i}. [{c['category']}] {_combo_label(c['device'], c['interest'], c['age'], c['creative'])}"
            f"  |  Offer: {c['offer']}  |  Dep: {c['deposits']}  |  ROI: {_fmt(c['roi'])}"
        )
        # Pull target context from KB
        ctx = format_target_context_line(c["interest"], language="en") if c["interest"] else None
        if ctx:
            lines.append(f"   → {ctx}")

    return "\n".join(lines)


def build_hypotheses_block(
    country: str,
    top_combos: List[Dict[str, Any]],
    targeting_rows: List[Dict[str, Any]],
    creative_rows: List[Dict[str, Any]],
) -> str:
    """Return a pre-formatted paste block for 'suggest test hypotheses' (section 7)."""
    lines = [
        f"Country: {country}",
        "",
        "Data summary for hypothesis generation (paste into Cursor chat and ask:",
        "'Based on this data, suggest 3 test hypotheses for new ad sets'):",
        "",
        "Working combos:",
    ]
    for c in top_combos[:3]:
        lines.append(
            f"  - {_combo_label(c['device'], c['interest'], c['creative'])}  [{c['category']}, {c['deposits']} dep]"
        )

    top_targets = [t for t in targeting_rows if t["deposits"] > 0][:5]
    if top_targets:
        lines.append("")
        lines.append("Top targeting by deposits:")
        for t in top_targets:
            lines.append(f"  - {t['interest']}: {t['deposits']} dep, ${t['spend']:,.0f} spend")

    zero_targets = [t for t in targeting_rows if t["deposits"] == 0 and t["spend"] > 50][:3]
    if zero_targets:
        lines.append("")
        lines.append("Failing targeting (0 dep, notable spend):")
        for t in zero_targets:
            lines.append(f"  - {t['interest']}: ${t['spend']:,.0f} spend, 0 dep")

    top_creatives = [c for c in creative_rows if c["deposits"] > 0][:5]
    if top_creatives:
        lines.append("")
        lines.append("Top creatives by deposits:")
        for c in top_creatives:
            lines.append(f"  - {c['ad_title']}: {c['deposits']} dep, ${c['spend']:,.0f} spend")

    return "\n".join(lines)


# ── Section 10: Close recommendations ─────────────────────────────────────────

def build_account_performance_summary(
    df: pd.DataFrame,
    country: str,
    account_alias: str,
) -> Dict[str, Any]:
    """Aggregate full account performance for close recommendation context."""
    df_c = df[
        (df["Country Final"] == country)
        & (df["Account Alias"].fillna("").astype(str) == account_alias)
    ]
    has_data = not df_c.empty
    leads = _safe_int(df_c["# Leads"].sum()) if has_data else 0
    deposits = _safe_int(df_c["# FTDs"].sum()) if has_data else 0
    spend = _safe_float(df_c["$ Spend"].sum()) if has_data else 0.0
    cpl = spend / leads if has_data and leads else None
    cpa = spend / deposits if has_data and deposits else None
    cr = (deposits / leads * 100.0) if has_data and leads else None
    return {
        "has_data": has_data,
        "leads": leads,
        "deposits": deposits,
        "spend": spend,
        "cpl": cpl,
        "cpa": cpa,
        "cr": cr,
    }


def aggregate_adsets_merged_for_account(
    df_c: pd.DataFrame,
) -> pd.DataFrame:
    """Roll up enriched Excel rows to **one row per Adset Name** for report tables.

    Sums ``# FTDs``, ``# Leads``, ``$ Spend`` across all rows that share the same
    ad set (even when ``Offer`` / ``Ad Asset`` differs row-to-row).

    Adds ``Offer``: the offer value among sub-rows whose summed spend for that ad
    set is highest (for benchmarks / notes). If the ``Offer`` column is missing,
    the column is omitted from the result.

    ``df_c`` must already be scoped (e.g. one country + one ``Account Alias``).
    Used by Optimization (§9) Markdown/HTML and any future ad-set campaign views.
    """
    if df_c.empty:
        cols = ["Adset Name", "Deposits", "Leads", "Spend"]
        if "Offer" in df_c.columns:
            cols.append("Offer")
        return pd.DataFrame(columns=cols)

    offer_col = "Offer" if "Offer" in df_c.columns else None

    agg_dict = {
        "# FTDs": ("# FTDs", "sum"),
        "# Leads": ("# Leads", "sum"),
        "$ Spend": ("$ Spend", "sum"),
    }
    grouped = df_c.groupby(["Adset Name"], dropna=False).agg(**agg_dict).reset_index()
    grouped.rename(columns={"# FTDs": "Deposits", "# Leads": "Leads", "$ Spend": "Spend"}, inplace=True)

    if offer_col:
        spend_by_offer = (
            df_c.groupby(["Adset Name", offer_col], dropna=False)["$ Spend"]
            .sum()
            .reset_index()
        )
        spend_by_offer = spend_by_offer.sort_values("$ Spend", ascending=False)
        dominant = spend_by_offer.drop_duplicates(subset=["Adset Name"], keep="first")
        dominant = dominant.rename(columns={offer_col: "Offer"})
        grouped = grouped.merge(dominant[["Adset Name", "Offer"]], on="Adset Name", how="left")

    return grouped


def build_close_recommendations(
    df: pd.DataFrame,
    country: str,
    account_alias: str,
    min_spend: float = 0.0,
    top_n: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Campaign-level optimization rows for a ready account, sorted by spend.

    One row per **Adset Name**: spend, leads and FTDs are summed across all rows
    for that ad set (including rows that differ only in ``Offer`` / ``Ad Asset``),
    so the table does not show duplicate ad-set names with split metrics.

    For benchmark / reason text, the **Offer** on the row is the one with the largest
    spend among sub-rows for that ad set (ties: first after sort).

    Includes rows with leads or deposits even when spend is zero — exports sometimes
    put account-level leads on ``Adset Name`` placeholders (``-`` / empty) with no spend,
    while spend lives on named ad sets with blank lead counts.
    """
    df_c = df[
        (df["Country Final"] == country)
        & (df["Account Alias"].fillna("").astype(str) == account_alias)
    ].copy()
    if df_c.empty:
        return []

    offer_col = "Offer" if "Offer" in df_c.columns else None
    grouped = aggregate_adsets_merged_for_account(df_c)

    def _row_cpl_cpa(r) -> tuple[Optional[float], Optional[float]]:
        deposits = _safe_int(r.get("Deposits"))
        leads = _safe_int(r.get("Leads"))
        spend = _safe_float(r.get("Spend"))
        if leads > 0 and spend > 0:
            cpl_v = spend / leads
        else:
            cpl_v = None
        if deposits > 0 and spend > 0:
            cpa_v = spend / deposits
        else:
            cpa_v = None
        return cpl_v, cpa_v

    def _optimization_reason(r) -> str:
        deposits = _safe_int(r.get("Deposits"))
        spend = _safe_float(r.get("Spend"))
        if deposits == 0 and spend >= 30.0:
            return "0 FTD"
        offer = str(r.get("Offer") or "").strip() if offer_col else ""
        bench = get_benchmark(country, offer or None)
        cpl, cpa = _row_cpl_cpa(r)
        reasons = []
        if bench.get("max_cpl") and cpl is not None and cpl > bench["max_cpl"]:
            reasons.append(f"CPL > bench ${bench['max_cpl']:,.0f}")
        if bench.get("max_cpa") and cpa is not None and cpa > bench["max_cpa"]:
            reasons.append(f"CPA > bench ${bench['max_cpa']:,.0f}")
        return "; ".join(reasons)

    def _make_rec(r) -> Dict[str, Any]:
        deposits = _safe_int(r.get("Deposits"))
        leads = _safe_int(r.get("Leads"))
        spend = _safe_float(r.get("Spend"))
        cpl, cpa = _row_cpl_cpa(r)
        return {
            "adset_name": str(r.get("Adset Name") or "").strip(),
            "deposits": deposits,
            "leads": leads,
            "spend": spend,
            "cpl": cpl,
            "cpa": cpa,
            "reason": _optimization_reason(r),
        }

    spend_ok = grouped["Spend"] > min_spend
    leads_ok = grouped["Leads"].fillna(0) > 0
    dep_ok = grouped["Deposits"].fillna(0) > 0
    visible = grouped[spend_ok | leads_ok | dep_ok].sort_values("Spend", ascending=False)
    if top_n is not None:
        visible = visible.head(top_n)
    return [_make_rec(r) for _, r in visible.iterrows()]


def build_close_recommendation_summary(
    account_summary: Dict[str, Any],
    close_recs: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Summarize full account totals beside only the ad sets recommended to close."""
    return {
        "account": account_summary,
        "candidates": {
            "leads": sum(_safe_int(r.get("leads")) for r in close_recs),
            "deposits": sum(_safe_int(r.get("deposits")) for r in close_recs),
            "spend": sum(_safe_float(r.get("spend")) for r in close_recs),
        },
    }


# ── Section 11: Open 2 campaigns ──────────────────────────────────────────────

# Short slugs used in real ad-set names for each Ad Asset (offer) value.
_OFFER_ABBR: Dict[str, str] = {
    "amazon": "Amz",
    "amazon": "Amz",
    "ai trader": "Ait",
    "ai trading": "Ait",
    "al trading": "Alt",
    "trader ai": "Trl",
    "trolley": "Trl",
    "favorita": "Fav",
    "corporación favorita": "Fav",
    "corporacion favorita": "Fav",
    "salik": "Slk",
    "banco industrial": "Bid",
    "banco g&t continental": "Bgt",
    "bancolombia": "Bnc",
    "mercado libre": "Ml",
    "adnoc": "Adc",
    "chatgpt": "Cgpt",
    "chatgptplatform": "Cgpt",
    "finance eagle": "Fe",
    "aa acady": "Acd",
    "al salam bank": "Asb",
    "lulu retail": "Llu",
    "fab": "Fab",
    "qatar energy": "Qe",
    "sqm": "Sqm",
    "enel": "Enl",
    "aramco": "Arm",
    "holcim": "Hlc",
    "lundin gold inc": "Lnd",
    "zeon grow": "Zen",
    "bimboa": "Bim",
    "aes el salvador": "Aes",
    "cervecería nacional": "Cvn",
    "cerveceria nacional": "Cvn",
    "grupo aval": "Gav",
    "banorte": "Bnt",
}


def _is_placeholder_offer(offer: str) -> bool:
    return (offer or "").strip().lower() in ("", "-", "nan", "none", NA_SENTINEL.lower())


def _offer_abbr(offer: str, country_name: str | None = None) -> str:
    """Return short slug for offer name, e.g. 'AI Trader' → 'Ait'."""
    key = (offer or "").strip().lower()
    if _is_placeholder_offer(key):
        return "Trl"
    if key in _OFFER_ABBR:
        return _OFFER_ABBR[key]
    # Fallback: CamelCase first letters, max 4 chars
    parts = offer.strip().split()
    return "".join(w.capitalize() for w in parts)[:4] if parts else "Unk"


def _display_offer(offer: Any, country_name: str | None = None) -> str:
    offer_s = _display_value(offer)
    if _is_placeholder_offer(offer_s):
        return "Trolley"
    return offer_s


def _creative_type(creative: str) -> str:
    """Return 'Image' or 'Video' based on the creative/Ad Title value."""
    return "Image" if (creative or "").strip().lower().startswith("image") else "Video"


def _creative_type_for_campaign(creatives: List[str]) -> str:
    """Return campaign creative type; Mix means selected ads include image and video."""
    types = {_creative_type(c) for c in creatives if str(c or "").strip()}
    if "Image" in types and "Video" in types:
        return "Mix"
    if "Image" in types:
        return "Image"
    return "Video"


def _is_nan_val(v: Any) -> bool:
    """True if the value is empty / nan / NA_SENTINEL."""
    if v is None:
        return True
    s = str(v).strip().lower()
    return s in ("", "nan", "none", NA_SENTINEL.lower())


def _combo_passes_benchmark(combo: Dict[str, Any], bench: Dict[str, Any]) -> bool:
    """True if combo meets all configured benchmarks."""
    cpl = combo.get("cpl", 0.0)
    cpa = combo.get("cpa", 0.0)
    cr = combo.get("cr", 0.0)

    max_cpl = bench.get("max_cpl")
    max_cpa = bench.get("max_cpa")
    min_cr = bench.get("min_cr")

    if max_cpl is not None and cpl > 0 and cpl > max_cpl:
        return False
    if max_cpa is not None and cpa > 0 and cpa > max_cpa:
        return False
    if min_cr is not None and cr < min_cr:
        return False
    return True


def _group_combos_by_targeting(
    combos_df: pd.DataFrame, country: str
) -> Dict[str, List[Dict[str, Any]]]:
    """Group all Strong/Interesting combos by interest, rank creatives within."""
    df_c = combos_df[combos_df["Country Final"] == country].copy()
    if df_c.empty:
        return {}

    qualified = df_c[df_c["Category"].isin(["Strong", "Interesting"])].copy()
    if qualified.empty:
        return {}

    qualified = qualified.sort_values(
        ["Deposits", "CR", "ROI"], ascending=[False, False, False]
    )

    groups: Dict[str, List[Dict[str, Any]]] = {}
    for _, r in qualified.iterrows():
        interest = str(r.get("Interest") or "").strip()
        if not interest or interest.lower() == "nan":
            interest = "(unknown)"
        entry = {
            "offer": _display_offer(r.get("Offer"), country),
            "device": _display_value(r.get("Device"), "all"),
            "interest": interest,
            "age": _display_value(r.get("Age")),
            "creative": _display_value(r.get("Creative")),
            "category": str(r.get("Category", "Strong")),
            "deposits": _safe_int(r.get("Deposits")),
            "leads": _safe_int(r.get("Leads")),
            "spend": _safe_float(r.get("Spend")),
            "cpl": _safe_float(r.get("CPL")),
            "cpa": _safe_float(r.get("CPA")),
            "cr": _safe_float(r.get("CR")),
            "roi": _safe_float(r.get("ROI")),
        }
        groups.setdefault(interest, []).append(entry)
    return groups


def _get_running_interests(
    df: pd.DataFrame, country: str, account_alias: str
) -> set:
    """Return set of Interest values already active on this account."""
    mask = (
        (df["Country Final"] == country)
        & (df["Account Alias"].fillna("").astype(str) == account_alias)
        & (df["$ Spend"] > 0)
    )
    if "Interest" not in df.columns:
        return set()
    vals = df.loc[mask, "Interest"].dropna().astype(str).str.strip().unique()
    return {v for v in vals if v and v.lower() != "nan"}


# §10 strategy buckets — aligned with §2 Trend (Recent Days)
OPEN_STRATEGY_LABELS: Dict[int, str] = {
    1: "1 · trend + stable",
    2: "2 · trend + variation",
    3: "3 · trend + recovery",
    4: "4 · test",
}


def _full_creative_from_trend(entry: Dict[str, Any]) -> str:
    ak = entry.get("aggregate_key") or {}
    cr = ak.get("creative")
    if cr and str(cr).strip() not in ("", NA_SENTINEL):
        return str(cr).strip()
    return str(entry.get("creative") or "").strip()


def _lookup_combo_in_groups(
    groups: Dict[str, List[Dict[str, Any]]],
    offer: str,
    interest: str,
    creative: str,
) -> Optional[Dict[str, Any]]:
    """Match a §2 trend row to a Strong/Interesting combo when possible."""
    for combos in groups.values():
        for c in combos:
            if (
                str(c.get("offer") or "").strip() == str(offer or "").strip()
                and str(c.get("interest") or "").strip() == str(interest or "").strip()
                and _creative_matches_trend_row(creative, str(c.get("creative") or ""))
            ):
                return c
    return None


def _combo_from_trend_entry(
    entry: Dict[str, Any],
    groups: Dict[str, List[Dict[str, Any]]],
) -> Dict[str, Any]:
    """Build a combo dict for §10 from a §2 trend row (enriched from groups when found)."""
    offer = str(entry.get("offer") or "").strip()
    interest = str(entry.get("interest") or "").strip()
    creative = _full_creative_from_trend(entry)
    found = _lookup_combo_in_groups(groups, offer, interest, creative)
    if found:
        return dict(found)
    return {
        "offer": offer,
        "device": str(entry.get("device") or "all").strip() or "all",
        "interest": interest,
        "age": "",
        "creative": creative,
        "category": "Trend",
        "deposits": _safe_int(entry.get("deposits_total")),
        "leads": 0,
        "spend": 0.0,
        "cpl": 0.0,
        "cpa": 0.0,
        "cr": 0.0,
        "roi": 0.0,
    }


def _variation_creatives_for_entry(
    trend: Optional[Dict[str, Any]],
    entry: Dict[str, Any],
    groups: Dict[str, List[Dict[str, Any]]],
    *,
    max_extra: int = 2,
) -> List[Dict[str, Any]]:
    """Other hot creatives in the same interest (§2 trending_up) or same combo group."""
    if not trend:
        return []
    interest = str(entry.get("interest") or "").strip()
    offer = str(entry.get("offer") or "").strip()
    primary_cr = _full_creative_from_trend(entry)
    out: List[Dict[str, Any]] = []
    seen = {primary_cr[:40]}

    for e in trend.get("trending_up", []):
        if str(e.get("interest") or "").strip() != interest:
            continue
        cr = _full_creative_from_trend(e)
        key = cr[:40]
        if key in seen:
            continue
        seen.add(key)
        combo = _combo_from_trend_entry(e, groups)
        tnote = _creative_trend_note(trend, combo["offer"], interest, cr)
        note = "variation: same targeting, new creative"
        if tnote:
            note = f"{note}; {tnote}"
        out.append({
            "name": cr[:40],
            "deposits": _safe_int(e.get("deposits_recent") or e.get("deposits_total")),
            "roi": combo.get("roi", 0.0),
            "note": note,
        })
        if len(out) >= max_extra:
            break

    if len(out) < max_extra and interest in groups:
        for c in groups[interest]:
            cr = str(c.get("creative") or "")
            key = cr[:40]
            if key in seen:
                continue
            if _creative_trend_note(trend, c["offer"], interest, cr) != "trend: all recent dep":
                continue
            seen.add(key)
            out.append({
                "name": cr[:40],
                "deposits": c["deposits"],
                "roi": c["roi"],
                "note": "variation: scale sibling creative",
            })
            if len(out) >= max_extra:
                break
    return out


def _recovery_creatives_for_entry(
    trend: Optional[Dict[str, Any]],
    entry: Dict[str, Any],
    groups: Dict[str, List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    """Stopped combo + fresh creatives from the same interest/offer in §2 New."""
    interest = str(entry.get("interest") or "").strip()
    offer = str(entry.get("offer") or "").strip()
    stale_cr = _full_creative_from_trend(entry)
    combo = _combo_from_trend_entry(entry, groups)
    creatives_out: List[Dict[str, Any]] = [{
        "name": stale_cr[:40],
        "deposits": _safe_int(entry.get("deposits_total")),
        "roi": combo.get("roi", 0.0),
        "note": "recovery: was working, refresh hook/creative",
    }]
    if trend:
        for e in trend.get("trending_up", []):
            if str(e.get("interest") or "").strip() != interest:
                continue
            if str(e.get("offer") or "").strip() != offer:
                continue
            cr = _full_creative_from_trend(e)
            if _creative_matches_trend_row(stale_cr, cr):
                continue
            nc = _combo_from_trend_entry(e, groups)
            creatives_out.append({
                "name": cr[:40],
                "deposits": _safe_int(e.get("deposits_recent")),
                "roi": nc.get("roi", 0.0),
                "note": "recovery: pair with new hot creative",
            })
            if len(creatives_out) >= 3:
                break
    return creatives_out


def _pick_creatives_for_combo(
    trend: Optional[Dict[str, Any]],
    primary: Dict[str, Any],
    siblings: List[Dict[str, Any]],
    *,
    n_creatives: int,
    allow_stalled_borrow: bool,
) -> List[Dict[str, Any]]:
    """Select creatives; never pull stalled global winners into non-recovery slots."""
    interest = str(primary.get("interest") or "").strip()
    creatives_out: List[Dict[str, Any]] = []
    for c in [primary, *siblings]:
        if len(creatives_out) >= n_creatives:
            break
        cr = str(c.get("creative") or "")
        key = cr[:40]
        if any(x["name"] == key for x in creatives_out):
            continue
        tnote = _creative_trend_note(trend, c["offer"], interest, cr)
        if not allow_stalled_borrow and tnote == "trend: stalled (recovery)":
            continue
        creatives_out.append({
            "name": key,
            "deposits": c.get("deposits", 0),
            "roi": c.get("roi", 0.0),
            "note": tnote,
        })
    return creatives_out


def _assemble_open_campaign(
    *,
    strategy_type: int,
    primary: Dict[str, Any],
    combos_list: List[Dict[str, Any]],
    total_dep: int,
    is_running: bool,
    country_code: str,
    country_name: str,
    today: str,
    trend: Optional[Dict[str, Any]],
    medians: Dict[str, Any],
    creatives_out: List[Dict[str, Any]],
    argument_suffix: str = "",
) -> Dict[str, Any]:
    """Build one §10 campaign recommendation dict."""
    interest = str(primary.get("interest") or "").strip()
    offer = str(primary.get("offer") or "").strip()
    device_raw = primary.get("device") or "all"
    age_raw = primary.get("age") or ""

    bench = get_benchmark(country_name, offer or None)
    quality = _check_quality(primary, bench)
    offer_part = _offer_abbr(offer, country_name=country_name)
    device_part = str(device_raw).capitalize()
    age_part = f"_{age_raw}" if not _is_nan_val(age_raw) else ""

    argument = f"{total_dep} dep"
    if is_running:
        argument += ", scale up"
    if argument_suffix:
        argument += f", {argument_suffix}"

    trend_tags = _interest_trend_tags(trend, interest)
    if trend_tags:
        argument += f", trend: {', '.join(trend_tags)}"

    creative_type = _creative_type_for_campaign([cr["name"] for cr in creatives_out])
    campaign_name = (
        f"{country_code}_{offer_part}_{device_part}"
        f"_{creative_type}_{interest}{age_part}_{today}"
    )

    return {
        "strategy_type": strategy_type,
        "strategy_label": OPEN_STRATEGY_LABELS[strategy_type],
        "campaign_name": campaign_name,
        "targeting": interest,
        "offer": offer,
        "category": primary.get("category", "Strong"),
        "quality": quality,
        "total_deposits": total_dep,
        "argument": argument,
        "creatives": creatives_out,
        "below_benchmark": quality == "warn",
        "data_median_cpl": medians.get("median_cpl"),
        "trend_tags": trend_tags,
    }


def open_recs_for_status_table(
    recs: List[Dict[str, Any]],
    *,
    max_campaigns: int = 2,
) -> List[Dict[str, Any]]:
    """Pick §6–7 To Do campaigns: only strategies 1–2 (stable / new trend), never recovery."""
    if not recs:
        return []
    order = {1: 0, 2: 1, 3: 2, 4: 3}
    primary = sorted(
        [r for r in recs if int(r.get("strategy_type") or 99) in (1, 2)],
        key=lambda r: order.get(int(r.get("strategy_type") or 99), 99),
    )
    return primary[:max_campaigns]


def build_open_recommendations(
    combos_df: pd.DataFrame,
    df: pd.DataFrame,
    account_alias: str,
    account_label: str,
    country_code: str,
    country_name: str,
    medians: Dict[str, Any],
    *,
    trend: Optional[Dict[str, Any]] = None,
    n_creatives: int = 3,
) -> List[Dict[str, Any]]:
    """Return up to four §10 recommendations — one per strategy bucket (§2 Trend).

    1. trend + stable — deposits before and now (§2 Stable + Strong combos)
    2. trend + variation — §2 New (all recent dep) + sibling creatives
    3. trend + recovery — §2 Stopped + fresh creatives from same interest
    4. test — when no trend signal; best unexplored Strong/Interesting combo
    """
    today = datetime.now().strftime("%d.%m")
    groups = _group_combos_by_targeting(combos_df, country_name)
    running = _get_running_interests(df, country_name, account_alias)
    has_trend = bool(trend and trend.get("has_date"))
    recs: List[Dict[str, Any]] = []
    used_interests: set = set()

    def _append_rec(
        strategy_type: int,
        primary: Dict[str, Any],
        combos_list: List[Dict[str, Any]],
        *,
        creatives_out: List[Dict[str, Any]],
        argument_suffix: str,
        allow_running: bool = False,
    ) -> None:
        interest = str(primary.get("interest") or "").strip()
        if interest in used_interests:
            return
        if not allow_running and interest in running:
            return
        total_dep = sum(c["deposits"] for c in combos_list) or primary.get("deposits", 0)
        if not creatives_out:
            siblings = [c for c in combos_list if c is not primary]
            creatives_out = _pick_creatives_for_combo(
                trend,
                primary,
                siblings,
                n_creatives=n_creatives,
                allow_stalled_borrow=(strategy_type == 3),
            )
        if not creatives_out:
            return
        used_interests.add(interest)
        recs.append(
            _assemble_open_campaign(
                strategy_type=strategy_type,
                primary=primary,
                combos_list=combos_list,
                total_dep=int(total_dep),
                is_running=interest in running,
                country_code=country_code,
                country_name=country_name,
                today=today,
                trend=trend,
                medians=medians,
                creatives_out=creatives_out,
                argument_suffix=argument_suffix,
            )
        )

    # 1 · trend + stable
    if has_trend:
        stable_entries = sorted(
            trend.get("stable", []),
            key=lambda e: (
                -int(e.get("deposits_recent", 0) or 0),
                -int(e.get("deposits_total", 0) or 0),
            ),
        )
        for entry in stable_entries:
            primary = _combo_from_trend_entry(entry, groups)
            interest = str(primary.get("interest") or "").strip()
            combos_list = sorted(
                groups.get(interest, [primary]),
                key=lambda c: (-int(c.get("deposits", 0)), -_safe_float(c.get("roi"))),
            )
            siblings = [c for c in combos_list if c is not primary]
            creatives_out = _pick_creatives_for_combo(
                trend, primary, siblings, n_creatives=n_creatives, allow_stalled_borrow=False,
            )
            _append_rec(
                1, primary, combos_list, creatives_out=creatives_out,
                argument_suffix="stable + ongoing deposits",
                allow_running=True,
            )
            break

    # 2 · trend + variation (§2 New)
    if has_trend:
        new_entries = sorted(
            trend.get("trending_up", []),
            key=lambda e: -int(e.get("deposits_recent", 0) or 0),
        )
        for entry in new_entries:
            primary = _combo_from_trend_entry(entry, groups)
            interest = str(primary.get("interest") or "").strip()
            combos_list = groups.get(interest, [primary])
            creatives_out = [{
                "name": _full_creative_from_trend(entry)[:40],
                "deposits": _safe_int(entry.get("deposits_recent")),
                "roi": primary.get("roi", 0.0),
                "note": "trend: all recent dep",
            }]
            creatives_out.extend(
                _variation_creatives_for_entry(trend, entry, groups, max_extra=n_creatives - 1)
            )
            _append_rec(
                2, primary, combos_list, creatives_out=creatives_out[:n_creatives],
                argument_suffix="new trend — scale + variations",
            )
            break

    # 3 · trend + recovery (§2 Stopped)
    if has_trend:
        stopped_entries = sorted(
            trend.get("trending_down", []),
            key=lambda e: -int(e.get("deposits_total", 0) or 0),
        )
        for entry in stopped_entries:
            primary = _combo_from_trend_entry(entry, groups)
            interest = str(primary.get("interest") or "").strip()
            combos_list = groups.get(interest, [primary])
            creatives_out = _recovery_creatives_for_entry(trend, entry, groups)[:n_creatives]
            _append_rec(
                3, primary, combos_list, creatives_out=creatives_out,
                argument_suffix="recovery — was working, refresh",
            )
            break

    # 4 · test — only when no trend window or nothing stable (bucket 1)
    has_stable_rec = any(r.get("strategy_type") == 1 for r in recs)
    if groups and (not has_trend or (has_trend and not has_stable_rec)):
        scored: List[Dict[str, Any]] = []
        for interest, combos in groups.items():
            if interest in used_interests:
                continue
            total_dep = sum(c["deposits"] for c in combos)
            boost = _interest_trend_boost(trend, interest) if has_trend else 0
            scored.append({
                "interest": interest,
                "combos": combos,
                "total_dep": total_dep,
                "boost": boost,
                "is_running": interest in running,
            })
        scored.sort(key=lambda g: (-int(not g["is_running"]), -g["boost"], -g["total_dep"]))
        for grp in scored:
            combos_list = grp["combos"]
            primary = combos_list[0]
            siblings = combos_list[1:]
            creatives_out = _pick_creatives_for_combo(
                trend, primary, siblings, n_creatives=n_creatives, allow_stalled_borrow=False,
            )
            suffix = "explore — no clear trend" if not has_trend else "test — diversify"
            _append_rec(4, primary, combos_list, creatives_out=creatives_out, argument_suffix=suffix)
            break

    return recs


def _ellipsis_short(text: str, max_len: int) -> str:
    """Trim for narrow table cells; use ellipsis when truncated."""
    s = (text or "").strip()
    if max_len <= 1 or len(s) <= max_len:
        return s
    return s[: max_len - 1] + "…"


def format_open_recommendations_for_status_table(
    recs: List[Dict[str, Any]],
    *,
    country_name: str | None = None,
    max_campaigns: int = 2,
    target_max: int = 12,
    creative_max: int = 16,
) -> str:
    """One-line summary of section 10 open recs for the Account Statuses table.

    Dense form for narrow cells: ``slug/target/creative`` per campaign
    (``slug`` = `_offer_abbr`, same order and first creative as §10),
    ``; `` between campaigns. Append ``⚠`` when below benchmark (no space).
    """
    if not recs:
        return ""
    parts: List[str] = []
    for r in recs[:max_campaigns]:
        offer = (r.get("offer") or "").strip() or "—"
        abbr = _offer_abbr(offer, country_name=country_name) if offer != "—" else "—"
        tgt = (r.get("targeting") or "").strip() or "—"
        creatives = r.get("creatives") or []
        cr0 = creatives[0] if creatives else {}
        cr_name = str(cr0.get("name", "")).strip() or "—"
        warn = "⚠" if r.get("below_benchmark") else ""
        parts.append(
            f"{abbr}/{_ellipsis_short(tgt, target_max)}/"
            f"{_ellipsis_short(cr_name, creative_max)}{warn}"
        )
    return "; ".join(parts)


def _account_status_todo_display(
    acc: Dict[str, Any],
    open_recs_for_account: List[Dict[str, Any]],
    country_name: str | None = None,
) -> str:
    """To Do cell: mirror section 10 for ready accounts; GSheet for Video selfie / not ready."""
    raw_todo = str(acc.get("to_do", "") or "")
    if "video selfie" in raw_todo.lower():
        return format_account_status_todo_cell(raw_todo, str(acc.get("to_do_extra", "") or ""))
    if acc.get("is_ready"):
        if open_recs_for_account:
            return format_open_recommendations_for_status_table(
                open_recs_for_status_table(open_recs_for_account),
                country_name=country_name,
            )
        # Section 9 omits accounts with no open recs — same here.
        return "—"
    return format_account_status_todo_cell(raw_todo, str(acc.get("to_do_extra", "") or ""))


# ── Country section builder ────────────────────────────────────────────────────

def build_country_section(
    df: pd.DataFrame,
    combos: pd.DataFrame,
    country: str,
    account_rows: List[Dict[str, Any]],
    my_cm: str,
    trend_days: int = 7,
) -> Dict[str, Any]:
    """Build all country sub-sections (rendered as §0–§10 in Markdown)."""
    top_combos = build_top_combos(combos, country)
    targeting = build_targeting_breakdown(df, country)
    creatives = build_creative_breakdown(df, country)
    medians = compute_country_medians(df, country)
    trend_block = build_trend(df, country, trend_days)

    country_accounts = [
        dict(acc) for acc in account_rows
        if not is_omitted_from_account_statuses_table(acc.get("to_do", ""))
    ]

    for acc in country_accounts:
        acc["account_excel"] = build_account_performance_summary(
            df, country, acc["alias"],
        )

    close_recs: Dict[str, List] = {}
    close_summaries: Dict[str, Dict[str, Any]] = {}
    open_recs: Dict[str, List] = {}
    for acc in country_accounts:
        alias = acc["alias"]
        if acc["is_ready"]:
            account_summary = build_account_performance_summary(df, country, alias)
            account_close_recs = build_close_recommendations(df, country, alias)
            close_recs[acc["account_label"]] = account_close_recs
            if account_close_recs:
                close_summaries[acc["account_label"]] = build_close_recommendation_summary(
                    account_summary,
                    account_close_recs,
                )
            cc = acc["country_code"] or country[:2].upper()
            open_recs[acc["account_label"]] = build_open_recommendations(
                combos_df=combos, df=df,
                account_alias=alias,
                account_label=acc["account_label"],
                country_code=cc,
                country_name=country,
                medians=medians,
                trend=trend_block,
            )

    for acc in country_accounts:
        label = acc["account_label"]
        acc["to_do_display"] = _account_status_todo_display(
            acc,
            open_recs.get(label, []),
            country_name=country,
        )

    return {
        "country": country,
        "cm_comparison": build_cm_comparison(df, country, my_cm),
        "asset_summary": build_asset_summary(df, country),
        "trend": trend_block,
        "top_combos": top_combos,
        "targeting": targeting,
        "creatives": creatives,
        "ai_context": build_ai_context_block(country, top_combos),
        "hypotheses": build_hypotheses_block(country, top_combos, targeting, creatives),
        "account_rows": country_accounts,
        "close_recs": close_recs,
        "close_summaries": close_summaries,
        "open_recs": open_recs,
        "medians": medians,
    }


# ── Main report builder ────────────────────────────────────────────────────────

def build_morning_report(
    excel_path: Optional[str] = None,
    account_rows: Optional[List[Dict[str, Any]]] = None,
    *,
    my_cm: str = "Evgeni Sedin",
    trend_days: int = 7,
    country_filter: Optional[str] = None,
    top_combos_n: int = 5,
    df: Optional[pd.DataFrame] = None,
) -> Dict[str, Any]:
    """Build the full MorningReport dict.

    Args:
        excel_path: path to stats Excel (cursor example.xlsx). Required
            unless `df` is given.
        account_rows: parsed rows from gsheet_accounts.fetch_account_rows().
            Pass [] for none.
        my_cm: CM name to highlight in section 1.
        trend_days: window for section 2 trend.
        country_filter: if set, only include this country (None = all).
        top_combos_n: max combos to show per country in section 3.
        df: optional pre-loaded enriched DataFrame (post compute_metrics +
            enrich_structure). When supplied, skips Excel I/O. Used by
            morning_service to avoid double-parsing.

    Returns:
        MorningReport dict with keys: date, source_excel, my_cm, countries[].
    """
    if account_rows is None:
        account_rows = []

    if df is None:
        if not excel_path:
            raise ValueError("build_morning_report: excel_path or df required")
        required_cols = [
            "Country", "Account Alias", "Adset Name", "Ad Title",
            "# Leads", "# FTDs", "$ Spend", "CM", "Ad Asset", "$ Net",
        ]
        df_raw = pd.read_excel(excel_path)
        missing = [c for c in required_cols if c not in df_raw.columns]
        if missing:
            raise ValueError(f"Missing columns in Excel: {missing}")
        df = compute_metrics(df_raw)
        df = enrich_structure(df)

    combos = aggregate_combos(df)

    all_countries = sorted(
        c for c in df["Country Final"].dropna().astype(str).unique()
        if c.strip() and c.lower() != "nan"
    )
    if country_filter:
        want = country_filter.strip().lower()
        all_countries = [c for c in all_countries if c.strip().lower() == want]

    def _accounts_for_country(country: str) -> List[Dict[str, Any]]:
        """GSheet rows for this country only (column B country code must match).

        Does not use Excel `Account Alias` to attach an account to a country — that caused
        the same alias (e.g. reused across geo) to appear under the wrong country section.

        Column B may be ISO codes (``KW``), short slugs (``uae``), or full names (``Kuwait``)
        matching Excel ``Country Final`` spelling — see ``canonical_gsheet_country_code``.
        """
        expected_code = _EXCEL_COUNTRY_TO_GSHEET_CODE.get(country.strip().lower(), "")
        if not expected_code:
            return []

        seen: set = set()
        matched: List[Dict[str, Any]] = []
        for r in account_rows:
            row_code_raw = str(r.get("country_code", "")).strip()
            row_canonical = canonical_gsheet_country_code(row_code_raw)
            if row_canonical != expected_code:
                continue
            alias = r["alias"]
            if alias in seen:
                continue
            seen.add(alias)
            matched.append(r)
        return matched

    country_sections = []
    for country in all_countries:
        acc_for_country = _accounts_for_country(country)
        section = build_country_section(
            df, combos, country, acc_for_country, my_cm, trend_days
        )
        country_sections.append(section)

    return {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "source_excel": str(excel_path),
        "my_cm": my_cm,
        "trend_days": trend_days,
        "countries": country_sections,
        "total_combos": len(combos),
        "has_gsheet": len(account_rows) > 0,
    }
