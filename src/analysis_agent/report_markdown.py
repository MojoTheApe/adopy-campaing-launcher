"""Build FB analytics report as a single Markdown document (full and short modes).

Full (non-short) reports use **structure v2**: one block per country in reading order
Account → CM → Combos → Offer comparison (see REPORT_CONTRACT.md).
"""
from __future__ import annotations

from typing import List, Optional

import pandas as pd

from src.analysis_agent.report_model import format_combo_row, strip_date_from_display

REPORT_STRUCTURE_VERSION = "2"


def _md_escape_cell(s: str) -> str:
    return str(s).replace("|", "/").replace("\n", " ")


def _md_table(headers: List[str], rows: List[List[str]]) -> str:
    if not rows:
        return ""
    h = "| " + " | ".join(_md_escape_cell(x) for x in headers) + " |"
    sep = "| " + " | ".join("---" for _ in headers) + " |"
    body_lines = [
        "| " + " | ".join(_md_escape_cell(x) for x in row) + " |" for row in rows
    ]
    return "\n".join([h, sep, *body_lines])


def render_report_overview_md(
    df: pd.DataFrame,
    combos: pd.DataFrame,
    *,
    source_path: Optional[str] = None,
) -> str:
    """Reading order and file stats (report structure v2)."""
    countries = sorted(
        c
        for c in df["Country Final"].dropna().astype(str).unique()
        if c.strip() and c.lower() != "nan"
    )
    lines = [
        "## How to read this report",
        "",
        "For each **Country** section below, the order is:",
        "",
        "1. **Account breakdown** — performance on your accounts in that geo.",
        "2. **CM breakdown** — how campaign managers compare in that geo.",
        "3. **Combos** — Strong / Interesting targeting+creative bundles (with TopAccount / TopCM where available).",
        "4. **Offer comparison** — offers in that geo and **Recommendation**: top campaigns per offer.",
        "",
        f"- **Report structure version:** `{REPORT_STRUCTURE_VERSION}`",
    ]
    if source_path:
        lines.append(f"- **Source workbook:** `{source_path}`")
    lines.append(
        f"- **Data rows:** {len(df)}  |  **Countries:** {len(countries)}  |  **Qualifying combos:** {len(combos)}"
    )
    lines.append("")
    return "\n".join(lines)


def render_combo_report_md_for_country(
    combos: pd.DataFrame, country: str, with_context: bool = False, max_rows: int = 3
) -> str:
    """Strong/Interesting combos for a single country (markdown subsection)."""
    header = f"### Combos (Strong / Interesting) — {country}"
    country_df = combos[combos["Country Final"] == country].copy()
    if country_df.empty:
        return f"{header}\n\nNo qualifying combos for this country.\n"

    strong = country_df[country_df["Category"] == "Strong"].copy()
    interesting = country_df[country_df["Category"] == "Interesting"].copy()
    strong = strong.sort_values(
        by=["Deposits", "CR", "Profit"],
        ascending=[False, False, False],
    )
    interesting = interesting.sort_values(
        by=["CR", "Profit"],
        ascending=[False, False],
    )
    combined = pd.concat([strong, interesting], ignore_index=True)
    top_n = combined.head(max_rows)
    if top_n.empty:
        return f"{header}\n\nNo qualifying combos for this country.\n"

    blocks = [format_combo_row(row, with_context=with_context) for _, row in top_n.iterrows()]
    return header + "\n\n" + "\n\n".join(blocks) + "\n"


def render_combo_report_md(combos: pd.DataFrame, with_context: bool = False) -> str:
    """Legacy single section: all countries, top 3 combos each (flat list)."""
    lines = ["## Combos (Strong / Interesting)", ""]
    if combos.empty:
        lines.append("No qualifying combos found based on current rules.")
        return "\n".join(lines)

    all_countries = sorted(
        {c for c in combos["Country Final"].fillna("").unique() if str(c).strip() != ""}
    )
    blocks: List[str] = []
    for country in all_countries:
        country_df = combos[combos["Country Final"] == country].copy()
        strong = country_df[country_df["Category"] == "Strong"].copy()
        interesting = country_df[country_df["Category"] == "Interesting"].copy()
        strong = strong.sort_values(
            by=["Deposits", "CR", "Profit"],
            ascending=[False, False, False],
        )
        interesting = interesting.sort_values(
            by=["CR", "Profit"],
            ascending=[False, False],
        )
        combined = pd.concat([strong, interesting], ignore_index=True)
        top_n = combined.head(3)
        if top_n.empty:
            continue
        for _, row in top_n.iterrows():
            blocks.append(format_combo_row(row, with_context=with_context))
    if not blocks:
        lines.append("No qualifying combos found based on current rules.")
        return "\n".join(lines)
    lines.append("\n\n".join(blocks))
    return "\n".join(lines)


