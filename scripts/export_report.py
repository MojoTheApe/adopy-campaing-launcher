"""
Export a short management Excel report from the morning dashboard data.
Output: output/management_report_<date>.xlsx
"""
import os, sys
from datetime import datetime
from pathlib import Path
import sys

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pandas as pd
from config.paths import load_env

load_env()

try:
    import openpyxl
    from openpyxl import Workbook
    from openpyxl.styles import (
        Alignment, Border, Font, PatternFill, Side
    )
    from openpyxl.utils import get_column_letter
except ImportError:
    print("Installing openpyxl...")
    import subprocess
    subprocess.run([sys.executable, "-m", "pip", "install", "openpyxl"], check=True)
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

# ── Config ─────────────────────────────────────────────────────────────────────
EXCEL_SRC = os.environ.get("FB_STATS_EXCEL", "").strip() or r"C:\Users\EvgeniSedin\Desktop\cursor example.xlsx"
OUT_DIR   = Path(__file__).parent.parent / "output"
OUT_DIR.mkdir(exist_ok=True)
TODAY     = datetime.now().strftime("%Y-%m-%d")
OUT_PATH  = OUT_DIR / f"management_report_{TODAY}.xlsx"

# ── Colors ──────────────────────────────────────────────────────────────────────
C_HEADER    = "1F2D3D"   # dark navy
C_SUBHEADER = "2E4057"   # mid navy
C_ACCENT    = "1A8FE3"   # blue accent
C_GOOD      = "1E8449"   # green
C_WARN      = "D4AC0D"   # gold
C_BAD       = "C0392B"   # red
C_ALT       = "F0F4F8"   # light stripe
C_WHITE     = "FFFFFF"
C_TITLE_FG  = "FFFFFF"

thin = Side(style="thin", color="CCCCCC")
border = Border(left=thin, right=thin, top=thin, bottom=thin)

def hfill(hex_color: str) -> PatternFill:
    return PatternFill("solid", fgColor=hex_color)

def bold_font(color=C_TITLE_FG, size=10) -> Font:
    return Font(bold=True, color=color, size=size)

def num_fmt(ws, row, col, value, fmt="#,##0", fill=None):
    c = ws.cell(row=row, column=col, value=value)
    c.number_format = fmt
    c.alignment = Alignment(horizontal="right")
    c.border = border
    if fill:
        c.fill = fill
    return c

def str_cell(ws, row, col, value, bold=False, fill=None, align="left", color="000000"):
    c = ws.cell(row=row, column=col, value=value)
    c.font = Font(bold=bold, color=color, size=10)
    c.alignment = Alignment(horizontal=align, vertical="center")
    c.border = border
    if fill:
        c.fill = fill
    return c

def header_row(ws, row, values, fill_color=C_HEADER):
    fill = hfill(fill_color)
    for col, val in enumerate(values, 1):
        c = ws.cell(row=row, column=col, value=val)
        c.font   = bold_font(C_TITLE_FG, 10)
        c.fill   = fill
        c.border = border
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws.row_dimensions[row].height = 30

# ── Load data ───────────────────────────────────────────────────────────────────
print(f"Reading {EXCEL_SRC} ...")
from src.analysis_agent.stats_workbook import read_stats_workbook
df = read_stats_workbook(
    EXCEL_SRC,
    usecols=["Country", "Account Alias", "CM", "Adset Name",
             "Ad Title", "# Leads", "# FTDs", "$ Spend", "$ Net"]
)
df["Country"]  = df["Country"].astype(str).str.strip()
df["CM"]       = df["CM"].astype(str).str.strip()

# Extract Interest from Adset Name (part between first two underscores group, after device token)
def _extract_interest(adset: str) -> str:
    try:
        parts = str(adset).split("_")
        # typical pattern: CC_INTEREST_DEVICE_... or CC_DEVICE_INTEREST_...
        # return everything between position 1 and last 3-4 tokens as Interest approximation
        if len(parts) >= 3:
            return parts[1]
        return adset
    except Exception:
        return ""

