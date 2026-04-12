"""Read account status rows from the daily Google Sheet (last/rightmost tab).

Environment variables (from .env):
    GSHEET_ACCOUNTS_ID      — Google Sheet ID from the spreadsheet URL
    GSHEET_ACCOUNTS_SHEET   — Tab name, or "auto_last" to always use the rightmost tab
    MY_CM_NAME              — Your CM name as it appears in column A (default: Evgeni Sedin)
    GSHEET_OAUTH_CLIENT     — Path to OAuth client JSON (default: config/oauth_client.json)
    GSHEET_OAUTH_TOKEN      — Path to saved OAuth token (default: config/token_accounts.json)
    GOOGLE_CREDENTIALS_JSON — Path to service-account JSON (fallback if OAuth client not found)

Auth mode (auto-detected):
    If config/oauth_client.json exists → OAuth2 user login (opens browser on first run).
    Otherwise → service account via GOOGLE_CREDENTIALS_JSON.

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

_SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]


# ── Google Sheets helpers ──────────────────────────────────────────────────────

def _build_sheets_service_oauth(oauth_client_path: str, token_path: str):
    """Create Google Sheets API service via OAuth2 user login.

    First run: opens browser so the user logs in with their Google account.
    Subsequent runs: reuses saved token automatically.
    """
    try:
        import google.auth.transport.requests
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError:
        print(
            "[gsheet_accounts] ERROR: Google API libraries not installed.\n"
            "  pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib",
            file=sys.stderr,
        )
        return None

    client_path = Path(oauth_client_path)
    if not client_path.is_absolute():
        client_path = _PROJ_ROOT / client_path

    token_file = Path(token_path)
    if not token_file.is_absolute():
        token_file = _PROJ_ROOT / token_file

    creds = None

    # Load saved token if it exists
    if token_file.is_file():
        try:
            creds = Credentials.from_authorized_user_file(str(token_file), _SCOPES)
        except Exception:
            creds = None

    # Refresh or re-authenticate
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(google.auth.transport.requests.Request())
            except Exception:
                creds = None

        if not creds:
            if not client_path.is_file():
                print(
                    f"[gsheet_accounts] ERROR: OAuth client file not found: {client_path}",
                    file=sys.stderr,
                )
                return None
            print(
                "[gsheet_accounts] Opening browser for Google login...",
                file=sys.stderr,
            )
            flow = InstalledAppFlow.from_client_secrets_file(str(client_path), _SCOPES)
            creds = flow.run_local_server(port=0)

        # Save token for future runs
        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text(creds.to_json(), encoding="utf-8")
        print(f"[gsheet_accounts] Token saved: {token_file}", file=sys.stderr)

    return build("sheets", "v4", credentials=creds)


def _build_sheets_service_sa(credentials_path: str):
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
            f"[gsheet_accounts] ERROR: Service account credentials not found: {creds_path}",
            file=sys.stderr,
        )
        return None

    creds = Credentials.from_service_account_file(
        str(creds_path),
        scopes=_SCOPES,
    )
    return build("sheets", "v4", credentials=creds)


def _build_sheets_service(
    credentials_path: str,
    oauth_client_path: str = "config/oauth_client.json",
    token_path: str = "config/token_accounts.json",
):
    """Auto-detect auth mode and return a Sheets API service.

    Prefers OAuth2 (oauth_client.json) over service account.
    """
    oauth_file = Path(oauth_client_path)
    if not oauth_file.is_absolute():
        oauth_file = _PROJ_ROOT / oauth_file

    if oauth_file.is_file():
        print("[gsheet_accounts] Auth mode: OAuth2 (user account)", file=sys.stderr)
        return _build_sheets_service_oauth(str(oauth_file), token_path)

    print("[gsheet_accounts] Auth mode: service account", file=sys.stderr)
    return _build_sheets_service_sa(credentials_path)


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
    """True if this row is a header or totals row (not an account row)."""
    normalized = col_a.strip().lower()
    if normalized in _TOTALS_HEADERS:
        return True
    # e.g. "Total + Fees", "Totals by country"
    for header in _TOTALS_HEADERS:
        if normalized.startswith(header):
            return True
    return False


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

    Columns:
        A (0) — CM name
        B (1) — Country code
        C (2) — Account label
        D (3) — Cost (daily spend)
        E (4) — Leads
        G (6) — Actual FTDs
        I (8) — Stock (asset/offer running)
        J (9) — To Do status
    """
    accounts: List[Dict[str, Any]] = []
    for row in raw_rows:
        # Pad to at least 10 columns so index 9 (col J) is safe
        padded = row + [""] * max(0, 10 - len(row))

        col_a = padded[0].strip()
        col_b = padded[1].strip() if len(padded) > 1 else ""
        col_c = padded[2].strip() if len(padded) > 2 else ""
        col_d = padded[3].strip() if len(padded) > 3 else ""   # Cost
        col_e = padded[4].strip() if len(padded) > 4 else ""   # Leads
        col_g = padded[6].strip() if len(padded) > 6 else ""   # Actual FTDs
        col_i = padded[8].strip() if len(padded) > 8 else ""   # Stock/Asset
        col_j = padded[9].strip() if len(padded) > 9 else ""   # To Do

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
                "cost": col_d,
                "leads": col_e,
                "ftds": col_g,
                "stock": col_i,
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
        # Match by first word of CM name (sheet may store only first name like "Evgeni ")
        cm_first = cm_filter.strip().split()[0].lower() if cm_filter.strip() else ""
        cm_full = cm_filter.strip().lower()
        rows = [
            r for r in rows
            if r["cm"].strip().lower() == cm_full
            or r["cm"].strip().lower().startswith(cm_first)
            or cm_first.startswith(r["cm"].strip().lower())
        ]

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
