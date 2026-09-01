#!/usr/bin/env python3
"""
Agent 8: Build upload Excel from plan.
Input: plan (CSV or Excel) — account, country, asset, os, creative, targeting, age, creative_number, date, copy.
Output: one .xlsx with one sheet per account, columns by name (same as Upluad FB campaign sheets).
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import sys
from pathlib import Path
from typing import Any

# Project root
_proj_root = Path(__file__).resolve().parents[2]
if str(_proj_root) not in sys.path:
    sys.path.insert(0, str(_proj_root))

from openpyxl import Workbook, load_workbook
from openpyxl.utils import get_column_letter

# Default template sheet for header order (same structure across campaign sheets)
TEMPLATE_SHEET = "Upload all"
SOURCE_SHEETS = ("Accounts", "Ad copy", "Countries", "Interests", "Video file name", "Image FIle name", "LP's")

# Column names we care about (by name, not letter)
COL_PIXEL = "Pixel"
COL_CAMPAIGN_NAME_COUNTRY = "Campaign name - country"
COL_ASSET = "Asset"
COL_OS = "OS"
COL_ICONS = "Icons"
COL_CREATIVE = "Creative"
COL_TARGETING = "Targeting"
COL_AGE = "Age"
COL_CREATIVE_NUMBER = "Creative number"
COL_DATE = "Date"
COL_COPY = "Copy"
COL_CAMPAIGN_NAME = "Campaign Name"
COL_LINK = "Link"
COL_COUNTRIES = "Countries"
COL_AGE_MIN = "Age Min"
COL_AGE_MAX = "Age Max"
COL_LOCALES = "Locales"
COL_IMAGE_FILE_NAME = "Image File name"
COL_VIDEO_FILE_NAME = "Video File name"
COL_AD_SET_NAME = "Ad Set Name"

# Default values for all upload columns (so every template column gets a value when possible)
DEFAULT_UPLOAD_VALUES = {
    "Campaign Status": "ACTIVE",
    "Campaign Objective": "OUTCOME_LEADS",
    "Buying Type": "AUCTION",
    "Campaign Daily Budget": "",
    "Campaign Bid Strategy": "LOWEST_COST_WITHOUT_CAP",
    "Bid amount": "",
    "Campaign Start Time": "",
    "New Objective": "",
    "Buy With Prime Type": "",
    "Is Budget Scheduling Enabled For Campaign": "",
    "Campaign High Demand Periods": "",
    "Buy With Integration Partner": "",
    "Ad Set ID": "",
    "Ad Set Run Status": "ACTIVE",
    "Ad Set Lifetime Impressions": "",
    "Ad Set Time Start": "",
    "Destination Type": "WEBSITE",
    "Use Accelerated Delivery": "",
    "Is Budget Scheduling Enabled For Ad Set": "",
    "Ad Set High Demand Periods": "",
    "Link Object ID": "",
    "Optimized Conversion Tracking Pixels 1": "",
    "Optimized Conversion Tracking Pixels": "",
    "Optimized Event": "LEAD",
    "Cities": "",
    "Location Types": "home",
    "Excluded Countries": "",
    "Gender": "all",
}

# Plan CSV columns (required)
PLAN_ACCOUNT = "account"
PLAN_COUNTRY = "country"
PLAN_ASSET = "asset"
PLAN_OS = "os"
PLAN_CREATIVE = "creative"
PLAN_TARGETING = "targeting"
PLAN_AGE = "age"
PLAN_CREATIVE_NUMBER = "creative_number"
PLAN_DATE = "date"
PLAN_COPY = "copy"

PLAN_COLUMNS = (PLAN_ACCOUNT, PLAN_COUNTRY, PLAN_ASSET, PLAN_OS, PLAN_CREATIVE, PLAN_TARGETING, PLAN_AGE, PLAN_CREATIVE_NUMBER, PLAN_DATE, PLAN_COPY)


def _plan_key_to_header(key: str) -> str | None:
    """Convert plan CSV column name to template header (e.g. campaign_status -> Campaign Status)."""
    if not key or not str(key).strip():
        return None
    return str(key).strip().replace("_", " ").title()


def _header_to_plan_key(header: str) -> str:
    """Convert template header to plan-like key (e.g. Campaign Status -> campaign_status)."""
    if not header:
        return ""
    return str(header).strip().lower().replace(" ", "_").replace("-", "_")


def _col_index_by_header(ws, header_name: str, row: int = 1) -> int | None:
    """Return 1-based column index where row has header_name, or None."""
    for col in range(1, ws.max_column + 1):
        val = ws.cell(row=row, column=col).value
        if val is None:
            continue
        if str(val).strip().lower() == header_name.strip().lower():
            return col
    return None


def _headers_from_sheet(ws) -> list[tuple[int, str]]:
    """Return list of (1-based col index, header string) for row 1. Skip empty."""
    out = []
    for col in range(1, ws.max_column + 1):
        val = ws.cell(row=1, column=col).value
        if val is not None and str(val).strip():
            out.append((col, str(val).strip()))
    return out


def load_reference_data(excel_path: str):
    """
    Load Accounts, Ad copy, Countries, Interests, Video file name, Image FIle name from Excel.
    Also get template headers from TEMPLATE_SHEET.
    Returns dict with keys: accounts, ad_copy, countries, interests, video_files, image_files, template_headers, template_sheet.
    """
    if not os.path.isfile(excel_path):
        raise FileNotFoundError(f"Excel not found: {excel_path}")
    wb = load_workbook(excel_path, read_only=False, data_only=True)
    data = {}

    # Accounts: Account -> {domain, pixel, link, ad_account_id}
    if "Accounts" in wb.sheetnames:
        ws = wb["Accounts"]
        account_col    = _col_index_by_header(ws, "Account")
        domain_col     = _col_index_by_header(ws, "Domain")
        pixel_col      = _col_index_by_header(ws, "Pixel")
        link_col       = _col_index_by_header(ws, "Link")
        act_id_col     = _col_index_by_header(ws, "Ad account ID")
        accounts = {}
        for r in range(2, ws.max_row + 1):
            acc = ws.cell(row=r, column=account_col).value if account_col else None
            if acc is None or not str(acc).strip():
                continue
            key = str(acc).strip()
            raw_act = ws.cell(row=r, column=act_id_col).value if act_id_col else None
            act_id = str(raw_act).strip() if raw_act is not None else None
            # Normalise: store without act_ prefix so consumers can add it themselves
            if act_id and act_id.lower().startswith("act_"):
                act_id = act_id[4:]
            accounts[key] = {
                "domain":         ws.cell(row=r, column=domain_col).value if domain_col else None,
                "pixel":          ws.cell(row=r, column=pixel_col).value  if pixel_col  else None,
                "link":           ws.cell(row=r, column=link_col).value   if link_col   else None,
                "ad_account_id":  act_id or None,
            }
        data["accounts"] = accounts
    else:
        data["accounts"] = {}

    # Ad copy: by Ad copy name -> {title, body, link_description}
    if "Ad copy" in wb.sheetnames:
        ws = wb["Ad copy"]
        name_col = _col_index_by_header(ws, "Ad copy name")
        title_col = _col_index_by_header(ws, "Title")
        body_col = _col_index_by_header(ws, "Body")
        link_desc_col = _col_index_by_header(ws, "Link Description")
        ad_copy = {}
        for r in range(2, ws.max_row + 1):
            name = ws.cell(row=r, column=name_col).value if name_col else None
            if name is None or not str(name).strip():
                continue
            key = str(name).strip()
            ad_copy[key] = {
                "title": ws.cell(row=r, column=title_col).value if title_col else None,
                "body": ws.cell(row=r, column=body_col).value if body_col else None,
                "link_description": ws.cell(row=r, column=link_desc_col).value if link_desc_col else None,
            }
        data["ad_copy"] = ad_copy
    else:
        data["ad_copy"] = {}

    # Countries: first column (country code) -> {code_b, language, value_d}
    if "Countries" in wb.sheetnames:
        ws = wb["Countries"]
        countries = {}
        for r in range(1, ws.max_row + 1):
            code_a = ws.cell(row=r, column=1).value
            if code_a is None or not str(code_a).strip():
                continue
            code_a = str(code_a).strip()
            countries[code_a.upper()] = {
                "code_b": ws.cell(row=r, column=2).value,
                "language": ws.cell(row=r, column=3).value,
                "value_d": ws.cell(row=r, column=4).value,
            }
        data["countries"] = countries
    else:
        data["countries"] = {}

    # Interests: Targeting name -> JSON string (column B)
    if "Interests" in wb.sheetnames:
        ws = wb["Interests"]
        interests = {}
        for r in range(2, ws.max_row + 1):
            name = ws.cell(row=r, column=1).value
            json_val = ws.cell(row=r, column=2).value
            if name is not None and str(name).strip():
                interests[str(name).strip()] = json_val
        data["interests"] = interests
    else:
        data["interests"] = {}

    # Video file name: column A (number/id) -> column B (file name)
    if "Video file name" in wb.sheetnames:
        ws = wb["Video file name"]
        video_files = {}
        for r in range(2, min(ws.max_row + 1, 5000)):
            a = ws.cell(row=r, column=1).value
            b = ws.cell(row=r, column=2).value
            if a is not None and b is not None:
                video_files[str(a).strip()] = str(b).strip()
        data["video_files"] = video_files
    else:
        data["video_files"] = {}

    # Image FIle name: column A -> column B (file name)
    if "Image FIle name" in wb.sheetnames:
        ws = wb["Image FIle name"]
        image_files = {}
        for r in range(2, ws.max_row + 1):
            a = ws.cell(row=r, column=1).value
            b = ws.cell(row=r, column=2).value
            if a is not None and b is not None:
                image_files[str(a).strip()] = str(b).strip()
        data["image_files"] = image_files
    else:
        data["image_files"] = {}

    # Template headers from Upload all (or first campaign-like sheet)
    template_headers = []
    if TEMPLATE_SHEET in wb.sheetnames:
        template_headers = _headers_from_sheet(wb[TEMPLATE_SHEET])
    else:
        for name in ("EC Favorita", "CO Cib", "Upload all"):
            if name in wb.sheetnames:
                template_headers = _headers_from_sheet(wb[name])
                break
    data["template_headers"] = template_headers
    wb.close()
    return data


def get_account_info(ref: dict, account_name: str) -> dict[str, Any]:
    """Get pixel, domain, link for account. account_name can be partial match."""
    accounts = ref.get("accounts") or {}
    name = (account_name or "").strip()
    if name in accounts:
        return accounts[name]
    for key, val in accounts.items():
        if name.upper() in key.upper():
            return val
    # Geo relabels of one profile ("FacebookELf221 EC" vs the sheet's
    # "FacebookElf221 AR Ait") share pixel/domain/tracker — match on the
    # first name token, but ONLY when exactly one row carries it. Sheets
    # like UL58 have several same-prefix rows with different links; guessing
    # between those would silently attach the wrong tracker, so ambiguity
    # keeps the old not-found behaviour. (User-confirmed case, 2026-07-31.)
    token = name.split()[0].upper() if name.split() else ""
    if token:
        hits = [val for key, val in accounts.items()
                if (key or "").upper().split()[:1] == [token]]
        if len(hits) == 1:
            return hits[0]
    # Nothing in the workbook: ask this machine's account map before giving up.
    #
    # The workbook stays the shared source of truth and wins wherever it has a
    # row. But an account connected on the dashboard exists only in the map
    # until someone edits the workbook by hand, and until then it was unusable:
    # no tracking link, no pixel, and the only symptom was a refusal to save
    # the link — long after the account looked fully configured.
    return _account_info_from_machine_map(name)


def _account_info_from_machine_map(account_name: str) -> dict[str, Any]:
    """Tracking link / pixel / domain from the machine's own account map."""
    empty = {"pixel": None, "domain": None, "link": None}
    try:
        from config.paths import get_ad_account_map_path
        from src.meta_campaign_agent.ad_account_map import (
            load_ad_account_map,
            resolve_account_entry,
        )

        entry = resolve_account_entry(
            load_ad_account_map(get_ad_account_map_path()), account_name
        )
    except Exception:
        return empty
    if entry is None:
        return empty
    return {
        "pixel": getattr(entry, "pixel_id", None),
        "domain": getattr(entry, "domain", None),
        "link": getattr(entry, "tracking_link", None),
    }