def render_short_report_md(
    combos: pd.DataFrame, top_per_country: int = 2, df: Optional[pd.DataFrame] = None
) -> str:
    _ = df  # reserved for future (problem rows / CM hints)
    lines = ["## Short report: top combos by country", ""]
    if combos.empty:
        lines.append("No qualifying combos found.")
        return "\n".join(lines)
    countries = sorted(
        {c for c in combos["Country Final"].fillna("").unique() if str(c).strip() != ""}
    )
    rows_data: List[List[str]] = []
    for country in countries:
        country_df = combos[combos["Country Final"] == country].copy()
        strong = country_df[country_df["Category"] == "Strong"].copy()
        interesting = country_df[country_df["Category"] == "Interesting"].copy()
        strong = strong.sort_values(
            by=["Deposits", "CR", "Profit"], ascending=[False, False, False]
        )
        interesting = interesting.sort_values(by=["CR", "Profit"], ascending=[False, False])
        combined = pd.concat([strong, interesting], ignore_index=True)
        top = combined.head(top_per_country)
        for i, (_, row) in enumerate(top.iterrows(), 1):
            offer = str(row.get("Offer") or "").strip() or "—"
            device = str(row.get("Device") or "ALL").strip()[:8]
            interest = strip_date_from_display(str(row.get("Interest") or "Wide").strip())[:12]
            age = strip_date_from_display(str(row.get("Age") or "ALL").strip())[:8]
            creative = str(row.get("Creative") or "—").strip()[:16]
            combo_str = f"{device} + {interest} + {age} + {creative}"
            cat = str(row.get("Category", ""))
            dep = int(row.get("Deposits") or 0)
            roi_val = row.get("ROI")
            roi_s = f"{roi_val:.2f}" if pd.notna(roi_val) else "—"
            rows_data.append(
                [country, str(i), offer, combo_str, cat, str(dep), roi_s]
            )
    headers = ["Country", "#", "Offer", "Targeting + Creative", "Type", "Dep", "ROI"]
    lines.append(_md_table(headers, rows_data))
    return "\n".join(lines)


