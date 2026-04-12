"""Read account status rows from the daily Google Sheet (last/rightmost tab).

Environment variables (from .env):
    GSHEET_ACCOUNTS_ID      — Google Sheet ID from the spreadsheet URL
    GSHEET_ACCOUNTS_SHEET   — Tab name, or "auto_last" to always use the rightmost tab
    MY_CM_NAME              — Your CM name as it appears in column A (default: Evgeni Sedin)
    GOOGLE_CREDENTIALS_JSON — Path to service-account JSON (default: config/google_credentials.json)

Sheet layout (last/today's tab):
    Col A — CM name (campaign manager)
    Col B — Country code (e.g. MX, GT, EC)
    Col C — Account label  (e.g. "FacebookMKTO1211 MX")
    Col J — To Do          (e.g. "Account Disabled", "Video Selfie", or empty = ready)

    The bottom of each tab also contains a totals table whose first cell starts
    with "Country" or "Total" — those rows are skipped automatically.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

_PROJ_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJ_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJ_ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(_PROJ_ROOT / ".env")
except ImportError:
    pass

# ── Constants ──────────────────────────────────────────────────────────────────

_DISABLED_STATUSES = {"account disabled", "account restricted", "video selfie"}

_TOTALS_HEADERS = {"country", "total", "totals", "cm", "sum"}


# ── Google Sheets helpers ──────────────────────────────────────────────────────

def _build_sheets_service(credentials_path: str):
    """Create Google Sheets API service via service account (read-only)."""
    try:
        from google.oauth2.service_account import Credentials
        from googleapiclient.discovery import build
    except ImportError:
        print(
            "[gsheet_accounts] ERROR: Google API libraries not installed.\n"
            "  pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib",
            file=sys.stderr,
        )
        return None

    creds_path = Path(credentials_path)
    if not creds_path.is_absolute():
        creds_path = _PROJ_ROOT / creds_path

    if not creds_path.is_file():
        print(
            f"[gsheet_accounts] ERROR: Credentials not found: {creds_path}\n"
            "  See scripts/README_GSHEET_SETUP.md",
            file=sys.stderr,
        )
        return None

    creds = Credentials.from_service_account_file(
        str(creds_path),
        scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"],
    )
    return build("sheets", "v4", credentials=creds)


def _get_last_tab_name(service, sheet_id: str) -> str:
    """Return the title of the rightmost (last) tab in the spreadsheet."""
    meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    sheets = meta.get("sheets", [])
    if not sheets:
        raise ValueError(f"No sheets found in spreadsheet {sheet_id}")
    last = sheets[-1]
    return last["properties"]["title"]


def _fetch_range(service, sheet_id: str, sheet_name: str, range_notation: str) -> List[List[str]]:
    result = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=sheet_id, range=f"{sheet_name}!{range_notation}")
        .execute()
    )
    return result.get("values", [])


# ── Parsing ────────────────────────────────────────────────────────────────────

def _is_totals_row(col_a: str) -> bool:
    """True if this row is part of the bottom totals table (not an account row)."""
    return col_a.strip().lower() in _TOTALS_HEADERS


def _account_alias_from_label(label: str) -> str:
    """Strip trailing country-code suffix to get Excel Account Alias.

    Examples:
        "FacebookMKTO1211 MX" → "FacebookMKTO1211"
        "Facebook662 MX"      → "Facebook662"
        "Facebook_8661 GT"    → "Facebook_8661"
    """
    parts = label.strip().rsplit(" ", 1)
    if len(parts) == 2 and len(parts[1]) <= 4 and parts[1].isalpha():
        return parts[0].strip()
    return label.strip()


def _is_ready(to_do: str) -> bool:
    return to_do.strip().lower() not in _DISABLED_STATUSES


def parse_account_rows(raw_rows: List[List[str]]) -> List[Dict[str, Any]]:
    """Parse raw sheet rows → list of account dicts.

    Each row must have at least 3 cells (A, B, C). Column J (index 9) is To Do.
    Rows where col A is a totals header or col C is empty are skipped.
    """
    accounts: List[Dict[str, Any]] = []
    for row in raw_rows:
        # Pad to at least 10 columns so index 9 (col J) is safe
        padded = row + [""] * max(0, 10 - len(row))

        col_a = padded[0].strip()
        col_b = padded[1].strip() if len(padded) > 1 else ""
        col_c = padded[2].strip() if len(padded) > 2 else ""
        col_j = padded[9].strip() if len(padded) > 9 else ""

        if not col_a or not col_c:
            continue
        if _is_totals_row(col_a):
            continue

        alias = _account_alias_from_label(col_c)
        accounts.append(
            {
                "cm": col_a,
                "country_code": col_b,
                "account_label": col_c,
                "alias": alias,
                "to_do": col_j,
                "is_ready": _is_ready(col_j),
            }
        )
    return accounts


# ── Public API ─────────────────────────────────────────────────────────────────

def fetch_account_rows(
    sheet_id: Optional[str] = None,
    sheet_name: Optional[str] = None,
    credentials_path: Optional[str] = None,
    my_cm: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Fetch and parse account rows from the Google Sheet.

    Args:
        sheet_id: Google Sheet ID (falls back to GSHEET_ACCOUNTS_ID env var).
        sheet_name: Tab name or "auto_last" (falls back to GSHEET_ACCOUNTS_SHEET env var).
        credentials_path: Path to service account JSON (falls back to GOOGLE_CREDENTIALS_JSON env var).
        my_cm: Your CM name to filter by (falls back to MY_CM_NAME env var; None = return all CMs).

    Returns:
        List of dicts with keys: cm, country_code, account_label, alias, to_do, is_ready.
        Returns empty list on any error (caller decides whether to continue without GSheet).
    """
    sid = sheet_id or os.environ.get("GSHEET_ACCOUNTS_ID", "").strip()
    sname = sheet_name or os.environ.get("GSHEET_ACCOUNTS_SHEET", "auto_last").strip()
    creds = credentials_path or os.environ.get(
        "GOOGLE_CREDENTIALS_JSON", "config/google_credentials.json"
    )
    cm_filter = my_cm or os.environ.get("MY_CM_NAME", "").strip()

    if not sid:
        print(
            "[gsheet_accounts] GSHEET_ACCOUNTS_ID not set — skipping GSheet read.\n"
            "  Add GSHEET_ACCOUNTS_ID to .env to enable account status sections.",
            file=sys.stderr,
        )
        return []

    service = _build_sheets_service(creds)
    if service is None:
        return []

    try:
        if sname.lower() == "auto_last":
            sname = _get_last_tab_name(service, sid)
            print(f"[gsheet_accounts] Using last tab: {sname!r}", file=sys.stderr)

        # Read columns A:J (10 columns) — enough to cover col J
        raw = _fetch_range(service, sid, sname, "A:J")
        print(
            f"[gsheet_accounts] Fetched {len(raw)} rows from tab {sname!r}",
            file=sys.stderr,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[gsheet_accounts] ERROR reading sheet: {exc}", file=sys.stderr)
        return []

    rows = parse_account_rows(raw)

    if cm_filter:
        cm_lower = cm_filter.lower()
        rows = [r for r in rows if r["cm"].lower() == cm_lower]

    print(
        f"[gsheet_accounts] Parsed {len(rows)} account rows"
        + (f" for CM '{cm_filter}'" if cm_filter else ""),
        file=sys.stderr,
    )
    return rows


def group_accounts_by_country(rows: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """Return {country_code: [account_row, ...]} mapping."""
    result: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        cc = row["country_code"] or "??"
        result.setdefault(cc, []).append(row)
    return result