def build_campaign_name(country: str, asset: str, os: str, creative: str, creative_number: str, targeting: str, age: str, date: str = "") -> str:
    """Campaign Name = Country_Asset_OS_Creative_CreativeNumber_Targeting_Age.

    Правило для Asset: не оставляем пробелы внутри названия бренда/оффера,
    т.е. "Qatar Energy" -> "QatarEnergy". Дату не включаем — проставляется в Excel по своей закономерности.
    """
    asset_clean = re.sub(r"\s+", "", asset or "")
    parts = [country or "", asset_clean or "", os or "All", creative or "Image", creative_number or "", targeting or "", age or "35"]
    return "_".join(str(p).strip() or "_" for p in parts).rstrip("_")


def lookup_ad_copy(ref: dict, copy_key: str) -> dict[str, Any]:
    """Return title, body, link_description for Copy (ad copy name). Match by start or exact."""
    ad_copy = ref.get("ad_copy") or {}
    key = (copy_key or "").strip()
    if not key:
        return {"title": "", "body": "", "link_description": ""}
    if key in ad_copy:
        return ad_copy[key]
    for name, val in ad_copy.items():
        if key in name or name in key:
            return val
    return {"title": key, "body": "", "link_description": ""}


def lookup_interest(ref: dict, targeting: str) -> str:
    """Return Interests JSON string for targeting name."""
    interests = ref.get("interests") or {}
    t = (targeting or "").strip()
    if t in interests:
        return interests[t] or ""
    for name, json_val in interests.items():
        if t in name or name in t:
            return json_val or ""
    return ""