df["Interest"] = df["Adset Name"].apply(_extract_interest)

# ── Build aggregations ──────────────────────────────────────────────────────────

# 1. By country
by_country = (
    df[df["Account Alias"].notna()]
    .groupby("Country", dropna=False)
    .agg(
        Accounts =("Account Alias", "nunique"),
        Leads    =("# Leads",       "sum"),
        Deposits =("# FTDs",        "sum"),
        Spend    =("$ Spend",       "sum"),
        Net      =("$ Net",         "sum"),
    )
    .reset_index()
)
by_country["CPA"] = by_country["Spend"] / by_country["Deposits"].replace(0, pd.NA)
by_country["ROI"] = by_country["Net"]   / by_country["Spend"].replace(0, pd.NA)
by_country["AvgDep"] = by_country["Deposits"] / by_country["Accounts"].replace(0, pd.NA)
by_country = by_country[by_country["Country"].str.len() > 0]
by_country = by_country.sort_values("Deposits", ascending=False)

# 2. By CM
by_cm = (
    df.groupby("CM", dropna=False)
    .agg(
        Leads    =("# Leads",  "sum"),
        Deposits =("# FTDs",   "sum"),
        Spend    =("$ Spend",  "sum"),
        Net      =("$ Net",    "sum"),
    )
    .reset_index()
)
by_cm["CPA"] = by_cm["Spend"] / by_cm["Deposits"].replace(0, pd.NA)
by_cm["ROI"] = by_cm["Net"]   / by_cm["Spend"].replace(0, pd.NA)
by_cm = by_cm[by_cm["CM"].str.len() > 0]
by_cm = by_cm.sort_values("Deposits", ascending=False)

# 3. Top creative/interest combos (deposits > 0)
by_combo = (
    df.groupby(["Country", "Interest", "Ad Title"], dropna=False)
    .agg(
        Deposits =("# FTDs",  "sum"),
        Leads    =("# Leads", "sum"),
        Spend    =("$ Spend", "sum"),
    )
    .reset_index()
)
by_combo["CPA"] = by_combo["Spend"] / by_combo["Deposits"].replace(0, pd.NA)
by_combo = by_combo[by_combo["Deposits"] > 0]
by_combo = by_combo.sort_values("Deposits", ascending=False).head(30)

# ── Create workbook ─────────────────────────────────────────────────────────────
wb = Workbook()

# ════════════════════════════════════════════════════════════════════════════════
# SHEET 1: Summary by Country
# ════════════════════════════════════════════════════════════════════════════════
ws = wb.active
ws.title = "By Country"

# Title
ws.merge_cells("A1:H1")
tc = ws["A1"]
tc.value = f"Management Report  ·  {TODAY}"
tc.font  = Font(bold=True, size=14, color=C_TITLE_FG)
tc.fill  = hfill(C_HEADER)
tc.alignment = Alignment(horizontal="center", vertical="center")
ws.row_dimensions[1].height = 36

ws.merge_cells("A2:H2")
sc = ws["A2"]
sc.value = "Performance by Country"
sc.font  = bold_font("AAAAAA", 9)
sc.fill  = hfill("2A3542")
sc.alignment = Alignment(horizontal="left", vertical="center")
ws.row_dimensions[2].height = 18

header_row(ws, 3, ["Country", "Accounts", "Leads", "Deposits", "Avg dep/acc", "Spend", "Net", "CPA"])

totals = {
    "Accounts": 0, "Leads": 0, "Deposits": 0,
    "Spend": 0.0, "Net": 0.0
}

