import argparse
import os
import re
import sys
from pathlib import Path
from typing import Optional, Tuple

# Ensure UTF-8 output so Cyrillic KB context doesn't crash on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# При прямом запуске скрипта из корня (python src/analysis_agent/analyze_fb_stats.py) нужен путь к проекту
_proj_root = Path(__file__).resolve().parents[2]
if str(_proj_root) not in sys.path:
    sys.path.insert(0, str(_proj_root))

import pandas as pd

from config.analysis_rules import (
    get_age_range,
    get_creative_skip_prefixes,
    get_device_keywords,
    get_text_segment_dictionary,
    get_text_segment_fallback,
)
from config.paths import get_excel_path
from src.knowledge_agent.knowledge_base import (
    format_target_context_line,
    resolve_target_code,
    log_unknown_target,
)

USE_LATEST_DATE_ONLY = False
_get_excel_path = get_excel_path


def normalize_text_segment(ad_title) -> str:
    """Классификация текста объявления: правила в config/analysis_reading_rules.json (ключ text_segments)."""
    # FIX A: guard against float/None input (NaN in Ad Title column)
    if ad_title is None or (isinstance(ad_title, float) and pd.isna(ad_title)):
        return get_text_segment_fallback()
    text = str(ad_title).lower()
    for segment, variants in get_text_segment_dictionary().items():
        for v in variants:
            if v in text:
                return segment.upper()
    return get_text_segment_fallback()


def parse_adset_name(
    adset_name: str,
    device_kw: dict,
    age_min: int,
    age_max: int,
    creative_prefixes: list,
) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    """Parse Adset Name to extract Country Code (display), Device, Interest, Age.

    Format: CountryCode_OfferName_Device_Creative[_CreativeNum]_Interest_Age[_Date]
    Country (full name) and Offer are taken from dedicated columns.
    Creative is taken from Ad Title column as-is.
    CountryCode (position 0) is extracted only for display purposes (e.g. MX, GCC, CL).

    Algorithm:
    1. Country code: parts[0] (2-3 letter code for display only).
    2. Find Device: first part matching a device keyword (e.g. "all", "ios").
    3. After Device, find Age: first numeric part in [age_min, age_max] (optionally with "+").
    4. Interest: non-creative, non-numeric parts between Device and Age.
       Creative parts = those starting with a creative prefix or being purely numeric.

    Returns: (country_code, device, interest, age)
    """
    if not isinstance(adset_name, str):
        return None, None, None, None

    parts = [p.strip() for p in adset_name.split("_") if p.strip()]

    # Country code is always the first token (2-3 letters like MX, CL, GCC, UAE)
    country_code: Optional[str] = parts[0].upper() if parts else None

    # Build a flat lookup: keyword → canonical label
    device_lookup: dict = {}
    for label, keywords in device_kw.items():
        for kw in keywords:
            device_lookup[kw.lower()] = label

    # Step 1: find Device
    device: Optional[str] = None
    device_idx: int = -1
    for i, p in enumerate(parts):
        canonical = device_lookup.get(p.lower())
        if canonical is not None:
            device = canonical
            device_idx = i
            break

    if device_idx == -1:
        # Fallback: no explicit Device keyword found — assume "all" and parse from
        # after the offer token (position 1), so creative/interest/age can still be extracted.
        device = "all"
        device_idx = 1  # skip CountryCode (0) and OfferName (1)

    remaining = parts[device_idx + 1:]

    # Step 2: find Age — first part whose digit content falls in [age_min, age_max].
    # Handles plain "25", "35+", and prefixed forms like "I35" or "A40".
    age: Optional[str] = None
    age_idx: int = -1
    for i, p in enumerate(remaining):
        if "." in p:
            continue
        p_digits = "".join(ch for ch in p if ch.isdigit())
        if p_digits:
            n = int(p_digits)
            if age_min <= n <= age_max:
                # Keep the original token as Age label (e.g. "35+"), normalise prefixed forms
                age = p_digits if not p.rstrip("+").isdigit() else p
                age_idx = i
                break

    # Step 3: find Interest — parts between creative section and Age
    parts_before_age = remaining[:age_idx] if age_idx != -1 else remaining

    interest_parts: list = []
    in_creative_phase = True
    for p in parts_before_age:
        p_lower = p.lower()
        # Skip date-like tokens anywhere (e.g. "28.03", "15.01.2024")
        if "." in p:
            continue
        if in_creative_phase:
            is_creative_part = (
                any(p_lower.startswith(pfx) for pfx in creative_prefixes)
                or p.isdigit()
            )
            if is_creative_part:
                continue
            in_creative_phase = False
        interest_parts.append(p)

    interest = "_".join(interest_parts) if interest_parts else None
    return country_code, device, interest, age