def lookup_country(ref: dict, country_code: str) -> dict[str, Any]:
    """Return code_b, language for Countries (Locales)."""
    countries = ref.get("countries") or {}
    c = (country_code or "").strip().upper()
    if c in countries:
        return countries[c]
    return {"code_b": c, "language": "", "value_d": None}


def lookup_creative_file(ref: dict, creative_type: str, creative_number: str) -> str:
    """Return file name for Video or Image by creative number (column L)."""
    num = (creative_number or "").strip()
    if not num:
        return ""
    is_video = (creative_type or "").strip().lower() == "video"
    if is_video:
        return (ref.get("video_files") or {}).get(num, "")
    return (ref.get("image_files") or {}).get(num, "")


def _header_lookup(header_names: list[str], constant_name: str) -> str | None:
    """Return the actual header from template that matches constant_name (case-insensitive)."""
    want = (constant_name or "").strip().lower()
    if not want:
        return None
    for h in header_names:
        if (h or "").strip().lower() == want:
            return h
    return None


def build_row(combo: dict, account_info: dict, ref: dict, header_names: list[str]) -> dict[str, Any]:
    """
    Build one campaign row as dict[header_name, value].
    combo: plan row (account, country, asset, ... + any extra columns matching template headers).
    All template columns are filled: from plan, from ref/account, or from DEFAULT_UPLOAD_VALUES.
    """
    country = (combo.get(PLAN_COUNTRY) or "").strip()
    asset = (combo.get(PLAN_ASSET) or "").strip()
    os_val = (combo.get(PLAN_OS) or "All").strip()
    creative = (combo.get(PLAN_CREATIVE) or "Image").strip()
    targeting = (combo.get(PLAN_TARGETING) or "").strip()
    age = (combo.get(PLAN_AGE) or "35").strip()
    creative_number = (combo.get(PLAN_CREATIVE_NUMBER) or "").strip()
    date_val = (combo.get(PLAN_DATE) or "").strip()
    copy_key = (combo.get(PLAN_COPY) or "").strip()

    override_name = (combo.get("campaign_name") or combo.get("ad_set_name") or "").strip()
    if override_name:
        campaign_name = override_name
    else:
        campaign_name = build_campaign_name(
            country, asset, os_val, creative, creative_number, targeting, age, date_val
        )
    ad_copy = lookup_ad_copy(ref, copy_key)
    country_info = lookup_country(ref, country)
    creative_filename = lookup_creative_file(ref, creative, creative_number)

    row = {}
    for h in header_names:
        row[h] = None

    # 1) From plan: any combo key that matches a template header (e.g. campaign_status -> Campaign Status)
    for key, val in combo.items():
        if val is None or (isinstance(val, str) and not val.strip()):
            continue
        h_from_key = _plan_key_to_header(key)
        if h_from_key:
            h_matched = _header_lookup(header_names, h_from_key)
            if h_matched is not None:
                row[h_matched] = val.strip() if isinstance(val, str) else val
        key_norm = key.lower().replace("-", " ").replace("_", " ")
        for h in header_names:
            if _header_to_plan_key(h).replace("_", " ") == key_norm or (h or "").strip().lower() == (key or "").strip().lower():
                row[h] = val.strip() if isinstance(val, str) else val
                break

    # 2) Computed / from ref and account (override plan). Use _header_lookup so template column names match.
    def set_cell(col_name: str, value: Any) -> None:
        h = _header_lookup(header_names, col_name)
        if h is not None:
            row[h] = value

    set_cell(COL_PIXEL, account_info.get("pixel"))
    set_cell(COL_CAMPAIGN_NAME_COUNTRY, country)
    set_cell(COL_ASSET, asset)
    set_cell(COL_OS, os_val)
    if _header_lookup(header_names, COL_ICONS):
        set_cell(COL_ICONS, "_")
    set_cell(COL_CREATIVE, creative)
    set_cell(COL_TARGETING, targeting)
    set_cell(COL_AGE, age)
    set_cell(COL_CREATIVE_NUMBER, creative_number)
    set_cell(COL_DATE, date_val)
    set_cell(COL_COPY, copy_key or ad_copy.get("title") or "")
    set_cell(COL_CAMPAIGN_NAME, campaign_name)
    if _header_lookup(header_names, COL_AD_SET_NAME):
        set_cell(COL_AD_SET_NAME, campaign_name)
    set_cell(COL_LINK, account_info.get("link"))
    set_cell(COL_COUNTRIES, country_info.get("code_b") or country)
    try:
        age_int = int(age) if age else 35
        set_cell(COL_AGE_MIN, age_int)
        set_cell(COL_AGE_MAX, age_int)
    except ValueError:
        set_cell(COL_AGE_MIN, 35)
        set_cell(COL_AGE_MAX, 35)
    set_cell(COL_LOCALES, country_info.get("language") or "")
    if _header_lookup(header_names, COL_VIDEO_FILE_NAME):
        set_cell(COL_VIDEO_FILE_NAME, creative_filename if (creative or "").strip().lower() == "video" else "")
    if _header_lookup(header_names, COL_IMAGE_FILE_NAME):
        set_cell(COL_IMAGE_FILE_NAME, creative_filename if (creative or "").strip().lower() == "image" else "")

    # 3) Defaults for any header still empty (match by case-insensitive header name)
    for h in header_names:
        if row.get(h) is None or (isinstance(row.get(h), str) and not (row[h] or "").strip()):
            for def_key, def_val in DEFAULT_UPLOAD_VALUES.items():
                if (def_key or "").strip().lower() == (h or "").strip().lower():
                    row[h] = def_val
                    break

    return row


