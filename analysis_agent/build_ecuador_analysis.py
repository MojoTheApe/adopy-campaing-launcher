"""
Build Ecuador Targeting Analysis Excel (English).
Sheets:
  1. CM Comparison
  2. Trend (last 7 days)
  3. Working Combos
  4. Targeting Analysis (why Trading works)
  5. Test Priorities
"""
from __future__ import annotations
from pathlib import Path
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side, numbers
from openpyxl.utils import get_column_letter

OUT = Path(__file__).parent / "Ecuador_Targeting_Analysis.xlsx"

# ── Style helpers ──────────────────────────────────────────────────────────────
def _border():
    s = Side(style="thin", color="BFBFBF")
    return Border(left=s, right=s, top=s, bottom=s)

def _fill(hex_color: str) -> PatternFill:
    return PatternFill("solid", fgColor=hex_color)

HEADER_BLUE   = _fill("1F4E79")
HEADER_DARK   = _fill("2F2F2F")
SECTION_GRAY  = _fill("D9E1F2")
SECTION_GREEN = _fill("E2EFDA")
SECTION_RED   = _fill("FCE4D6")
SECTION_AMBER = _fill("FFF2CC")
SECTION_TEAL  = _fill("DDEBF7")
ROW_ALT       = _fill("F5F8FF")
WHITE         = _fill("FFFFFF")

def hdr(ws, row, col, text, fill=None, bold=True, font_color="FFFFFF", size=10,
        wrap=False, align="center"):
    c = ws.cell(row=row, column=col, value=text)
    c.font = Font(bold=bold, color=font_color, size=size, name="Calibri")
    if fill:
        c.fill = fill
    c.border = _border()
    c.alignment = Alignment(horizontal=align, vertical="center", wrap_text=wrap)
    return c

def val(ws, row, col, value, bold=False, fill=None, align="center", fmt=None,
        font_color="000000", size=10, wrap=False, italic=False):
    c = ws.cell(row=row, column=col, value=value)
    c.font = Font(bold=bold, color=font_color, size=size, name="Calibri",
                  italic=italic)
    if fill:
        c.fill = fill
    c.border = _border()
    c.alignment = Alignment(horizontal=align, vertical="center", wrap_text=wrap)
    if fmt:
        c.number_format = fmt
    return c

def merge_title(ws, row, col1, col2, text, fill=None, font_color="FFFFFF",
                size=12, bold=True):
    ws.merge_cells(start_row=row, start_column=col1,
                   end_row=row, end_column=col2)
    c = ws.cell(row=row, column=col1, value=text)
    c.font = Font(bold=bold, color=font_color, size=size, name="Calibri")
    if fill:
        c.fill = fill
    c.border = _border()
    c.alignment = Alignment(horizontal="center", vertical="center")
    return c

def set_col_widths(ws, widths: dict[int, float]):
    for col, w in widths.items():
        ws.column_dimensions[get_column_letter(col)].width = w

def freeze(ws, cell="A2"):
    ws.freeze_panes = cell