def compute_metrics(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    if USE_LATEST_DATE_ONLY and "Date" in df.columns:
        df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
        latest_date = df["Date"].max()
        if pd.notna(latest_date):
            df = df[df["Date"] == latest_date]

    df["# Leads"] = pd.to_numeric(df["# Leads"], errors="coerce").fillna(0)
    df["# FTDs"] = pd.to_numeric(df["# FTDs"], errors="coerce").fillna(0)
    df["$ Spend"] = pd.to_numeric(df["$ Spend"], errors="coerce").fillna(0.0)
    df["$ Net"] = pd.to_numeric(df["$ Net"], errors="coerce").fillna(0.0)

    df["CPL"] = df["$ Spend"] / df["# Leads"].replace(0, pd.NA)
    df["CPA"] = df["$ Spend"] / df["# FTDs"].replace(0, pd.NA)
    df["CR"] = (df["# FTDs"] / df["# Leads"].replace(0, pd.NA)) * 100.0
    df["Profit"] = df["$ Net"] - df["$ Spend"]
    df["ROI"] = df["$ Net"] / df["$ Spend"].replace(0, pd.NA)

    return df


def enrich_structure(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    # Read parsing config once (not per-row) for efficiency
    device_kw = get_device_keywords()
    age_min, age_max = get_age_range()
    creative_prefixes = get_creative_skip_prefixes()

    parsed = df["Adset Name"].apply(
        lambda x: parse_adset_name(x, device_kw, age_min, age_max, creative_prefixes)
    )
    df["Country Code"] = parsed.apply(lambda x: x[0])
    df["Device"] = parsed.apply(lambda x: x[1])
    df["Interest"] = parsed.apply(lambda x: x[2])
    df["Age"] = parsed.apply(lambda x: x[3])

    # Creative = full Ad Title as-is (includes creative identifier + ad copy text)
    df["Creative"] = df["Ad Title"].fillna("").astype(str).str.strip()

    df["TextSegment"] = df["Ad Title"].apply(normalize_text_segment)

    df["Country Final"] = df["Country"]

    # Offer is always taken from Ad Asset column
    df["Offer"] = df["Ad Asset"].fillna("").astype(str).str.strip()

    # Log any Interest values not found in Knowledge Base
    _log_unknown_interests(df)

    return df


def _log_unknown_interests(df: pd.DataFrame) -> None:
    """Проверить все уникальные значения Interest против KB и залогировать неизвестные."""
    if "Interest" not in df.columns or "Adset Name" not in df.columns:
        return
    skip_values = {"", "nan", "none", "wide"}
    unique_interests = (
        df["Interest"].dropna().astype(str).str.strip().unique()
    )
    for interest in unique_interests:
        if interest.lower() in skip_values:
            continue
        if resolve_target_code(interest) is None:
            # Find an example adset name for context
            example_rows = df[df["Interest"].astype(str).str.strip() == interest]["Adset Name"]
            source = str(example_rows.iloc[0]) if not example_rows.empty else ""
            log_unknown_target(interest, source=source)


def aggregate_combos(df: pd.DataFrame) -> pd.DataFrame:
    group_cols = [
        "Country Final",
        "Offer",
        "Device",
        "Interest",
        "Age",
        "Creative",
        "TextSegment",
    ]

    grouped = (
        df.groupby(group_cols, dropna=False)
        .agg(
            Deposits=("# FTDs", "sum"),
            Leads=("# Leads", "sum"),
            Spend=("$ Spend", "sum"),
            Net=("$ Net", "sum"),
        )
        .reset_index()
    )

    # Attach Country Code (short display code from Adset Name position 0, e.g. MX, CL, GCC)
    if "Country Code" in df.columns:
        code_map = (
            df[["Country Final", "Country Code"]]
            .dropna(subset=["Country Code"])
            .drop_duplicates("Country Final")
            .set_index("Country Final")["Country Code"]
        )
        grouped["Country Code"] = grouped["Country Final"].map(code_map)

    detail_group_cols = group_cols + ["Ad Title"]
    detail = (
        df.groupby(detail_group_cols, dropna=False)
        .agg(TitleDeposits=("# FTDs", "sum"))
        .reset_index()
    )

    if not detail.empty:
        idx = detail.groupby(group_cols)["TitleDeposits"].idxmax()
        top_titles = detail.loc[idx, group_cols + ["Ad Title", "TitleDeposits"]]
        top_titles = top_titles.rename(
            columns={"Ad Title": "TopAdTitle", "TitleDeposits": "TopAdTitleDeposits"}
        )
        grouped = grouped.merge(top_titles, on=group_cols, how="left")

    if "CM" in df.columns and "Account Alias" in df.columns:
        by_cm_acc = (
            df.groupby(group_cols + ["CM", "Account Alias"], dropna=False)
            .agg(FTDs=("# FTDs", "sum"))
            .reset_index()
        )
        if not by_cm_acc.empty:
            idx = by_cm_acc.groupby(group_cols)["FTDs"].idxmax()
            top_ca = by_cm_acc.loc[idx, group_cols + ["CM", "Account Alias"]].rename(
                columns={"CM": "TopCM", "Account Alias": "TopAccount"}
            ).reset_index(drop=True)

            def _row_key(r: pd.Series, cols: list) -> str:
                parts = []
                for c in cols:
                    v = r.get(c)
                    if v is None or (isinstance(v, float) and pd.isna(v)) or (isinstance(v, str) and str(v).strip().lower() == "nan"):
                        parts.append("__NA__")
                    else:
                        parts.append(str(v).strip())
                return "_".join(parts)

            grouped["_mk"] = grouped[group_cols].apply(lambda r: _row_key(r, group_cols), axis=1)
            top_ca["_mk"] = top_ca[group_cols].apply(lambda r: _row_key(r, group_cols), axis=1)
            grouped = grouped.merge(
                top_ca[["_mk", "TopCM", "TopAccount"]],
                on="_mk",
                how="left",
            ).drop(columns=["_mk"], errors="ignore")

    grouped["CPL"] = grouped["Spend"] / grouped["Leads"].replace(0, pd.NA)
    grouped["CPA"] = grouped["Spend"] / grouped["Deposits"].replace(0, pd.NA)
    grouped["CR"] = (grouped["Deposits"] / grouped["Leads"].replace(0, pd.NA)) * 100.0
    grouped["Profit"] = grouped["Net"] - grouped["Spend"]
    grouped["ROI"] = grouped["Net"] / grouped["Spend"].replace(0, pd.NA)

    grouped["Category"] = "Other"
    # STRONG: 2+ deposits, sorted by best CR
    mask_strong = grouped["Deposits"] >= 2
    # INTERESTING: 1 deposit + CR above 5%
    mask_interesting = (grouped["Deposits"] == 1) & (grouped["CR"] > 5.0)
    grouped.loc[mask_strong, "Category"] = "Strong"
    grouped.loc[mask_interesting, "Category"] = "Interesting"

    grouped = grouped[grouped["Category"].isin(["Strong", "Interesting"])]

    return grouped


def format_combo_row(row: pd.Series, with_context: bool = False) -> str:
    country = str(row.get("Country Final") or "").strip()
    country_code_raw = row.get("Country Code")
    country_code = (
        str(country_code_raw).strip()
        if country_code_raw and str(country_code_raw).strip().lower() not in ("", "nan")
        else country.upper()
    )
    device = str(row.get("Device") or "ALL").strip()
    interest = str(row.get("Interest") or "Wide").strip()
    age = str(row.get("Age") or "ALL").strip()
    creative = str(row.get("Creative") or "—").strip()
    text_segment = str(row.get("TextSegment") or "OTHER").strip()

    category = str(row.get("Category", "Strong"))
    category_label = "Strong" if category == "Strong" else "Interesting"

    line_header = f"{country} ({country_code})"
    combo_line = f"{country_code} - {device} + {interest} + {age} + {creative} | {text_segment}"

    deposits = int(row.get("Deposits") or 0)
    leads = int(row.get("Leads") or 0)
    cpl_val = row.get("CPL")
    cpl = float(cpl_val) if pd.notna(cpl_val) else 0.0
    cpa_val = row.get("CPA")
    cpa = float(cpa_val) if pd.notna(cpa_val) else 0.0
    cr_val = row.get("CR")
    cr = float(cr_val) if pd.notna(cr_val) else 0.0

    stats_line = f"Stats: Deposits: {deposits} | Leads: {leads} | CPL: ${cpl:.2f} | CPA: ${cpa:.2f} | CR: {cr:.2f}%"

    context_line = ""
    if with_context:
        ctx = format_target_context_line(interest)
        if ctx:
            context_line = f"\n{ctx}"

    return f"""{line_header}
[{category_label}]
{combo_line}
{stats_line}{context_line}
"""


def print_report(combos: pd.DataFrame, with_context: bool = False) -> None:
    if combos.empty:
        print("No qualifying combos found based on current rules.")
        return

    all_countries = sorted(
        {c for c in combos["Country Final"].fillna("").unique() if str(c).strip() != ""}
    )

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
            print(format_combo_row(row, with_context=with_context))


def _strip_date_from_display(s: str) -> str:
    """Убрать дату вида DD.MM.YYYY из строки для вывода (дата открытия кампании не показываем)."""
    if not s or not isinstance(s, str):
        return s
    s = re.sub(r"\s*\d{1,2}\.\d{1,2}\.\d{4}\s*", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def print_short_report(combos: pd.DataFrame, top_per_country: int = 2, df: Optional[pd.DataFrame] = None) -> None:
    """Короткий отчёт: топ N связок по каждой стране; проблемные — отдельной категорией с CM и аккаунтом."""
    if combos.empty:
        print("No qualifying combos found.")
        return
    countries = sorted(
        {c for c in combos["Country Final"].fillna("").unique() if str(c).strip() != ""}
    )
    col_country = 22
    col_offer = 22
    col_combo = 44
    col_type = 12
    col_dep = 8
    col_roi = 8
    rows = []
    for country in countries:
        country_df = combos[combos["Country Final"] == country].copy()
        strong = country_df[country_df["Category"] == "Strong"].copy()
        interesting = country_df[country_df["Category"] == "Interesting"].copy()
        strong = strong.sort_values(by=["Deposits", "CR", "Profit"], ascending=[False, False, False])
        interesting = interesting.sort_values(by=["CR", "Profit"], ascending=[False, False])
        combined = pd.concat([strong, interesting], ignore_index=True)
        top = combined.head(top_per_country)
        for i, (_, row) in enumerate(top.iterrows(), 1):
            offer = str(row.get("Offer") or "").strip() or "—"
            device = str(row.get("Device") or "ALL").strip()[:8]
            interest = _strip_date_from_display(str(row.get("Interest") or "Wide").strip())[:12]
            age = _strip_date_from_display(str(row.get("Age") or "ALL").strip())[:8]
            creative = str(row.get("Creative") or "—").strip()[:16]
            combo_str = f"{device} + {interest} + {age} + {creative}"
            cat = str(row.get("Category", ""))
            dep = int(row.get("Deposits") or 0)
            roi_val = row.get("ROI")
            roi_s = f"{roi_val:.2f}" if pd.notna(roi_val) else "—"
            rows.append((country[:col_country], str(i), offer[:col_offer], combo_str[:col_combo], cat[:col_type], str(dep), roi_s))
    if rows:
        sep = "+" + "-" * (col_country + 2) + "+" + "-" * 4 + "+" + "-" * (col_offer + 2) + "+" + "-" * (col_combo + 2) + "+" + "-" * (col_type + 2) + "+" + "-" * (col_dep + 2) + "+" + "-" * (col_roi + 2) + "+"
        fmt = "| {:<%d} | {:>2} | {:<%d} | {:<%d} | {:<%d} | {:>%d} | {:>%d} |" % (col_country, col_offer, col_combo, col_type, col_dep, col_roi)
        print("\n" + sep)
        print(fmt.format("Country", "#", "Offer", "Targeting + Creative", "Type", "Dep", "ROI"))
        print(sep)
        for r in rows:
            print(fmt.format(r[0], r[1], r[2], r[3], r[4], r[5], r[6]))
        print(sep)




def _combo_match_mask(df: pd.DataFrame, row: pd.Series, group_cols: list) -> pd.Series:
    """Маска строк df, совпадающих с комбо row по group_cols."""
    mask = pd.Series(True, index=df.index)
    for col in group_cols:
        if col not in df.columns:
            continue
        rv = row.get(col)
        if rv is None or (isinstance(rv, float) and pd.isna(rv)):
            mask &= df[col].isna()
        else:
            mask &= (df[col] == rv)
    return mask




def print_cm_breakdown_for_country(df: pd.DataFrame, country_code: str) -> None:
    df_country = df[df["Country Final"] == country_code].copy()
    if df_country.empty:
        print(f"No data for country {country_code}.")
        return

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
    cols = ["CM", "Deposits", "Leads", "Spend", "Net", "CPL", "CPA", "CR%", "ROI"]
    widths = [22, 9, 7, 12, 12, 8, 10, 7, 6]
    sep = "+" + "+".join("-" * w for w in widths) + "+"
    fmt = "|" + "|".join(f" {{:<{w}}} " for w in widths) + "|"

    print()
    print(f"CM breakdown for {country_display}:")
    print(sep)
    print(fmt.format(*cols))
    print(sep)

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
        print(
            fmt.format(
                cm_name,
                str(deposits),
                str(leads),
                f"${spend:,.2f}",
                f"${net:,.2f}",
                f"${cpl:.2f}",
                cpa_str,
                f"{cr:.2f}",
                f"{roi:.2f}",
            )
        )
    print(sep)


def print_offer_comparison_and_top_campaigns(
    df: pd.DataFrame, combos: pd.DataFrame, country_name: str, top_n: int = 2
) -> None:
    """Сравнение по офферам для страны и рекомендация топ-N кампаний на каждый оффер."""
    country_lower = str(country_name).strip().lower()
    df_country = df[df["Country Final"].astype(str).str.strip().str.lower() == country_lower]
    if df_country.empty:
        print(f"No data for country {country_name}.")
        return

    # Таблица по офферам
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

    cols = ["Offer", "Deposits", "Leads", "Spend", "Net", "CPL", "CPA", "CR%", "ROI"]
    widths = [18, 9, 7, 12, 12, 8, 10, 7, 6]
    sep = "+" + "+".join("-" * w for w in widths) + "+"
    fmt = "|" + "|".join(f" {{:<{w}}} " for w in widths) + "|"

    print()
    print(f"Offer comparison for {country_name}:")
    print(sep)
    print(fmt.format(*cols))
    print(sep)
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
        print(fmt.format(offer, str(deposits), str(leads), f"${spend:,.2f}", f"${net:,.2f}", cpl, cpa_str, cr, roi))
    print(sep)

    # Топ-N кампаний на каждый оффер (из combos)
    combos_country = combos[
        combos["Country Final"].astype(str).str.strip().str.lower() == country_lower
    ].copy()
    if combos_country.empty:
        print(f"No qualifying combos for {country_name} to recommend.")
        return

    combos_country["Offer"] = combos_country["Offer"].fillna("").astype(str).str.strip()
    offers_in_combos = sorted(combos_country["Offer"].unique())
    print()
    print(f"Recommendation: open top {top_n} campaigns per offer for {country_name}:")
    print("-" * 60)
    for offer in offers_in_combos:
        combo_offer = combos_country[combos_country["Offer"] == offer].copy()
        combo_offer = combo_offer.sort_values(
            by=["ROI", "Deposits", "CR"], ascending=[False, False, False]
        )
        top = combo_offer.head(top_n)
        if top.empty:
            continue
        offer_display = offer if offer else "—"
        print(f"\n  Offer: {offer_display}")
        for i, (_, row) in enumerate(top.iterrows(), 1):
            device = row.get("Device")
            device = "ALL" if device is None or (isinstance(device, float) and pd.isna(device)) else str(device).strip()
            interest = row.get("Interest")
            interest = "Wide" if interest is None or (isinstance(interest, float) and pd.isna(interest)) else str(interest).strip()
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
            print(f"    {i}. {device} + {interest} + {age} + {creative} + {text_seg}  |  Deposits: {deposits}  {roi_s}")


def main(  # noqa: PLR0913
    excel_path: Optional[str] = None,
    country_offer: str = "all",
    top_n: int = 2,
    output_path: Optional[str] = None,
    short_report: bool = False,
    with_context: bool = False,
    asset_filter: Optional[str] = None,
) -> None:
    excel_path = excel_path or _get_excel_path()
    if not os.path.exists(excel_path):
        raise FileNotFoundError(f"Excel file not found: {excel_path}")

    def run() -> None:
        df = pd.read_excel(excel_path)

        if asset_filter:
            mask = df["Ad Asset"].fillna("").astype(str).str.lower() == asset_filter.lower()
            df_filtered = df[mask]
            if df_filtered.empty:
                print(f"[!] No rows found for asset '{asset_filter}'. Check the name.")
                available = sorted(df["Ad Asset"].dropna().unique())
                print(f"    Available assets: {', '.join(available)}")
                return
            print(f"[filter] Asset='{asset_filter}': {len(df_filtered)} rows\n")
            df.__class__ = df_filtered.__class__
            df = df_filtered

        required_cols = [
            "Country",
            "Account Alias",
            "Adset Name",
            "Ad Title",
            "# Leads",
            "# FTDs",
            "$ Spend",
            "CM",
            "Ad Asset",
            "$ Net",
        ]
        missing = [c for c in required_cols if c not in df.columns]
        if missing:
            raise ValueError(f"Missing expected columns in Excel: {missing}")

        df = compute_metrics(df)
        df = enrich_structure(df)
        combos = aggregate_combos(df)
        if short_report:
            print_short_report(combos, top_per_country=top_n, df=df)
            return
        print_report(combos, with_context=with_context)

        all_countries = sorted(
            c for c in df["Country Final"].dropna().astype(str).unique()
            if c.strip() and c != "nan"
        )
        for c in all_countries:
            print_cm_breakdown_for_country(df, c)

        # Отчёт по офферам: по умолчанию для всех стран; при указании --country — только для неё
        if country_offer.strip().lower() == "all":
            for c in all_countries:
                print_offer_comparison_and_top_campaigns(df, combos, c, top_n=top_n)
        else:
            print_offer_comparison_and_top_campaigns(df, combos, country_offer, top_n=top_n)

    if output_path:
        with open(output_path, "w", encoding="utf-8") as f:
            old_stdout = sys.stdout
            sys.stdout = f
            try:
                run()
            finally:
                sys.stdout = old_stdout
        print(f"Report saved to {output_path}", file=sys.stderr)
    else:
        run()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="FB campaigns analysis: combos by country, CM breakdown, offer comparison."
    )
    parser.add_argument(
        "--excel", "-e",
        type=str,
        default=None,
        help="Path to Excel file (overrides FB_STATS_EXCEL env and default)",
    )
    parser.add_argument(
        "--country", "-c",
        type=str,
        default="all",
        help="Country for offer comparison and top campaigns, or 'all' for every country (default: all)",
    )
    parser.add_argument(
        "--top", "-t",
        type=int,
        default=2,
        help="Number of top campaigns per offer to recommend (default: 2)",
    )
    parser.add_argument(
        "--output", "-o",
        type=str,
        default=None,
        metavar="FILE",
        help="Save full report to FILE instead of printing to console",
    )
    parser.add_argument(
        "--short", "-s",
        action="store_true",
        help="Short report: top N combos per country only (N from --top, default 2)",
    )
    parser.add_argument(
        "--with-context",
        action="store_true",
        dest="with_context",
        help="Show target context from Knowledge Base below each combo stats line",
    )
    parser.add_argument(
        "--asset", "-a",
        type=str,
        default=None,
        help="Filter by Ad Asset name (e.g. 'Adnoc', 'Amazon'). Case-insensitive.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    main(
        excel_path=args.excel,
        country_offer=args.country,
        top_n=args.top,
        output_path=args.output,
        short_report=getattr(args, "short", False),
        with_context=getattr(args, "with_context", False),
        asset_filter=getattr(args, "asset", None),
    )