def parse_plan_csv(csv_path: str) -> list[dict]:
    """Parse plan CSV; return list of combo dicts. All CSV columns are kept (for extra upload columns)."""
    rows = []
    with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        for r in reader:
            combo = {}
            for k in fieldnames:
                val = r.get(k)
                if val is not None and isinstance(val, str):
                    val = val.strip()
                combo[k] = val or ""
            if combo.get(PLAN_ACCOUNT) or combo.get("account"):
                rows.append(combo)
    return rows


def group_plan_by_account(combos: list[dict]) -> dict[str, list[dict]]:
    """Group combos by account name."""
    by_account = {}
    for c in combos:
        acc = (c.get(PLAN_ACCOUNT) or "").strip()
        if not acc:
            continue
        if acc not in by_account:
            by_account[acc] = []
        by_account[acc].append(c)
    return by_account


def build_sheets(ref: dict, plan_by_account: dict[str, list[dict]]) -> dict[str, list[dict]]:
    """For each account, build list of row dicts (header_name -> value)."""
    header_names = [h[1] for h in ref["template_headers"]]
    if not header_names:
        header_names = [COL_PIXEL, COL_CAMPAIGN_NAME_COUNTRY, COL_ASSET, COL_OS, COL_CREATIVE, COL_TARGETING, COL_AGE, COL_CREATIVE_NUMBER, COL_DATE, COL_COPY, COL_CAMPAIGN_NAME, COL_LINK, COL_COUNTRIES, COL_AGE_MIN, COL_AGE_MAX, COL_LOCALES]
    result = {}
    for account, combos in plan_by_account.items():
        account_info = get_account_info(ref, account)
        rows = []
        for combo in combos:
            row = build_row(combo, account_info, ref, header_names)
            rows.append(row)
        result[account] = rows
    return result