# ══════════════════════════════════════════════════════════════════════════════
# Sheet 1 — CM Comparison
# ══════════════════════════════════════════════════════════════════════════════
def sheet_cm(wb: Workbook):
    ws = wb.create_sheet("1. CM Comparison")
    ws.row_dimensions[1].height = 24
    ws.row_dimensions[2].height = 18

    merge_title(ws, 1, 1, 7, "Ecuador — Campaign Manager Comparison",
                fill=HEADER_BLUE, size=13)

    headers = ["Campaign Manager", "Spend ($)", "Leads", "Deposits",
               "CPL ($)", "CPA ($)", "CR (%)", "ROI"]
    for i, h in enumerate(headers, 1):
        hdr(ws, 2, i, h, fill=HEADER_BLUE)

    rows = [
        ("Evgeni Sedin  ← YOU", 34879.49, 1924, 19, 18.13, 1835.76, 1.0, 0.76),
        ("Gil Zangi",           173416.56, 8230, 129, 21.07, 1344.31, 1.6, 1.03),
        ("Yotam Balteriski",    45783.58, 2395, 34, 19.12, 1346.58, 1.4, 2.74),
        ("Dan Dicker",          36004.42, 1833, 22, 19.64, 1636.56, 1.2, 0.13),
    ]

    for r, (cm, spend, leads, dep, cpl, cpa, cr, roi) in enumerate(rows, 3):
        is_you = cm.endswith("YOU")
        f = _fill("D6E4F7") if is_you else (ROW_ALT if r % 2 == 0 else WHITE)
        bold = is_you
        val(ws, r, 1, cm,    bold=bold, fill=f, align="left")
        val(ws, r, 2, spend, bold=bold, fill=f, fmt='#,##0.00')
        val(ws, r, 3, leads, bold=bold, fill=f)
        val(ws, r, 4, dep,   bold=bold, fill=f)
        val(ws, r, 5, cpl,   bold=bold, fill=f, fmt='#,##0.00')
        val(ws, r, 6, cpa,   bold=bold, fill=f, fmt='#,##0.00')
        val(ws, r, 7, cr,    bold=bold, fill=f, fmt='0.0%')
        val(ws, r, 8, roi,   bold=bold, fill=f, fmt='0.00')

    set_col_widths(ws, {1: 24, 2: 14, 3: 10, 4: 10,
                        5: 10, 6: 12, 7: 10, 8: 10})
    freeze(ws, "A3")

    # note row
    r = 8
    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=8)
    c = ws.cell(row=r, column=1,
        value=("ℹ️  You are significantly behind Gil and Yotam on volume "
               "(19 dep vs 129 / 34). Your CPA of $1,836 is the highest — "
               "priority: relaunch proven combos on new accounts."))
    c.font = Font(italic=True, color="595959", size=9, name="Calibri")
    c.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)
    ws.row_dimensions[r].height = 36

# ══════════════════════════════════════════════════════════════════════════════
# Sheet 2 — 7-Day Trend
# ══════════════════════════════════════════════════════════════════════════════
def sheet_trend(wb: Workbook):
    ws = wb.create_sheet("2. Trend (7d)")
    ws.row_dimensions[1].height = 24

    merge_title(ws, 1, 1, 5,
                "Ecuador — Trend Apr 07–13 2026 (last 7 days)",
                fill=HEADER_BLUE, size=13)

    # ── NEW combos ─────────────────────────────────────────────────────────
    merge_title(ws, 2, 1, 5, "NEW — Deposits appeared this period",
                fill=SECTION_GREEN, font_color="1A3C1A", size=10)
    hdr(ws, 3, 1, "Targeting",  fill=HEADER_BLUE, size=9)
    hdr(ws, 3, 2, "Creative",   fill=HEADER_BLUE, size=9)
    hdr(ws, 3, 3, "Offer",      fill=HEADER_BLUE, size=9)
    hdr(ws, 3, 4, "Deposits",   fill=HEADER_BLUE, size=9)
    hdr(ws, 3, 5, "Note",       fill=HEADER_BLUE, size=9)

    new_rows = [
        ("TRD", "Blue_Story1", "AI Trader", 1, ""),
        ("TRD", "Blue_Story3", "AI Trader", 1, ""),
        ("TRD", "Blue_Story5", "AI Trader", 1, "TRD emerging — 3 new combos"),
        ("Trading_vid", "—",          "Favorita",  1, ""),
        ("Trading_vid", "1953 3",     "Favorita",  1, "New creative 1953"),
    ]
    for r, (tgt, cr, offer, dep, note) in enumerate(new_rows, 4):
        f = ROW_ALT if r % 2 == 0 else WHITE
        val(ws, r, 1, tgt,   fill=f)
        val(ws, r, 2, cr,    fill=f)
        val(ws, r, 3, offer, fill=f)
        val(ws, r, 4, dep,   fill=f)
        val(ws, r, 5, note,  fill=f, align="left", italic=True, size=9)

    # ── STOPPED combos ─────────────────────────────────────────────────────
    r = 10
    merge_title(ws, r, 1, 5,
                "STOPPED — Had deposits before, 0 in last 7 days",
                fill=SECTION_RED, font_color="5C0000", size=10)
    hdr(ws, r+1, 1, "Targeting",     fill=HEADER_BLUE, size=9)
    hdr(ws, r+1, 2, "Creative",      fill=HEADER_BLUE, size=9)
    hdr(ws, r+1, 3, "Offer",         fill=HEADER_BLUE, size=9)
    hdr(ws, r+1, 4, "Total Dep",     fill=HEADER_BLUE, size=9)
    hdr(ws, r+1, 5, "Recent (7d)",   fill=HEADER_BLUE, size=9)

    stopped = [
        ("Trading",     "1389 2", "Favorita", 23, 0),
        ("Trading_vid", "1319 3", "Favorita", 17, 0),
        ("Trading",     "1319 3", "Favorita", 14, 0),
        ("Trading",     "1319 1", "Favorita", 13, 0),
        ("Trading",     "509 3",  "Favorita", 10, 0),
    ]
    for i, (tgt, cr, offer, tot, rec) in enumerate(stopped, r+2):
        f = ROW_ALT if i % 2 == 0 else WHITE
        val(ws, i, 1, tgt,   fill=f)
        val(ws, i, 2, cr,    fill=f)
        val(ws, i, 3, offer, fill=f)
        val(ws, i, 4, tot,   fill=f, bold=True)
        val(ws, i, 5, rec,   fill=f, font_color="CC0000", bold=True)

    note_r = r + 2 + len(stopped) + 1
    ws.merge_cells(start_row=note_r, start_column=1,
                   end_row=note_r, end_column=5)
    c = ws.cell(row=note_r, column=1,
        value=("ℹ️  All stopped combos were on accounts that are now Disabled. "
               "These combos are not dead — they need to be relaunched on new accounts."))
    c.font = Font(italic=True, color="595959", size=9, name="Calibri")
    c.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)
    ws.row_dimensions[note_r].height = 32

    set_col_widths(ws, {1: 16, 2: 14, 3: 12, 4: 12, 5: 36})

