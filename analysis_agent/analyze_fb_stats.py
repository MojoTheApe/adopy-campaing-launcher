def report():
    print("выдай мне отчет")

import os
from typing import Optional, Tuple

import pandas as pd


EXCEL_PATH_DEFAULT = r"C:\Users\EvgeniSedin\Desktop\cursor example.xlsx"
USE_LATEST_DATE_ONLY = False


TEXT_DICTIONARY = {
    "pro": ["pro", "pro_new", "professional", "pro_v2"],
    "reg": ["reg", "registration", "easy_reg"],
    "learn": ["learn", "learning", "learn_new", "education"],
}


def normalize_text_segment(ad_title: str) -> str:
    text = (ad_title or "").lower()
    for segment, variants in TEXT_DICTIONARY.items():
        for v in variants:
            if v in text:
                return segment.upper()
    return "OTHER"


def parse_adset_name(adset_name: str) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str], Optional[str]]:
    """
    Expected pattern (can be partially missing):
    0: Country (MX, CL, ...)
    1: Offer (MZ, AMZ, Amazon, ...)
    2: Device/OS (All, IOS, AND, DESKTOP, ...)
    3-4: Creative pattern, e.g. Images_134, Image 134, video17
    5: Interest (Luxury, Reading, Trading, FX, GMBL, Stockmarket, Wide, HNW, ...)
    6: Age (40, 40+, 25-40, ...)
    """
    if not isinstance(adset_name, str):
        return None, None, None, None, None

    parts = [p.strip() for p in adset_name.split("_") if p.strip()]
    country = parts[0] if len(parts) > 0 else None
    device = parts[2] if len(parts) > 2 else None

    creative_raw = ""
    if len(parts) > 4:
        creative_raw = f"{parts[3]}_{parts[4]}"
    elif len(parts) > 3:
        creative_raw = parts[3]

    creative_type = None
    creative_number = None
    if creative_raw:
        lower = creative_raw.lower().replace(" ", "")
        if "image" in lower or "images" in lower:
            creative_type = "IMAGE"
        elif "video" in lower:
            creative_type = "VIDEO"
        else:
            creative_type = "OTHER"
        digits = "".join(ch for ch in lower if ch.isdigit())
        creative_number = digits or None

    interest = parts[5] if len(parts) > 5 else None
    age = parts[6] if len(parts) > 6 else None

    creative_label = None
    if creative_type and creative_number:
        creative_label = f"{creative_type}#{creative_number}"
    elif creative_type:
        creative_label = creative_type

    return country, device, interest, age, creative_label


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

    parsed = df["Adset Name"].apply(parse_adset_name)
    df["Parsed Country"] = parsed.apply(lambda x: x[0])
    df["Device"] = parsed.apply(lambda x: x[1])
    df["Interest"] = parsed.apply(lambda x: x[2])
    df["Age"] = parsed.apply(lambda x: x[3])
    df["Creative"] = parsed.apply(lambda x: x[4])

    def fill_creative_from_asset(row: pd.Series) -> str:
        if row.get("Creative"):
            return row["Creative"]

        ad_title = str(row.get("Ad Title") or "")
        if not ad_title:
            return row.get("Creative")

        joined = ad_title.lower().replace(" ", "")

        creative_type = None
        if "image" in joined or "images" in joined:
            creative_type = "IMAGE"
        elif "video" in joined:
            creative_type = "VIDEO"
        else:
            creative_type = "OTHER"

        digits = "".join(ch for ch in joined if ch.isdigit())
        creative_number = digits or None

        if creative_type and creative_number:
            return f"{creative_type}#{creative_number}"
        return creative_type

    df["Creative"] = df.apply(fill_creative_from_asset, axis=1)

    df["TextSegment"] = df["Ad Title"].apply(normalize_text_segment)

    df["Country Final"] = df["Country"]

    return df


def aggregate_combos(df: pd.DataFrame) -> pd.DataFrame:
    group_cols = [
        "Country Final",
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

    grouped["CPL"] = grouped["Spend"] / grouped["Leads"].replace(0, pd.NA)
    grouped["CPA"] = grouped["Spend"] / grouped["Deposits"].replace(0, pd.NA)
    grouped["CR"] = (grouped["Deposits"] / grouped["Leads"].replace(0, pd.NA)) * 100.0
    grouped["Profit"] = grouped["Net"] - grouped["Spend"]
    grouped["ROI"] = grouped["Net"] / grouped["Spend"].replace(0, pd.NA)

    grouped["Category"] = "Other"
    mask_strong = (grouped["Deposits"] >= 2) & (grouped["ROI"] > 1.0)
    mask_interesting = (grouped["Deposits"] == 1) & (grouped["CR"] > 5.0) & (grouped["ROI"] > 1.0)
    grouped.loc[mask_strong, "Category"] = "Strong"
    grouped.loc[mask_interesting, "Category"] = "Interesting"

    grouped = grouped[grouped["Category"].isin(["Strong", "Interesting"])]

    return grouped


def format_combo_row(row: pd.Series) -> str:
    country = str(row.get("Country Final") or "").upper()
    device = row.get("Device") or "ALL"
    interest = row.get("Interest") or "Wide"
    age = row.get("Age") or "ALL"
    creative = row.get("Creative") or "CREATIVE"
    text_segment = row.get("TextSegment") or "TEXT"

    line_header = f"{country} ({country})"
    category = row.get("Category", "Strong")
    category_label = "Strong" if category == "Strong" else "Interesting"

    combo_line = f"{country} - {device} + {interest} + {age} + {creative} + {text_segment}"

    deposits = int(row.get("Deposits") or 0)
    leads = int(row.get("Leads") or 0)
    cpl = float(row.get("CPL") or 0.0)
    cpa_val = row.get("CPA")
    cpa = float(cpa_val) if pd.notna(cpa_val) else 0.0
    cr = float(row.get("CR") or 0.0)

    stats_line = f"Stats: Deposits: {deposits} | Leads: {leads} | CPL: ${cpl:.2f} | CPA: ${cpa:.2f} | CR: {cr:.2f}%"

    extra_line = ""
    if "#" not in str(creative):
        top_title = row.get("TopAdTitle")
        top_title_deps = row.get("TopAdTitleDeposits")
        if isinstance(top_title, str) and top_title.strip():
            deps_from_title = int(top_title_deps or 0)
            extra_line = f"\nAd Title: {top_title} | Deposits from this title: {deps_from_title}"

    return f"""{line_header}
[{category_label}]
{combo_line}
{stats_line}{extra_line}
"""


def print_report(combos: pd.DataFrame) -> None:
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
            print(format_combo_row(row))


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


def main() -> None:
    excel_path = EXCEL_PATH_DEFAULT
    if not os.path.exists(excel_path):
        raise FileNotFoundError(f"Excel file not found: {excel_path}")

    df = pd.read_excel(excel_path)

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
    print_report(combos)

    all_countries = sorted(
        c for c in df["Country Final"].dropna().astype(str).unique()
        if c.strip() and c != "nan"
    )
    for country in all_countries:
        print_cm_breakdown_for_country(df, country)


if __name__ == "__main__":
    main()