def render_cm_breakdown_md(df: pd.DataFrame, country_code: str) -> str:
    df_country = df[df["Country Final"] == country_code].copy()
    if df_country.empty:
        return f"## CM breakdown — {country_code}\n\nNo data for country {country_code}.\n"
    grouped = (
        df_country.groupby("CM", dropna=False)
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
    grouped["CR"] = (grouped["Deposits"] / grouped["Leads"].replace(0, pd.NA)) * 100.0
    grouped["ROI"] = grouped["Net"] / grouped["Spend"].replace(0, pd.NA)
    grouped = grouped.sort_values(by=["Net", "Deposits"], ascending=[False, False])
    country_display = "".join(c for c in str(country_code) if ord(c) < 128).strip() or "Unknown"
    rows: List[List[str]] = []
    for _, row in grouped.iterrows():
        cm_name = str(row.get("CM") or "UNKNOWN_CM")[:22]
        deposits = int(row.get("Deposits") or 0)
        leads = int(row.get("Leads") or 0)
        spend = float(row.get("Spend") or 0.0)
        net = float(row.get("Net") or 0.0)
        cpl_val = row.get("CPL")
        cpl = float(cpl_val) if pd.notna(cpl_val) else 0.0
        cpa_val = row.get("CPA")
        cpa_str = f"${float(cpa_val):.2f}" if pd.notna(cpa_val) else "-"
        cr_val = row.get("CR")
        cr = float(cr_val) if pd.notna(cr_val) else 0.0
        roi_val = row.get("ROI")
        roi = float(roi_val) if pd.notna(roi_val) else 0.0
        rows.append(
            [
                cm_name,
                str(deposits),
                str(leads),
                f"${spend:,.2f}",
                f"${net:,.2f}",
                f"${cpl:.2f}",
                cpa_str,
                f"{cr:.2f}",
                f"{roi:.2f}",
            ]
        )
    headers = ["CM", "Deposits", "Leads", "Spend", "Net", "CPL", "CPA", "CR%", "ROI"]
    tbl = _md_table(headers, rows)
    return f"## CM breakdown — {country_display}\n\n{tbl}\n"


def render_account_breakdown_md(df: pd.DataFrame, country_code: str) -> str:
    df_country = df[df["Country Final"] == country_code].copy()
    if df_country.empty:
        return ""
    grouped = (
        df_country.groupby("Account Alias", dropna=False)
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
    grouped["CR"] = (grouped["Deposits"] / grouped["Leads"].replace(0, pd.NA)) * 100.0
    grouped["ROI"] = grouped["Net"] / grouped["Spend"].replace(0, pd.NA)
    grouped = grouped.sort_values(by=["Net", "Deposits"], ascending=[False, False])
    country_display = "".join(c for c in str(country_code) if ord(c) < 128).strip() or "Unknown"
    rows: List[List[str]] = []
    for _, row in grouped.iterrows():
        acc_name = str(row.get("Account Alias") or "UNKNOWN_ACCOUNT")[:28]
        deposits = int(row.get("Deposits") or 0)
        leads = int(row.get("Leads") or 0)
        spend = float(row.get("Spend") or 0.0)
        net = float(row.get("Net") or 0.0)
        cpl_val = row.get("CPL")
        cpl = float(cpl_val) if pd.notna(cpl_val) else 0.0
        cpa_val = row.get("CPA")
        cpa_str = f"${float(cpa_val):.2f}" if pd.notna(cpa_val) else "-"
        cr_val = row.get("CR")
        cr = float(cr_val) if pd.notna(cr_val) else 0.0
        roi_val = row.get("ROI")
        roi = float(roi_val) if pd.notna(roi_val) else 0.0
        rows.append(
            [
                acc_name,
                str(deposits),
                str(leads),
                f"${spend:,.2f}",
                f"${net:,.2f}",
                f"${cpl:.2f}",
                cpa_str,
                f"{cr:.2f}",
                f"{roi:.2f}",
            ]
        )
    headers = [
        "Account Alias",
        "Deposits",
        "Leads",
        "Spend",
        "Net",
        "CPL",
        "CPA",
        "CR%",
        "ROI",
    ]
    tbl = _md_table(headers, rows)
    return f"## Account breakdown — {country_display}\n\n{tbl}\n"


def render_offer_comparison_md(
    df: pd.DataFrame, combos: pd.DataFrame, country_name: str, top_n: int = 2
) -> str:
    country_lower = str(country_name).strip().lower()
    df_country = df[df["Country Final"].astype(str).str.strip().str.lower() == country_lower]
    lines: List[str] = [
        f"## Offer comparison — {country_name}",
        "",
    ]
    if df_country.empty:
        lines.append(f"No data for country {country_name}.")
        return "\n".join(lines)
    by_offer = (
        df_country.groupby("Offer", dropna=False)
        .agg(
            Deposits=("# FTDs", "sum"),
            Leads=("# Leads", "sum"),
            Spend=("$ Spend", "sum"),
            Net=("$ Net", "sum"),
        )
        .reset_index()
    )
    by_offer["Offer"] = by_offer["Offer"].fillna("").astype(str).str.strip()
    by_offer["CPL"] = by_offer["Spend"] / by_offer["Leads"].replace(0, pd.NA)
    by_offer["CPA"] = by_offer["Spend"] / by_offer["Deposits"].replace(0, pd.NA)
    by_offer["CR"] = (by_offer["Deposits"] / by_offer["Leads"].replace(0, pd.NA)) * 100.0
    by_offer["ROI"] = by_offer["Net"] / by_offer["Spend"].replace(0, pd.NA)
    by_offer = by_offer.sort_values(by=["Net", "Deposits"], ascending=[False, False])
    offer_rows: List[List[str]] = []
    for _, row in by_offer.iterrows():
        offer = (str(row.get("Offer") or "").strip() or "—")[:18]
        deposits = int(row.get("Deposits") or 0)
        leads = int(row.get("Leads") or 0)
        spend = float(row.get("Spend") or 0.0)
        net = float(row.get("Net") or 0.0)
        cpl_val = row.get("CPL")
        cpl = f"${float(cpl_val):.2f}" if pd.notna(cpl_val) else "-"
        cpa_val = row.get("CPA")
        cpa_str = f"${float(cpa_val):.2f}" if pd.notna(cpa_val) else "-"
        cr_val = row.get("CR")
        cr = f"{float(cr_val):.2f}" if pd.notna(cr_val) else "-"
        roi_val = row.get("ROI")
        roi = f"{float(roi_val):.2f}" if pd.notna(roi_val) else "-"
        offer_rows.append(
            [offer, str(deposits), str(leads), f"${spend:,.2f}", f"${net:,.2f}", cpl, cpa_str, cr, roi]
        )
    headers = ["Offer", "Deposits", "Leads", "Spend", "Net", "CPL", "CPA", "CR%", "ROI"]
    lines.append(_md_table(headers, offer_rows))
    lines.append("")

    combos_country = combos[
        combos["Country Final"].astype(str).str.strip().str.lower() == country_lower
    ].copy()
    if combos_country.empty:
        lines.append(f"No qualifying combos for {country_name} to recommend.")
        return "\n".join(lines)

    combos_country["Offer"] = combos_country["Offer"].fillna("").astype(str).str.strip()
    offers_in_combos = sorted(combos_country["Offer"].unique())
    lines.append(f"### Recommendation: top {top_n} campaigns per offer")
    lines.append("")
    for offer in offers_in_combos:
        combo_offer = combos_country[combos_country["Offer"] == offer].copy()
        combo_offer = combo_offer.sort_values(
            by=["ROI", "Deposits", "CR"], ascending=[False, False, False]
        )
        top = combo_offer.head(top_n)
        if top.empty:
            continue
        offer_display = offer if offer else "—"
        lines.append(f"- **Offer:** {offer_display}")
        for i, (_, row) in enumerate(top.iterrows(), 1):
            device = row.get("Device")
            device = "ALL" if device is None or (isinstance(device, float) and pd.isna(device)) else str(device).strip()
            interest = row.get("Interest")
            interest = (
                "Wide"
                if interest is None or (isinstance(interest, float) and pd.isna(interest))
                else str(interest).strip()
            )
            age = row.get("Age")
            age = "ALL" if age is None or (isinstance(age, float) and pd.isna(age)) else str(age).strip()
            creative = row.get("Creative") or "—"
            if isinstance(creative, float) and pd.isna(creative):
                creative = "—"
            creative = str(creative).strip()
            text_seg = row.get("TextSegment") or "—"
            if isinstance(text_seg, float) and pd.isna(text_seg):
                text_seg = "—"
            text_seg = str(text_seg).strip()
            roi_val = row.get("ROI")
            roi_s = f"ROI {roi_val:.2f}" if pd.notna(roi_val) else ""
            deposits = int(row.get("Deposits") or 0)
            top_acc = row.get("TopAccount")
            top_acc_s = (
                str(top_acc).strip()[:28]
                if top_acc is not None and not (isinstance(top_acc, float) and pd.isna(top_acc))
                else "—"
            )
            top_cm = row.get("TopCM")
            top_cm_s = (
                str(top_cm).strip()[:22]
                if top_cm is not None and not (isinstance(top_cm, float) and pd.isna(top_cm))
                else "—"
            )
            lines.append(
                f"  {i}. {device} + {interest} + {age} + {creative} + {text_seg}  |  "
                f"Deposits: {deposits}  {roi_s}  |  TopAccount: {top_acc_s}  TopCM: {top_cm_s}"
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def build_full_report_markdown(
    df: pd.DataFrame,
    combos: pd.DataFrame,
    *,
    country_offer: str = "all",
    top_n: int = 2,
    short_report: bool = False,
    with_context: bool = False,
    account_substrings: Optional[List[str]] = None,
    filter_preamble: str = "",
    source_excel_path: Optional[str] = None,
) -> str:
    """Assemble the full Markdown report.

    Non-short full reports use **v2 layout**: overview, then per-country blocks
    (Account → CM → Combos → Offer/recommendation). Legacy flat combo+CM loop
    is replaced so the document matches a country-first morning read.
    """
    parts: List[str] = ["# FB campaigns analytics report", ""]
    if filter_preamble.strip():
        parts.append(filter_preamble.strip())
        parts.append("")
    if short_report:
        parts.append(render_short_report_md(combos, top_per_country=top_n, df=df))
        return "\n".join(parts).rstrip() + "\n"

    parts.append(
        render_report_overview_md(df, combos, source_path=source_excel_path)
    )
    parts.append("")

    all_countries = sorted(
        c for c in df["Country Final"].dropna().astype(str).unique()
        if c.strip() and c != "nan"
    )
    country_filter = country_offer.strip().lower()
    if country_filter != "all":
        all_countries = [c for c in all_countries if c.strip().lower() == country_filter]

    if not all_countries and country_filter != "all":
        parts.append(f"## Country filter: {country_offer}\n\nNo `Country` values match this label in the workbook.\n")
        parts.append("")
        parts.append(render_offer_comparison_md(df, combos, country_offer, top_n=top_n))
        return "\n".join(parts).rstrip() + "\n"

    for c in all_countries:
        parts.append(f"## Country: {c}")
        parts.append("")
        acc_block = render_account_breakdown_md(df, c)
        if acc_block.strip():
            parts.append(acc_block)
        else:
            parts.append(
                f"### Account breakdown — {c}\n\n"
                "_No per-account rows in this geo (check filters or raw data)._"
                "\n"
            )
        parts.append("")
        parts.append(render_cm_breakdown_md(df, c))
        parts.append("")
        if combos.empty:
            parts.append(
                "### Combos (Strong / Interesting)\n\n"
                "_No rows passed Strong/Interesting filters for this export._\n"
            )
        else:
            parts.append(
                render_combo_report_md_for_country(
                    combos, c, with_context=with_context, max_rows=3
                )
            )
        parts.append("")
        parts.append(render_offer_comparison_md(df, combos, c, top_n=top_n))
        parts.append("")

    return "\n".join(parts).rstrip() + "\n"