# ══════════════════════════════════════════════════════════════════════════════
# Sheet 3 — Working Combos
# ══════════════════════════════════════════════════════════════════════════════
def sheet_combos(wb: Workbook):
    ws = wb.create_sheet("3. Working Combos")
    ws.row_dimensions[1].height = 24
    ws.row_dimensions[2].height = 18

    merge_title(ws, 1, 1, 7, "Ecuador — All Strong Combos (cumulative)",
                fill=HEADER_BLUE, size=13)

    hdrs = ["Targeting", "Creative", "Offer", "Deposits", "CPA ($)", "ROI",
            "Assessment"]
    for i, h in enumerate(hdrs, 1):
        hdr(ws, 2, i, h, fill=HEADER_BLUE)

    combos = [
        ("Trading",     "1389 2", "Favorita", 23, 1148.29, 1.32,
         "High volume, profitable"),
        ("Trading_vid", "1319 3", "Favorita", 17, 1343.68, 0.42,
         "⚠️ ROI < 1 — unprofitable, volume only"),
        ("Trading",     "1319 3", "Favorita", 14, 1159.54, 3.79,
         "Best ROI, high quality traffic"),
        ("Trading",     "1319 1", "Favorita", 13, 1200.90, 0.81,
         "Borderline — watch closely"),
        ("Trading",     "509 3",  "Favorita", 10, 467.16,  2.48,
         "⭐ Best CPA by far — 2.5× cheaper than others"),
    ]

    for r, (tgt, cr, offer, dep, cpa, roi, note) in enumerate(combos, 3):
        # color by ROI
        if roi >= 2.0:
            f = _fill("E2EFDA")
        elif roi >= 1.0:
            f = _fill("DDEBF7")
        elif roi >= 0.5:
            f = _fill("FFF2CC")
        else:
            f = _fill("FCE4D6")

        val(ws, r, 1, tgt,   fill=f, bold=True)
        val(ws, r, 2, cr,    fill=f, bold=True)
        val(ws, r, 3, offer, fill=f)
        val(ws, r, 4, dep,   fill=f, bold=True)
        val(ws, r, 5, cpa,   fill=f, fmt='#,##0.00')
        val(ws, r, 6, roi,   fill=f, bold=True, fmt='0.00')
        val(ws, r, 7, note,  fill=f, align="left", size=9, italic=True)

    # legend
    r = 9
    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=7)
    c = ws.cell(row=r, column=1,
        value="Color: Green = ROI ≥ 2.0 | Blue = ROI ≥ 1.0 | Yellow = ROI ≥ 0.5 | Red = ROI < 0.5")
    c.font = Font(italic=True, color="595959", size=9, name="Calibri")
    c.alignment = Alignment(horizontal="left", vertical="center")

    set_col_widths(ws, {1: 16, 2: 12, 3: 12, 4: 10, 5: 12, 6: 10, 7: 38})
    freeze(ws, "A3")

