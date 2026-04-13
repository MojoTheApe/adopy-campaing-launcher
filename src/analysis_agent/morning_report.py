"""Build the MorningReport data model — 11 sections per country.

Calls existing analysis pipeline (compute_metrics / enrich_structure / aggregate_combos)
and adds new aggregations for sections 2 (trend), 4 (targeting), 5 (creatives),
10 (close recs), 11 (open recs).

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
from src.knowledge_agent.knowledge_base import format_target_context_line, get_all_targets


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
            }
        )
    # Put my row first
    rows.sort(key=lambda x: (not x["is_me"], -x["net"]))
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
        return {
            "offer": str(r.get("Offer") or "").strip(),
            "device": str(r.get("Device") or "all").strip(),
            "interest": str(r.get("Interest") or "").strip(),
            "creative": str(r.get("Creative") or "").strip()[:40],
            "deposits_total": _safe_int(r.get("Deposits")),
            "deposits_recent": _safe_int(r.get("RecentDep")),
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


# ── Section 3: Top combos (from aggregate_combos output) ──────────────────────

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
        result.append(
            {
                "offer": str(r.get("Offer") or "").strip(),
                "device": str(r.get("Device") or "all").strip(),
                "interest": str(r.get("Interest") or "").strip(),
                "age": str(r.get("Age") or "").strip(),
                "creative": str(r.get("Creative") or "").strip(),
                "category": str(r.get("Category", "Strong")),
                "deposits": _safe_int(r.get("Deposits")),
                "leads": _safe_int(r.get("Leads")),
                "spend": _safe_float(r.get("Spend")),
                "cpa": _safe_float(r.get("CPA")),
                "roi": _safe_float(r.get("ROI")),
                "top_account": str(r.get("TopAccount") or "").strip(),
            }
        )
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
        interest = str(r.get("Interest") or "").strip()
        if not interest or interest.lower() == "nan":
            interest = "(unknown)"
        rows.append(
            {
                "interest": interest,
                "deposits": _safe_int(r.get("Deposits")),
                "leads": _safe_int(r.get("Leads")),
                "spend": _safe_float(r.get("Spend")),
                "cpa": _safe_float(r.get("CPA")),
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
        title = str(r.get("Ad Title") or "").strip()
        if not title or title.lower() == "nan":
            title = "(no title)"
        rows.append(
            {
                "ad_title": title,
                "deposits": _safe_int(r.get("Deposits")),
                "leads": _safe_int(r.get("Leads")),
                "spend": _safe_float(r.get("Spend")),
                "cpa": _safe_float(r.get("CPA")),
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
            f"{i}. [{c['category']}] {c['device']} + {c['interest']} + {c['age']} + {c['creative']}"
            f"  |  Offer: {c['offer']}  |  Dep: {c['deposits']}  |  ROI: {_fmt(c['roi'])}"
        )
        # Pull target context from KB
        ctx = format_target_context_line(c["interest"]) if c["interest"] else None
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
            f"  - {c['device']} + {c['interest']} + {c['creative']}  [{c['category']}, {c['deposits']} dep]"
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

def build_close_recommendations(
    df: pd.DataFrame,
    country: str,
    account_alias: str,
    min_spend: float = 30.0,
    top_n: int = 5,
) -> List[Dict[str, Any]]:
    """Ad sets with 0 FTD and notable spend — candidates to close."""
    df_c = df[
        (df["Country Final"] == country)
        & (df["Account Alias"].fillna("").astype(str) == account_alias)
    ].copy()
    if df_c.empty:
        return []

    grouped = (
        df_c.groupby("Adset Name", dropna=False)
        .agg(
            Deposits=("# FTDs", "sum"),
            Leads=("# Leads", "sum"),
            Spend=("$ Spend", "sum"),
        )
        .reset_index()
    )
    zero_ftd = grouped[(grouped["Deposits"] == 0) & (grouped["Spend"] >= min_spend)]
    zero_ftd = zero_ftd.sort_values("Spend", ascending=False).head(top_n)

    return [
        {
            "adset_name": str(r.get("Adset Name") or "").strip(),
            "deposits": 0,
            "leads": _safe_int(r.get("Leads")),
            "spend": _safe_float(r.get("Spend")),
        }
        for _, r in zero_ftd.iterrows()
    ]


# ── Section 11: Open 2 campaigns ──────────────────────────────────────────────

def build_open_recommendations(
    top_combos: List[Dict[str, Any]],
    account_label: str,
    country_code: str,
    n: int = 2,
) -> List[Dict[str, Any]]:
    """Return up to n combos formatted as campaign open suggestions."""
    recs = []
    for combo in top_combos[:n]:
        offer_slug = (combo["offer"] or "").replace(" ", "")[:12]
        device = (combo["device"] or "all").upper()
        interest = (combo["interest"] or "Wide")
        creative = (combo["creative"] or "")[:20]
        age = combo.get("age", "35")

        campaign_name = f"{country_code}_{offer_slug}_{device}_{interest}_{age}"
        adset_name = (
            f"{combo['device']} + {interest} + {age} + {creative}"
        )
        recs.append(
            {
                "campaign_name": campaign_name,
                "adset_name": adset_name,
                "offer": combo["offer"],
                "category": combo["category"],
                "deposits": combo["deposits"],
                "roi": combo["roi"],
            }
        )
    return recs


# ── Country section builder ────────────────────────────────────────────────────

def build_country_section(
    df: pd.DataFrame,
    combos: pd.DataFrame,
    country: str,
    account_rows: List[Dict[str, Any]],
    my_cm: str,
    trend_days: int = 7,
) -> Dict[str, Any]:
    """Build all 11 sub-sections for one country."""
    top_combos = build_top_combos(combos, country)
    targeting = build_targeting_breakdown(df, country)
    creatives = build_creative_breakdown(df, country)

    # GSheet rows for this country: match by country code (col B) or by account labels
    # The country_code in GSheet (e.g. "MX") maps to Excel country names (e.g. "Mexico")
    # We use account alias matching via the accounts list
    country_accounts = account_rows  # already filtered to my CM; further filtered by caller

    close_recs: Dict[str, List] = {}
    open_recs: Dict[str, List] = {}
    for acc in country_accounts:
        alias = acc["alias"]
        if acc["is_ready"]:
            close_recs[acc["account_label"]] = build_close_recommendations(df, country, alias)
            cc = acc["country_code"] or country[:2].upper()
            open_recs[acc["account_label"]] = build_open_recommendations(
                top_combos, acc["account_label"], cc
            )

    return {
        "country": country,
        "cm_comparison": build_cm_comparison(df, country, my_cm),
        "trend": build_trend(df, country, trend_days),
        "top_combos": top_combos,
        "targeting": targeting,
        "creatives": creatives,
        "ai_context": build_ai_context_block(country, top_combos),
        "hypotheses": build_hypotheses_block(country, top_combos, targeting, creatives),
        "account_rows": country_accounts,
        "close_recs": close_recs,    # {account_label: [close_rec, ...]}
        "open_recs": open_recs,       # {account_label: [open_rec, ...]}
    }


# ── Main report builder ────────────────────────────────────────────────────────

def build_morning_report(
    excel_path: str,
    account_rows: List[Dict[str, Any]],
    *,
    my_cm: str = "Evgeni Sedin",
    trend_days: int = 7,
    country_filter: Optional[str] = None,
    top_combos_n: int = 5,
) -> Dict[str, Any]:
    """Load Excel and build the full MorningReport dict.

    Args:
        excel_path: path to stats Excel (cursor example.xlsx)
        account_rows: parsed rows from gsheet_accounts.fetch_account_rows()
        my_cm: CM name to highlight in section 1
        trend_days: window for section 2 trend
        country_filter: if set, only include this country (None = all)
        top_combos_n: max combos to show per country in section 3

    Returns:
        MorningReport dict with keys: date, source_excel, my_cm, countries[].
    """
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

    # Country name → ISO-2 code mapping (extend as needed)
    _COUNTRY_TO_CODE: Dict[str, str] = {
        "mexico": "mx", "guatemala": "gt", "brazil": "br", "chile": "cl",
        "colombia": "co", "peru": "pe", "argentina": "ar", "ecuador": "ec",
        "venezuela": "ve", "bolivia": "bo", "paraguay": "py", "uruguay": "uy",
        "bahrain": "bh", "uae": "uae", "united arab emirates": "uae",
        "qatar": "qa", "oman": "om", "kuwait": "kw", "saudi arabia": "sa",
        "nigeria": "ng", "kenya": "ke", "ghana": "gh", "south africa": "za",
        "albania": "al", "morocco": "ma", "egypt": "eg",
    }

    def _accounts_for_country(country: str) -> List[Dict[str, Any]]:
        """Find GSheet account rows relevant to this Excel country."""
        aliases_in_country = set(
            df[df["Country Final"] == country]["Account Alias"]
            .dropna().astype(str).str.strip().unique()
        )
        # Determine the expected GSheet country_code for this Excel country
        expected_code = _COUNTRY_TO_CODE.get(country.strip().lower(), "")
        matched = []
        for r in account_rows:
            alias_ok = r["alias"] in aliases_in_country
            code_ok = (
                not expected_code  # unknown country — skip code check
                or r.get("country_code", "").strip().lower() == expected_code
            )
            if alias_ok and code_ok:
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