def sheet_name_safe(name: str, max_len: int = 31) -> str:
    """Excel sheet names: max 31 chars, no []:*?/\\"""
    s = re.sub(r'[\[\]:*?/\\]', "_", str(name or "Sheet"))
    return s[:max_len] if len(s) > max_len else s or "Sheet"


def write_upload_excel(sheets_data: dict[str, list[dict]], ref: dict, output_path: str) -> None:
    """Write one .xlsx: one sheet per account, headers from ref['template_headers']."""
    header_names = [h[1] for h in ref["template_headers"]]
    if not header_names:
        header_names = [COL_PIXEL, COL_CAMPAIGN_NAME_COUNTRY, COL_ASSET, COL_OS, COL_CREATIVE, COL_TARGETING, COL_AGE, COL_CREATIVE_NUMBER, COL_DATE, COL_COPY, COL_CAMPAIGN_NAME, COL_LINK, COL_COUNTRIES, COL_AGE_MIN, COL_AGE_MAX, COL_LOCALES]
    wb = Workbook()
    if "Sheet" in wb.sheetnames:
        del wb["Sheet"]
    for account, rows in sheets_data.items():
        ws = wb.create_sheet(title=sheet_name_safe(account))
        for col_idx, header in enumerate(header_names, start=1):
            ws.cell(row=1, column=col_idx, value=header)
        for row_idx, row_dict in enumerate(rows, start=2):
            for col_idx, header in enumerate(header_names, start=1):
                val = row_dict.get(header)
                ws.cell(row=row_idx, column=col_idx, value=val if val is not None else "")
    wb.save(output_path)


