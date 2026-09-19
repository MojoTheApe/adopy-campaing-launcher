"""Shared Stats workbook validation for setup and dashboard settings."""
from pathlib import Path

STATS_COLUMNS = {
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
}

class StatsWorkbookError(ValueError):
    pass


def validate_stats_workbook(path: str) -> str:
    workbook_path = Path(path)
    if not workbook_path.is_file():
        raise StatsWorkbookError("Stats workbook does not exist.")
    if workbook_path.suffix.casefold() not in {".xlsx", ".xlsm"}:
        raise StatsWorkbookError("Stats workbook must be an .xlsx or .xlsm file.")
    try:
        from openpyxl import load_workbook

        workbook = load_workbook(workbook_path, read_only=True, data_only=True)
        try:
            sheet = workbook[workbook.sheetnames[0]]
            headers = {str(cell.value).strip() for cell in next(sheet.iter_rows(max_row=1)) if cell.value}
        finally:
            workbook.close()
    except Exception as exc:
        raise StatsWorkbookError(f"Stats workbook cannot be opened: {exc}") from exc
    missing = sorted(STATS_COLUMNS - headers)
    if missing:
        raise StatsWorkbookError("Stats workbook is missing columns: " + ", ".join(missing))
    return str(workbook_path.resolve())
