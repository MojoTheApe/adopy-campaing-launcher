"""One-off: print structure of Accounts, Main, LP's to implement Description logic."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from config.paths import get_upload_fb_excel_path
from openpyxl import load_workbook

path = get_upload_fb_excel_path()
print(f"Reading: {path}\n")
wb = load_workbook(path, read_only=False, data_only=False)  # formulas to see refs
wb_val = load_workbook(path, read_only=False, data_only=True)  # values

for sheet_name in ["Accounts", "Main", "LP's"]:
    if sheet_name not in wb.sheetnames:
        print(f"Sheet '{sheet_name}' not found. Available: {wb.sheetnames}\n")
        continue
    ws = wb[sheet_name]
    ws_val = wb_val[sheet_name]
    print(f"=== {sheet_name} (row 1 = headers) ===")
    headers = []
    for c in range(1, min(ws.max_column + 1, 35)):
        v = ws.cell(row=1, column=c).value
        headers.append((c, str(v)[:50] if v else ""))
    for col_idx, h in headers:
        if h:
            print(f"  Col {col_idx}: {h}")
    print()
    # Row 2 sample (formulas vs values)
    if ws.max_row >= 2 and sheet_name == "Main":
        print(f"  Row 2 all cols (formula -> value) for Description order:")
        for c in range(1, min(ws.max_column + 1, 35)):
            f = ws.cell(row=2, column=c).value
            v = ws_val.cell(row=2, column=c).value
            f_str = str(f)[:60] if f else ""
            v_str = str(v)[:70] if v else ""
            if f_str or v_str:
                print(f"    {c}: val={v_str!r}")
    elif ws.max_row >= 2:
        print(f"  Row 2 sample (first 8 cols):")
        for c in range(1, min(9, ws.max_column + 1)):
            v = ws_val.cell(row=2, column=c).value
            if v is not None:
                print(f"    Col {c}: {str(v)[:50]!r}")
    print()

wb.close()
wb_val.close()