def build_upload_plan_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Agent 8: Build upload Excel from plan CSV")
    parser.add_argument(
        "plan",
        nargs="?",
        help="Path to plan CSV (columns: account, country, asset, os, creative, targeting, age, creative_number, date, copy)",
    )
    parser.add_argument("-o", "--output", default=None, help="Output .xlsx path (default: output/upload_plan.xlsx)")
    parser.add_argument("-e", "--excel", default=None, help="Path to Upluad FB.xlsx (default: config)")
    return parser


def run_upload_plan_from_args(args: argparse.Namespace) -> int:
    from config.paths import get_upload_fb_excel_path

    excel_path = (args.excel or "").strip() or get_upload_fb_excel_path()
    output_path = (args.output or "").strip() or str(_proj_root / "output" / "upload_plan.xlsx")
    if not args.plan:
        print("Usage: python -m src.upload_agent.build_upload_excel <plan.csv> [-o output.xlsx] [-e Upluad FB.xlsx]")
        print("Plan CSV columns:", ", ".join(PLAN_COLUMNS))
        return 1
    plan_path = args.plan
    if not os.path.isfile(plan_path):
        print(f"Plan file not found: {plan_path}")
        return 1
    print("Loading reference data from", excel_path)
    ref = load_reference_data(excel_path)
    print("Parsing plan", plan_path)
    combos = parse_plan_csv(plan_path)
    if not combos:
        print("No rows in plan (need 'account' column)")
        return 1
    by_account = group_plan_by_account(combos)
    print("Building sheets for", len(by_account), "account(s)")
    sheets_data = build_sheets(ref, by_account)
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    write_upload_excel(sheets_data, ref, output_path)
    print("Written:", output_path)
    return 0


def main(argv: list[str] | None = None) -> None:
    args = build_upload_plan_argument_parser().parse_args(argv)
    code = run_upload_plan_from_args(args)
    if code != 0:
        sys.exit(code)


if __name__ == "__main__":
    main()