# ══════════════════════════════════════════════════════════════════════════════
# Sheet 4 — Targeting Analysis
# ══════════════════════════════════════════════════════════════════════════════
def sheet_targeting(wb: Workbook):
    ws = wb.create_sheet("4. Targeting Analysis")
    ws.row_dimensions[1].height = 28

    merge_title(ws, 1, 1, 6,
                "Ecuador — Why 'Trading' Targeting Outperforms Everything",
                fill=HEADER_BLUE, size=13)

    # ── Performance table ───────────────────────────────────────────────────
    merge_title(ws, 2, 1, 6, "All Targeting — Performance Ranked by CPA",
                fill=SECTION_GRAY, font_color="1F4E79", size=10)

    hdrs = ["Targeting", "Deposits", "Spend ($)", "CPA ($)", "Status", "Why"]
    for i, h in enumerate(hdrs, 1):
        hdr(ws, 3, i, h, fill=HEADER_BLUE)

    targeting_data = [
        ("Trading",        62, 71311,  1150,  "✅ #1 Best CPA",
         "Broad 'want to start trading' intent. Widest quality audience."),
        ("GPT_FX/SM/IOS",  6,  7580,   1263,  "✅ #2 (low volume)",
         "Likely FX / crypto interest — high intent, needs more data."),
        ("Day (Day Trading)",30,43819,  1461,  "✅ Works but costlier",
         "Day traders already have a broker — harder to convert."),
        ("Trading_vid",    35, 54771,  1565,  "⚠️ High spend, moderate CPA",
         "Video viewers — engagement ≠ purchase intent. Wider, colder audience."),
        ("TRD",             5,  8647,  1729,  "🔵 Emerging (last 7d)",
         "New targeting, growing now. Blue Story combos just started converting."),
        ("DT_Trading",      0,  1490,   None, "❌ 0 deposits",
         "Day trading subset — too specific, professional traders ignore offers."),
        ("Day3",            0,  1847,   None, "❌ 0 deposits",
         "Likely retargeting day-3 sequence — audience too cold at this stage."),
        ("(unknown)",       0,  2351,   None, "❌ 0 deposits",
         "No label — untracked campaign, cannot optimise."),
    ]

    for r, (tgt, dep, spend, cpa, status, why) in enumerate(targeting_data, 4):
        if dep == 0:
            f = SECTION_RED
        elif cpa and cpa <= 1200:
            f = SECTION_GREEN
        elif cpa and cpa <= 1500:
            f = SECTION_TEAL
        else:
            f = SECTION_AMBER
        val(ws, r, 1, tgt,    fill=f, bold=dep > 0)
        val(ws, r, 2, dep,    fill=f, bold=True)
        val(ws, r, 3, spend,  fill=f, fmt='#,##0')
        val(ws, r, 4, cpa if cpa else "—",  fill=f,
            fmt='#,##0' if cpa else '@', bold=cpa is not None)
        val(ws, r, 5, status, fill=f, align="left", size=9)
        val(ws, r, 6, why,    fill=f, align="left", size=9, wrap=True)
        ws.row_dimensions[r].height = 28

    # ── Explanation block ───────────────────────────────────────────────────
    r = 13
    merge_title(ws, r, 1, 6,
                "Key Insight: Audience Intent Pyramid",
                fill=SECTION_GRAY, font_color="1F4E79", size=10)
    r += 1

    explanations = [
        ("Trading (generic)",
         "$1,150 CPA",
         "People who WANT to start investing. They are looking for an entry point. "
         "Favorita = perfect match. Widest pool of high-intent beginners."),
        ("Day Trading (Day)",
         "$1,461 CPA",
         "People actively day-trading. Already have a broker. More skeptical. "
         "Still converts because some look for better platforms, but 27% more expensive."),
        ("DT_Trading (strict)",
         "0 deposits",
         "Professional day traders. Know the market, don't click broker ads. "
         "Audience is too sophisticated for a general trading offer."),
        ("Trading_vid",
         "$1,565 CPA",
         "Watched a trading video at some point. No strong purchase intent — "
         "engagement ≠ intention to invest. Good for retargeting, bad for cold traffic."),
    ]

    hdr(ws, r, 1, "Targeting Tier",  fill=HEADER_BLUE, size=9)
    hdr(ws, r, 2, "CPA",             fill=HEADER_BLUE, size=9)
    ws.merge_cells(start_row=r, start_column=3, end_row=r, end_column=6)
    hdr(ws, r, 3, "Explanation",     fill=HEADER_BLUE, size=9)
    r += 1

    for i, (tier, cpa_str, expl) in enumerate(explanations, r):
        f = ROW_ALT if i % 2 == 0 else WHITE
        val(ws, i, 1, tier,     fill=f, bold=True, align="left")
        val(ws, i, 2, cpa_str,  fill=f, bold=True)
        ws.merge_cells(start_row=i, start_column=3, end_row=i, end_column=6)
        val(ws, i, 3, expl,     fill=f, align="left", wrap=True, size=9)
        ws.row_dimensions[i].height = 36

    set_col_widths(ws, {1: 22, 2: 12, 3: 14, 4: 12, 5: 18, 6: 38})

