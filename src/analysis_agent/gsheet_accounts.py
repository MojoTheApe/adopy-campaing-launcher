"""Read account status rows from the daily Google Sheet (last/rightmost tab).

Environment variables (from .env):
    GSHEET_ACCOUNTS_ID      — Google Sheet ID from the spreadsheet URL
    GSHEET_ACCOUNTS_SHEET   — Tab name, or "auto_last" for the **rightmost** tab
                             (by `SheetProperties.index`, i.e. left-to-right order in the UI)
    MY_CM_NAME              — Your CM name as it appears in column A (default: Evgeni Sedin)
    GSHEET_OAUTH_CLIENT     — Optional override for the machine-local OAuth client JSON
    GSHEET_OAUTH_TOKEN      — Optional override for the machine-local saved OAuth token
    GOOGLE_CREDENTIALS_JSON — Path to service-account JSON (fallback if OAuth client not found)

Auth mode (auto-detected):
    If the machine-local OAuth client exists → user login (opens browser on first run).
    Otherwise → service account via GOOGLE_CREDENTIALS_JSON.

Sheet layout (last/today's tab):

    **Legacy tabs** — one row per account:
        Col A — CM name (campaign manager)
        Col B — Country code (e.g. MX, GT, EC, KW) or full country name
        Col C — Account label  (e.g. "FacebookMKTO1211 MX")
        Col J — To Do
        Col K — Optional bundle hints

    **Daily tabs (2026+, e.g. ``05.07``)** — country blocks with header row
    ``CM | Country | Account | … | Stock | Done Today | Cap``:
        Col A — Account bucket: Active / Dead / Issue / New (not CM name)
        Col B — Country code
        Col C — Account label
        Col H — Stock (asset running)
        Col I — Done Today / To Do
        The whole tab is treated as belonging to ``MY_CM_NAME`` (no CM column).

    Rows whose To Do contains **Account Disabled** or **Account Restricted** are excluded
    from the Morning Report "Account Statuses" table (not shown at all).

    The bottom of each tab also contains a totals table whose first cell starts
    with "Country" or "Total" — those rows are skipped automatically.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_PROJ_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJ_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJ_ROOT))

try:
    from config.paths import load_env

    load_env()
except Exception:
    pass

# ── Constants ──────────────────────────────────────────────────────────────────

# Substrings: account not "ready" for open/close optimization (sections 8–9).
_ACCOUNT_NOT_READY_SUBSTRINGS = (
    "account disabled",
    "account restricted",
    "video selfie",
)

# Substrings: omit row entirely from section 6–7 Account Statuses table.
_ACCOUNT_STATUS_OMIT_SUBSTRINGS = (
    "account disabled",
    "account restricted",
)

_TOTALS_HEADERS = {"country", "total", "totals", "cm", "sum"}

# Daily tab (2026+): col A is account bucket (Active/Dead/Issue/New), not CM name.
_ACCOUNT_STATUS_BUCKETS = frozenset({"active", "dead", "issue", "new"})

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
            from config import token_vault

            creds = Credentials.from_authorized_user_info(
                json.loads(token_vault.read_secret_text(token_file)), _SCOPES
            )
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
        from config import token_vault

        token_vault.write_secret_text(token_file, creds.to_json())
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
    oauth_client_path: Optional[str] = None,
    token_path: Optional[str] = None,
):
    """Auto-detect auth mode and return a Sheets API service.

    Prefers the machine-local OAuth2 files over a service account. Optional
    paths exist for focused tests and legacy callers.
    """
    if oauth_client_path is None or token_path is None:
        from config.paths import (
            get_google_oauth_client_path,
            get_google_oauth_token_path,
        )

        oauth_client_path = oauth_client_path or get_google_oauth_client_path()
        token_path = token_path or get_google_oauth_token_path()

    oauth_file = Path(oauth_client_path)
    if not oauth_file.is_absolute():
        oauth_file = _PROJ_ROOT / oauth_file

    if oauth_file.is_file():
        print("[gsheet_accounts] Auth mode: OAuth2 (user account)", file=sys.stderr)
        return _build_sheets_service_oauth(str(oauth_file), token_path)

    print("[gsheet_accounts] Auth mode: service account", file=sys.stderr)
    return _build_sheets_service_sa(credentials_path)


def _select_rightmost_sheet(
    sheets: List[Dict[str, Any]],
) -> Tuple[str, int]:
    """Pick the UI-rightmost tab from `spreadsheets.get` → `sheets` (by max `properties.index`)."""
    if not sheets:
        raise ValueError("No sheets in spreadsheet metadata")
    rightmost = max(
        sheets,
        key=lambda s: int(s.get("properties", {}).get("index", 0)),
    )
    props = rightmost["properties"]
    return str(props["title"]), int(props.get("index", 0))


def _rightmost_tab_title_and_index(service, sheet_id: str) -> Tuple[str, int]:
    """Title and index of the rightmost tab (see `_select_rightmost_sheet`)."""
    meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    return _select_rightmost_sheet(meta.get("sheets", []))


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


def _normalize_todo(t: str) -> str:
    return (t or "").strip().lower()


def _matches_any_substring(normalized: str, substrings: Tuple[str, ...]) -> bool:
    return any(s in normalized for s in substrings)


def is_omitted_from_account_statuses_table(to_do: str) -> bool:
    """True if this account should not appear in the Morning Report section 6–7 table."""
    return _matches_any_substring(_normalize_todo(to_do), _ACCOUNT_STATUS_OMIT_SUBSTRINGS)


def format_account_status_todo_cell(to_do: str, extra: str) -> str:
    """Format col J (+ optional col K) for Account Statuses (Markdown + HTML)."""
    raw = (to_do or "").strip()
    ext = (extra or "").strip()
    if not raw:
        return "—"
    if "video selfie" in raw.lower():
        return raw
    if ext:
        return f"{raw} | {ext}"
    return raw


def _is_ready(to_do: str) -> bool:
    return not _matches_any_substring(_normalize_todo(to_do), _ACCOUNT_NOT_READY_SUBSTRINGS)


def _detect_sheet_layout(raw_rows: List[List[str]]) -> str:
    """Return ``status_v2`` for the Active/Dead/Issue/New daily layout, else ``legacy``."""
    for row in raw_rows[:40]:
        padded = row + [""] * max(0, 11 - len(row))
        if padded[0].strip().lower() != "cm":
            continue
        headers = [str(c or "").strip().lower() for c in padded]
        if any("done today" in h for h in headers):
            return "status_v2"
    return "legacy"


def _is_status_bucket(col_a: str) -> bool:
    return col_a.strip().lower() in _ACCOUNT_STATUS_BUCKETS


def _is_sheet_header_row(col_a: str, col_b: str) -> bool:
    a = col_a.strip().lower()
    b = col_b.strip().lower()
    if a in _TOTALS_HEADERS:
        return True
    return a == "cm" and b in ("country", "")


def parse_account_rows(raw_rows: List[List[str]]) -> List[Dict[str, Any]]:
    """Parse raw sheet rows → list of account dicts.

    Supports two layouts:

    **Legacy** (col A = CM name):
        A CM, B country, C account, D cost, E leads, G FTDs, I stock, J to-do, K extra

    **status_v2** (2026 daily tab — col A = Active/Dead/Issue/New):
        A status bucket, B country, C account, D cost, E leads, G cohort FTDs,
        H stock, I done today / to-do, J cap, K optional extra
    """
    layout = _detect_sheet_layout(raw_rows)
    accounts: List[Dict[str, Any]] = []
    for row in raw_rows:
        # Pad to at least 11 columns so index 10 (col K) is safe
        padded = row + [""] * max(0, 11 - len(row))

        col_a = padded[0].strip()
        col_b = padded[1].strip() if len(padded) > 1 else ""
        col_c = padded[2].strip() if len(padded) > 2 else ""

        if _is_sheet_header_row(col_a, col_b):
            continue
        if not col_a or not col_c:
            continue
        if _is_totals_row(col_a):
            continue
        if layout == "status_v2" and not _is_status_bucket(col_a):
            # Country subtotal rows reuse col A for ISO code (EC, KW, …).
            continue

        if layout == "status_v2" or _is_status_bucket(col_a):
            col_d = padded[3].strip() if len(padded) > 3 else ""
            col_e = padded[4].strip() if len(padded) > 4 else ""
            col_g = padded[6].strip() if len(padded) > 6 else ""
            col_i = padded[7].strip() if len(padded) > 7 else ""   # Stock
            col_j = padded[8].strip() if len(padded) > 8 else ""   # Done Today / To Do
            col_k = padded[10].strip() if len(padded) > 10 else ""
            cm_name = ""
            account_status = col_a
        else:
            col_d = padded[3].strip() if len(padded) > 3 else ""
            col_e = padded[4].strip() if len(padded) > 4 else ""
            col_g = padded[6].strip() if len(padded) > 6 else ""
            col_i = padded[8].strip() if len(padded) > 8 else ""
            col_j = padded[9].strip() if len(padded) > 9 else ""
            col_k = padded[10].strip() if len(padded) > 10 else ""
            cm_name = col_a
            account_status = ""

        alias = _account_alias_from_label(col_c)
        accounts.append(
            {
                "cm": cm_name,
                "account_status": account_status,
                "country_code": col_b,
                "account_label": col_c,
                "alias": alias,
                "cost": col_d,
                "leads": col_e,
                "ftds": col_g,
                "stock": col_i,
                "to_do": col_j,
                "to_do_extra": col_k,
                "is_ready": _is_ready(col_j),
            }
        )
    return accounts


def _apply_cm_filter(
    rows: List[Dict[str, Any]],
    cm_filter: str,
    layout: str,
) -> List[Dict[str, Any]]:
    """Filter rows to one CM; status_v2 tabs have no CM column — keep all rows."""
    if not cm_filter:
        return rows

    cm_first = cm_filter.strip().split()[0].lower() if cm_filter.strip() else ""
    cm_full = cm_filter.strip().lower()

    def _matches(row: Dict[str, Any]) -> bool:
        cm = row.get("cm", "").strip().lower()
        if not cm:
            return False
        return (
            cm == cm_full
            or cm.startswith(cm_first)
            or cm_first.startswith(cm)
        )

    filtered = [r for r in rows if _matches(r)]
    if filtered:
        return filtered

    # New daily sheet: col A is Active/Dead/Issue/New — whole tab belongs to MY_CM_NAME.
    if layout == "status_v2" or any(r.get("account_status") for r in rows):
        for row in rows:
            row["cm"] = cm_filter
        print(
            f"[gsheet_accounts] Tab uses status buckets (Active/Dead/…) — "
            f"assigned all {len(rows)} rows to CM {cm_filter!r}",
            file=sys.stderr,
        )
        return rows

    return filtered


# ── Public API ─────────────────────────────────────────────────────────────────

DEFAULT_ACCOUNTS_SHEET_ID = "1BSQvO219ybFn0zKiT5T6ayfwy0AuIrzKh_RCXDit3Es"


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
        List of dicts with keys: cm, country_code, account_label, alias, cost,
        leads, ftds, stock, to_do, to_do_extra (col K, optional),
        is_ready.
        Returns empty list on any error (caller decides whether to continue without GSheet).
    """
    # The company's accounts spreadsheet is the SAME sheet for every campaign
    # manager, so asking each of them to paste its id during setup only creates
    # a way to get it wrong. Same precedent as the targeting sheet, which has
    # carried its id in code since the beginning. Access is still decided by
    # Google permissions on the person's own account — an id is not a key.
    #
    # GSHEET_ACCOUNTS_ID still overrides, for anyone pointing at a copy.
    sid = sheet_id or os.environ.get("GSHEET_ACCOUNTS_ID", "").strip() or DEFAULT_ACCOUNTS_SHEET_ID
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

    from config.paths import get_google_oauth_client_path, get_google_oauth_token_path

    service = _build_sheets_service(
        creds,
        get_google_oauth_client_path(),
        get_google_oauth_token_path(),
    )
    if service is None:
        return []

    try:
        if sname.lower() == "auto_last":
            sname, tab_index = _rightmost_tab_title_and_index(service, sid)
            print(
                f"[gsheet_accounts] Rightmost tab: {sname!r} (sheet index={tab_index})",
                file=sys.stderr,
            )

        # Read columns A:K (incl. optional col K for §6–7 To Do extensions)
        raw = _fetch_range(service, sid, sname, "A:K")
        print(
            f"[gsheet_accounts] Fetched {len(raw)} rows from tab {sname!r}",
            file=sys.stderr,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[gsheet_accounts] ERROR reading sheet: {exc}", file=sys.stderr)
        return []

    rows = parse_account_rows(raw)
    layout = _detect_sheet_layout(raw)
    rows = _apply_cm_filter(rows, cm_filter, layout)

    print(
        f"[gsheet_accounts] Parsed {len(rows)} account rows"
        + (f" for CM '{cm_filter}'" if cm_filter else "")
        + (f" (layout={layout})" if layout != "legacy" else ""),
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
