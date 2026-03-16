#!/usr/bin/env python3
"""
Deep inspection of Upluad FB.xlsx: every sheet, columns, formulas, values, patterns.
Output to output/excel_inspection_report.txt for agent 8 research.
"""
import os
import sys
from pathlib import Path

# project root
_root = Path(__file__).resolve().parent
sys.path.insert(0, str(_root))
os.chdir(_root)

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

EXCEL_PATH = os.environ.get("UPLOAD_FB_EXCEL", "").strip() or r"C:\Users\EvgeniSedin\Desktop\Upluad FB.xlsx"
OUTPUT_PATH = _root / "output" / "excel_inspection_report.txt"


def inspect_sheet(ws, sheet_name, out_lines):
    """Inspect one sheet: headers, sample rows, formulas, data_only sample."""
    out_lines.append("")
    out_lines.append("=" * 80)
    out_lines.append(f"SHEET: {sheet_name!r}")
    out_lines.append("=" * 80)
    if ws.max_row == 0 and ws.max_column == 0:
        out_lines.append("(empty sheet)")
        return
    out_lines.append(f"Dimensions: max_row={ws.max_row}, max_column={ws.max_column}")
    out_lines.append("")

    # Header row (assume row 1)
    headers = []
    for c in range(1, min(ws.max_column + 1, 50)):
        val = ws.cell(row=1, column=c).value
        headers.append((c, get_column_letter(c), val))
    out_lines.append("--- COLUMNS (row 1) ---")
    for col_idx, col_letter, val in headers:
        out_lines.append(f"  {col_letter}({col_idx}): {repr(val)}")
    out_lines.append("")

    # Sample rows: first 5 data rows, show both formula and value for key columns
    out_lines.append("--- SAMPLE ROWS (rows 2-6) - formulas ---")
    for r in range(2, min(7, ws.max_row + 1)):
        row_vals = []
        for c in range(1, min(ws.max_column + 1, 25)):
            cell = ws.cell(row=r, column=c)
            v = cell.value
            if hasattr(cell, "data_type") and cell.data_type == "f" and v and str(v).startswith("="):
                row_vals.append(f"{get_column_letter(c)}={repr(v)[:60]}")
            elif v is not None and str(v).strip():
                row_vals.append(f"{get_column_letter(c)}={repr(str(v)[:40])}")
        if row_vals:
            out_lines.append(f"  Row {r}: " + " | ".join(row_vals[:12]))
    out_lines.append("")

    # Same rows but data_only (computed values) - need second workbook load
    out_lines.append("--- SAMPLE ROWS (rows 2-6) - computed values (data_only) ---")
    # Will be filled when we load with data_only
    out_lines.append("(see data_only section below)")
    out_lines.append("")


def main():
    if not os.path.isfile(EXCEL_PATH):
        print(f"File not found: {EXCEL_PATH}")
        return 1
    out_lines = []
    out_lines.append("DEEP EXCEL INSPECTION: " + EXCEL_PATH)
    out_lines.append("")

    # Load with formulas to see structure
    wb = load_workbook(EXCEL_PATH, read_only=False, keep_vba=True, data_only=False)
    sheet_names = wb.sheetnames
    out_lines.append(f"All sheets: {sheet_names}")
    out_lines.append("")

    for name in sheet_names:
        ws = wb[name]
        inspect_sheet(ws, name, out_lines)

    wb.close()

    # Load again with data_only to see computed values for first few rows of each sheet
    out_lines.append("")
    out_lines.append("#" * 80)
    out_lines.append("COMPUTED VALUES (data_only=True) - first 5 data rows per sheet")
    out_lines.append("#" * 80)
    wb2 = load_workbook(EXCEL_PATH, read_only=False, data_only=True)
    for name in wb2.sheetnames:
        ws = wb2[name]
        out_lines.append("")
        out_lines.append(f"--- {name} (values only) ---")
        if ws.max_row == 0:
            continue
        for r in range(1, min(6, ws.max_row + 1)):
            row_vals = []
            for c in range(1, min(ws.max_column + 1, 30)):
                v = ws.cell(row=r, column=c).value
                if v is not None and str(v).strip():
                    row_vals.append(f"{get_column_letter(c)}:{repr(str(v)[:35])}")
            if row_vals:
                out_lines.append(f"  R{r}: " + " | ".join(row_vals[:14]))
    wb2.close()

    # Formula patterns: collect all unique formula prefixes (e.g. =VLOOKUP, =IF, =Sheet2!
    out_lines.append("")
    out_lines.append("#" * 80)
    out_lines.append("FORMULA PATTERNS (which functions / refs used)")
    out_lines.append("#" * 80)
    wb3 = load_workbook(EXCEL_PATH, read_only=False, data_only=False)
    formula_refs = set()
    for name in wb3.sheetnames:
        ws = wb3[name]
        for row in ws.iter_rows(min_row=1, max_row=min(ws.max_row, 500), min_col=1, max_col=min(ws.max_column, 50)):
            for cell in row:
                v = cell.value
                if isinstance(v, str) and v.strip().startswith("="):
                    # Normalize: take first token or first 50 chars
                    formula_refs.add((name, v[:80].replace("\n", " ")))
    for name in sorted(set(n for n, _ in formula_refs)):
        out_lines.append("")
        out_lines.append(f"Sheet {name}:")
        for _, f in sorted((n, f) for n, f in formula_refs if n == name)[:30]:
            out_lines.append(f"  {f}")
    wb3.close()

    # Write report
    output_dir = OUTPUT_PATH.parent
    output_dir.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(out_lines))
    print(f"Report written to {OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