for i, (_, r) in enumerate(by_country.iterrows()):
    row = 4 + i
    fill = hfill(C_ALT) if i % 2 == 0 else None
    deps = int(r["Deposits"]) if pd.notna(r["Deposits"]) else 0
    leads = int(r["Leads"]) if pd.notna(r["Leads"]) else 0
    acc   = int(r["Accounts"]) if pd.notna(r["Accounts"]) else 0
    spend = float(r["Spend"]) if pd.notna(r["Spend"]) else 0.0
    net   = float(r["Net"])   if pd.notna(r["Net"])   else 0.0
    cpa   = float(r["CPA"])   if pd.notna(r["CPA"])   else None
    avg   = float(r["AvgDep"]) if pd.notna(r["AvgDep"]) else 0.0

    str_cell(ws, row, 1, r["Country"], fill=fill)
    num_fmt(ws, row, 2, acc,   fill=fill)
    num_fmt(ws, row, 3, leads, fill=fill)

    dep_cell = num_fmt(ws, row, 4, deps, fill=fill)
    if deps >= 100:
        dep_cell.font = Font(bold=True, color=C_GOOD, size=10)
    elif deps > 0:
        dep_cell.font = Font(color=C_WARN, size=10)

    avg_cell = num_fmt(ws, row, 5, avg, "#,##0.00", fill=fill)
    if avg >= 1:
        avg_cell.font = Font(bold=True, color=C_GOOD, size=10)
    elif avg < 0.4 and avg > 0:
        avg_cell.font = Font(color=C_BAD, size=10)

    num_fmt(ws, row, 6, spend, "#,##0", fill=fill)
    net_cell = num_fmt(ws, row, 7, net, "#,##0", fill=fill)
    if net > 0:
        net_cell.font = Font(color=C_GOOD, size=10)
    elif net < 0:
        net_cell.font = Font(color=C_BAD, size=10)

    if cpa is not None:
        num_fmt(ws, row, 8, cpa, "#,##0", fill=fill)
    else:
        str_cell(ws, row, 8, "—", fill=fill, align="right")

    totals["Accounts"] += acc
    totals["Leads"]    += leads
    totals["Deposits"] += deps
    totals["Spend"]    += spend
    totals["Net"]      += net

# Totals row
tr = 4 + len(by_country)
tf = hfill(C_SUBHEADER)
str_cell(ws, tr, 1, "TOTAL", bold=True, fill=tf, color=C_TITLE_FG)
for col, key in [(2,"Accounts"),(3,"Leads"),(4,"Deposits")]:
    c = num_fmt(ws, tr, col, totals[key], fill=tf)
    c.font = bold_font(C_TITLE_FG)
total_avg = totals["Deposits"] / totals["Accounts"] if totals["Accounts"] else 0
c = num_fmt(ws, tr, 5, total_avg, "#,##0.00", fill=tf)
c.font = bold_font(C_TITLE_FG)
for col, key in [(6,"Spend"),(7,"Net")]:
    c = num_fmt(ws, tr, col, totals[key], "#,##0", fill=tf)
    c.font = bold_font(C_TITLE_FG)
str_cell(ws, tr, 8, "—", bold=True, fill=tf, align="right", color=C_TITLE_FG)

ws.column_dimensions["A"].width = 24
for col in "BCDEFGH":
    ws.column_dimensions[col].width = 14
ws.freeze_panes = "A4"

# ════════════════════════════════════════════════════════════════════════════════
# SHEET 2: By CM
# ════════════════════════════════════════════════════════════════════════════════
ws2 = wb.create_sheet("By CM")
ws2.merge_cells("A1:G1")
tc2 = ws2["A1"]
tc2.value = "Performance by Campaign Manager"
tc2.font  = Font(bold=True, size=13, color=C_TITLE_FG)
tc2.fill  = hfill(C_HEADER)
tc2.alignment = Alignment(horizontal="center", vertical="center")
ws2.row_dimensions[1].height = 34

header_row(ws2, 2, ["CM", "Leads", "Deposits", "Spend", "Net", "CPA", "ROI"])