# ══════════════════════════════════════════════════════════════════════════════
# Sheet 5 — Test Priorities
# ══════════════════════════════════════════════════════════════════════════════
def sheet_priorities(wb: Workbook):
    ws = wb.create_sheet("5. Test Priorities")
    ws.row_dimensions[1].height = 28

    merge_title(ws, 1, 1, 6,
                "Ecuador — Test Priorities (based on targeting analysis)",
                fill=HEADER_BLUE, size=13)

    # ── Priority 2 ──────────────────────────────────────────────────────────
    merge_title(ws, 2, 1, 6,
                "Priority 2 — Test Creative 509 on Day (Day Trading) Targeting",
                fill=SECTION_AMBER, font_color="3F2800", size=11)

    hdr(ws, 3, 1, "Ad Set",        fill=HEADER_BLUE, size=9)
    hdr(ws, 3, 2, "Targeting",     fill=HEADER_BLUE, size=9)
    hdr(ws, 3, 3, "Creative",      fill=HEADER_BLUE, size=9)
    hdr(ws, 3, 4, "Offer",         fill=HEADER_BLUE, size=9)
    hdr(ws, 3, 5, "Expected CPA",  fill=HEADER_BLUE, size=9)
    hdr(ws, 3, 6, "Rationale",     fill=HEADER_BLUE, size=9)

    p2_rows = [
        ("EC_Fav_All_Day_509_3",
         "Day",    "509 3", "Favorita",
         "~$600–900?",
         "Day has 30 total deps. 509 3 gives $467 CPA with Trading. "
         "Different hook may convert skeptical day-traders better."),
        ("EC_Fav_All_Day_509_1",
         "Day",    "509 1", "Favorita",
         "Unknown",
         "509 series — test all 3 versions. 509 1 not yet tried with any targeting."),
        ("EC_Fav_All_Day_509_2",
         "Day",    "509 2", "Favorita",
         "Unknown",
         "509 series — test all 3 versions. 509 2 not yet tried with any targeting."),
    ]

    for r, (name, tgt, cr, offer, exp_cpa, rat) in enumerate(p2_rows, 4):
        f = ROW_ALT if r % 2 == 0 else WHITE
        val(ws, r, 1, name,    fill=f, bold=True, align="left", size=9)
        val(ws, r, 2, tgt,     fill=f)
        val(ws, r, 3, cr,      fill=f, bold=True)
        val(ws, r, 4, offer,   fill=f)
        val(ws, r, 5, exp_cpa, fill=f, italic=True, size=9)
        val(ws, r, 6, rat,     fill=f, align="left", size=9, wrap=True)
        ws.row_dimensions[r].height = 36

    # ── Priority 4 ──────────────────────────────────────────────────────────
    r = 8
    merge_title(ws, r, 1, 6,
                "Priority 4 — Test Creative 509 on GPT_FX/SM/IOS Targeting",
                fill=SECTION_TEAL, font_color="0C2340", size=11)

    hdr(ws, r+1, 1, "Ad Set",        fill=HEADER_BLUE, size=9)
    hdr(ws, r+1, 2, "Targeting",     fill=HEADER_BLUE, size=9)
    hdr(ws, r+1, 3, "Creative",      fill=HEADER_BLUE, size=9)
    hdr(ws, r+1, 4, "Offer",         fill=HEADER_BLUE, size=9)
    hdr(ws, r+1, 5, "Expected CPA",  fill=HEADER_BLUE, size=9)
    hdr(ws, r+1, 6, "Rationale",     fill=HEADER_BLUE, size=9)

    p4_rows = [
        ("EC_Fav_All_GPTFX_509_3",
         "GPT_FX/SM/IOS", "509 3", "Favorita",
         "~$400–700?",
         "GPT_FX/SM/IOS = #2 best CPA on the country at $1,263 (6 deps). "
         "509 3 CPA advantage vs other creatives is 2.5×. "
         "Combining best targeting #2 + best creative = highest potential."),
        ("EC_Fav_All_GPTFX_509_1",
         "GPT_FX/SM/IOS", "509 1", "Favorita",
         "Unknown",
         "Test full 509 series with this targeting — only 6 total deps so far, "
         "need data to see which 509 version resonates."),
        ("EC_Fav_All_GPTFX_509_2",
         "GPT_FX/SM/IOS", "509 2", "Favorita",
         "Unknown",
         "Same — complete the series test."),
    ]

    for i, (name, tgt, cr, offer, exp_cpa, rat) in enumerate(p4_rows, r+2):
        f = ROW_ALT if i % 2 == 0 else WHITE
        val(ws, i, 1, name,    fill=f, bold=True, align="left", size=9)
        val(ws, i, 2, tgt,     fill=f)
        val(ws, i, 3, cr,      fill=f, bold=True)
        val(ws, i, 4, offer,   fill=f)
        val(ws, i, 5, exp_cpa, fill=f, italic=True, size=9)
        val(ws, i, 6, rat,     fill=f, align="left", size=9, wrap=True)
        ws.row_dimensions[i].height = 40

    # ── 509 Full Matrix ──────────────────────────────────────────────────────
    r = 15
    merge_title(ws, r, 1, 6,
                "Full 509 Series × Targeting Matrix — What to Test",
                fill=SECTION_GRAY, font_color="1F4E79", size=10)
    r += 1

    targets = ["Trading", "Trading_vid", "Day", "TRD", "GPT_FX/SM/IOS"]
    creatives = ["509 1", "509 2", "509 3"]
    results = {
        ("Trading", "509 3"): ("✅ $467 / ROI 2.48", SECTION_GREEN),
    }

    # header row
    hdr(ws, r, 1, "Targeting \\ Creative", fill=HEADER_BLUE, size=9)
    for ci, cr_name in enumerate(creatives, 2):
        hdr(ws, r, ci, cr_name, fill=HEADER_BLUE, size=9)
    ws.merge_cells(start_row=r, start_column=5, end_row=r, end_column=6)
    hdr(ws, r, 5, "Note", fill=HEADER_BLUE, size=9)
    r += 1

    for ti, tgt in enumerate(targets):
        row_f = ROW_ALT if ti % 2 == 0 else WHITE
        val(ws, r, 1, tgt, fill=row_f, bold=True)
        for ci, cr_name in enumerate(creatives, 2):
            key = (tgt, cr_name)
            if key in results:
                text, f = results[key]
                val(ws, r, ci, text, fill=f, bold=True, size=8)
            else:
                val(ws, r, ci, "❓ Not tested", fill=row_f, size=8,
                    font_color="808080", italic=True)
        note = ""
        if tgt == "Day":
            note = "30 total deps — high potential"
        elif tgt == "GPT_FX/SM/IOS":
            note = "$1,263 CPA — #2 best on country"
        elif tgt == "TRD":
            note = "Growing now (Blue Story active)"
        ws.merge_cells(start_row=r, start_column=5, end_row=r, end_column=6)
        val(ws, r, 5, note, fill=row_f, align="left", size=9, italic=True)
        r += 1

    set_col_widths(ws, {1: 26, 2: 20, 3: 20, 4: 22, 5: 20, 6: 18})


# ══════════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════════
def main():
    wb = Workbook()
    wb.remove(wb.active)

    sheet_cm(wb)
    sheet_trend(wb)
    sheet_combos(wb)
    sheet_targeting(wb)
    sheet_priorities(wb)

    wb.save(OUT)
    print(f"Saved: {OUT}")

if __name__ == "__main__":
    main()
