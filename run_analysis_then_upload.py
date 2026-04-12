#!/usr/bin/env python3
"""
Variant 2: single entrypoint — run Agent 1 (analysis), then Agent 8 (upload Excel).
1) Load stats Excel, compute combos (Strong/Interesting), take top N per country.
2) Convert combos to plan rows (account, country, asset, targeting, creative, ...).
3) Build upload Excel via Agent 8 (one sheet per account).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_root = Path(__file__).resolve().parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))
os.chdir(_root)

import pandas as pd

from config.paths import get_excel_path, get_upload_fb_excel_path
from src.analysis_agent.analyze_fb_stats import (
    aggregate_combos,
    compute_metrics,
    enrich_structure,
)
from src.upload_agent.build_upload_excel import (
    PLAN_ACCOUNT,
    PLAN_AGE,
    PLAN_ASSET,
    PLAN_COPY,
    PLAN_COUNTRY,
    PLAN_CREATIVE,
    PLAN_CREATIVE_NUMBER,
    PLAN_DATE,
    PLAN_OS,
    PLAN_TARGETING,
    build_sheets,
    load_reference_data,
    write_upload_excel,
)


def _parse_creative_from_ad_title(ad_title: str) -> tuple[str, str]:
    """Креатив и номер из названия объявления (правило: брать с названия объявления)."""
    if not ad_title or not isinstance(ad_title, str):
        return "Image", ""
    s = ad_title.strip().lower().replace(" ", "")
    creative_type = "Image"
    if "video" in s:
        creative_type = "Video"
    elif "image" in s or "images" in s:
        creative_type = "Image"
    digits = "".join(c for c in s if c.isdigit())
    return creative_type, digits


def combo_row_to_plan(combo: pd.Series, account: str) -> dict:
    """Convert one combo (analysis row) to plan row for Agent 8. Креатив и ad copy — из названия объявления (TopAdTitle)."""
    country = str(combo.get("Country Final") or "").strip()
    offer = str(combo.get("Offer") or "").strip()
    device = str(combo.get("Device") or "ALL").strip()
    interest = str(combo.get("Interest") or "Wide").strip()
    age = str(combo.get("Age") or "35").strip()
    top_title = combo.get("TopAdTitle")
    copy_val = str(top_title).strip() if isinstance(top_title, str) and top_title else ""

    # Креатив и копи — из названия объявления (TopAdTitle)
    if isinstance(top_title, str) and top_title.strip():
        creative_type, creative_number = _parse_creative_from_ad_title(top_title)
    else:
        creative_raw = str(combo.get("Creative") or "").strip()
        creative_type = "Image"
        creative_number = ""
        if creative_raw:
            u = creative_raw.upper()
            if "VIDEO" in u:
                creative_type = "Video"
            elif "IMAGE" in u:
                creative_type = "Image"
            if "#" in creative_raw:
                try:
                    creative_number = creative_raw.split("#")[-1].strip()
                except Exception:
                    pass
            else:
                digits = "".join(c for c in creative_raw if c.isdigit())
                if digits:
                    creative_number = digits

    # Дата не подставляем — в Excel проставляется по своей закономерности
    return {
        PLAN_ACCOUNT: account,
        PLAN_COUNTRY: country,
        PLAN_ASSET: offer,
        PLAN_OS: device if device in ("All", "ALL", "IOS", "Android") else "All",
        PLAN_CREATIVE: creative_type,
        PLAN_TARGETING: interest,
        PLAN_AGE: age if age and age != "ALL" else "35",
        PLAN_CREATIVE_NUMBER: creative_number,
        PLAN_DATE: "",
        PLAN_COPY: copy_val,
    }


def run(
    stats_excel: str,
    upload_excel: str,
    account: str,
    output_path: str,
    country_filter: str = "all",
    asset_filter: str = "",
    top_n: int = 10,
    save_plan_csv: str | None = None,
) -> None:
    # 1) Agent 1 pipeline: load stats, combos
    if not os.path.isfile(stats_excel):
        raise FileNotFoundError(f"Stats Excel not found: {stats_excel}")
    df = pd.read_excel(stats_excel)
    required = ["Country", "Account Alias", "Adset Name", "Ad Title", "# Leads", "# FTDs", "$ Spend", "CM", "Ad Asset", "$ Net"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"Stats Excel missing columns: {missing}")

    df = compute_metrics(df)
    df = enrich_structure(df)
    combos = aggregate_combos(df)
    if combos.empty:
        print("No Strong/Interesting combos found. Upload Excel will be empty.", file=sys.stderr)
        plan_rows = []
    else:
        if country_filter and country_filter.strip().lower() != "all":
            # Filter by value from Country column (Country Final = df["Country"])
            want = country_filter.strip()
            combos_filtered = combos[
                combos["Country Final"].astype(str).str.strip().str.lower() == want.lower()
            ]
            if len(combos_filtered) == 0:
                print(f"No combos for country '{want}'. Using top combos from all countries.", file=sys.stderr)
            else:
                combos = combos_filtered
        if asset_filter and asset_filter.strip():
            sub = asset_filter.strip()
            combos_af = combos[
                combos["Offer"].astype(str).str.contains(sub, case=False, na=False)
            ]
            if len(combos_af) == 0:
                print(
                    f"No combos for offer matching '{sub}'. Using combos without offer filter.",
                    file=sys.stderr,
                )
            else:
                combos = combos_af
        combos = combos.sort_values(
            by=["Category", "Deposits", "ROI"],
            ascending=[True, False, False],
        )
        # Strong first (Category Strong < Interesting in sort), then by Deposits
        combos["_order"] = combos["Category"].map({"Strong": 0, "Interesting": 1}).fillna(2)
        combos = combos.sort_values(by=["_order", "Deposits"], ascending=[True, False]).drop(columns=["_order"], errors="ignore")
        combos = combos.head(top_n)
        plan_rows = [combo_row_to_plan(combo, account) for _, combo in combos.iterrows()]

    if save_plan_csv and plan_rows:
        import csv
        keys = list(plan_rows[0].keys())
        with open(save_plan_csv, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=keys)
            w.writeheader()
            w.writerows(plan_rows)
        print(f"Plan CSV saved: {save_plan_csv}", file=sys.stderr)

    # 2) Agent 8: build upload Excel
    if not os.path.isfile(upload_excel):
        raise FileNotFoundError(f"Upload FB Excel not found: {upload_excel}")
    ref = load_reference_data(upload_excel)
    plan_by_account = {account: plan_rows}
    sheets_data = build_sheets(ref, plan_by_account)
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    write_upload_excel(sheets_data, ref, output_path)
    print(f"Upload Excel written: {output_path} ({len(plan_rows)} rows)", file=sys.stderr)


def build_bridge_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run analysis (Agent 1) then build upload Excel (Agent 8) from combos.",
    )
    parser.add_argument("--excel", "-e", default=None, help="Path to stats Excel (default: FB_STATS_EXCEL / config)")
    parser.add_argument("--upload-excel", "-u", default=None, help="Path to Upluad FB.xlsx (default: config)")
    parser.add_argument("--account", "-a", required=True, help="Account name for upload (e.g. FacebookUL574 EC Fav)")
    parser.add_argument("--output", "-o", default=None, help="Output upload Excel path (default: output/upload_from_analysis.xlsx)")
    parser.add_argument("--country", "-c", default="all", help="Filter combos by country name (e.g. Chile) (default: all)")
    parser.add_argument(
        "--asset",
        default="",
        help="Filter combos by substring in Offer / Ad Asset (e.g. Mercado Libre)",
    )
    parser.add_argument("--top", "-t", type=int, default=10, help="Max combos to include (default: 10)")
    parser.add_argument("--plan-csv", default=None, help="Optional: save intermediate plan CSV")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_bridge_argument_parser().parse_args(argv)

    stats_excel = (args.excel or "").strip() or get_excel_path()
    upload_excel = (args.upload_excel or "").strip() or get_upload_fb_excel_path()
    output_path = (args.output or "").strip() or str(_root / "output" / "upload_from_analysis.xlsx")

    run(
        stats_excel=stats_excel,
        upload_excel=upload_excel,
        account=args.account.strip(),
        output_path=output_path,
        country_filter=args.country or "all",
        asset_filter=(args.asset or "").strip(),
        top_n=max(1, args.top),
        save_plan_csv=(args.plan_csv or "").strip() or None,
    )


if __name__ == "__main__":
    main()