for i, (_, r) in enumerate(by_cm.iterrows()):
    row = 3 + i
    fill = hfill(C_ALT) if i % 2 == 0 else None
    deps  = int(r["Deposits"])   if pd.notna(r["Deposits"])   else 0
    leads = int(r["Leads"])      if pd.notna(r["Leads"])      else 0
    spend = float(r["Spend"])    if pd.notna(r["Spend"])      else 0.0
    net   = float(r["Net"])      if pd.notna(r["Net"])        else 0.0
    cpa   = float(r["CPA"])      if pd.notna(r["CPA"])        else None
    roi   = float(r["ROI"])      if pd.notna(r["ROI"])        else None

    str_cell(ws2, row, 1, r["CM"], fill=fill)
    num_fmt(ws2, row, 2, leads, fill=fill)
    dep_c = num_fmt(ws2, row, 3, deps, fill=fill)
    if deps > 0:
        dep_c.font = Font(bold=True, color=C_GOOD, size=10)
    num_fmt(ws2, row, 4, spend, "#,##0", fill=fill)
    net_c = num_fmt(ws2, row, 5, net, "#,##0", fill=fill)
    if net > 0:
        net_c.font = Font(color=C_GOOD, size=10)
    elif net < 0:
        net_c.font = Font(color=C_BAD, size=10)
    if cpa is not None:
        num_fmt(ws2, row, 6, cpa, "#,##0", fill=fill)
    else:
        str_cell(ws2, row, 6, "—", fill=fill, align="right")
    if roi is not None:
        roi_c = num_fmt(ws2, row, 7, roi, "0.00", fill=fill)
        if roi >= 1:
            roi_c.font = Font(bold=True, color=C_GOOD, size=10)
        elif roi < 1:
            roi_c.font = Font(color=C_BAD, size=10)
    else:
        str_cell(ws2, row, 7, "—", fill=fill, align="right")

ws2.column_dimensions["A"].width = 26
for col in "BCDEFG":
    ws2.column_dimensions[col].width = 14
ws2.freeze_panes = "A3"

# ════════════════════════════════════════════════════════════════════════════════
# SHEET 3: Top Combos
# ════════════════════════════════════════════════════════════════════════════════
ws3 = wb.create_sheet("Top Combos")
ws3.merge_cells("A1:F1")
tc3 = ws3["A1"]
tc3.value = f"Top 30 Combos (Country + Interest + Creative)  ·  sorted by Deposits"
tc3.font  = Font(bold=True, size=12, color=C_TITLE_FG)
tc3.fill  = hfill(C_HEADER)
tc3.alignment = Alignment(horizontal="center", vertical="center")
ws3.row_dimensions[1].height = 30

header_row(ws3, 2, ["Country", "Interest", "Creative (Ad Title)", "Deposits", "Leads", "CPA"])

for i, (_, r) in enumerate(by_combo.iterrows()):
    row = 3 + i
    fill = hfill(C_ALT) if i % 2 == 0 else None
    deps  = int(r["Deposits"])  if pd.notna(r["Deposits"])  else 0
    leads = int(r["Leads"])     if pd.notna(r["Leads"])     else 0
    cpa   = float(r["CPA"])     if pd.notna(r["CPA"])       else None

    str_cell(ws3, row, 1, r["Country"],   fill=fill)
    str_cell(ws3, row, 2, r["Interest"],  fill=fill)
    str_cell(ws3, row, 3, str(r["Ad Title"])[:60], fill=fill)
    dep_c = num_fmt(ws3, row, 4, deps, fill=fill)
    if deps >= 10:
        dep_c.font = Font(bold=True, color=C_GOOD, size=10)
    num_fmt(ws3, row, 5, leads, fill=fill)
    if cpa is not None:
        num_fmt(ws3, row, 6, cpa, "#,##0", fill=fill)
    else:
        str_cell(ws3, row, 6, "—", fill=fill, align="right")

ws3.column_dimensions["A"].width = 20
ws3.column_dimensions["B"].width = 22
ws3.column_dimensions["C"].width = 50
ws3.column_dimensions["D"].width = 12
ws3.column_dimensions["E"].width = 12
ws3.column_dimensions["F"].width = 12
ws3.freeze_panes = "A3"

# ── Save ────────────────────────────────────────────────────────────────────────
wb.save(OUT_PATH)
print(f"\nReport saved: {OUT_PATH}")
