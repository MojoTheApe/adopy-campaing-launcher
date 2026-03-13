#!/usr/bin/env python3
"""
Agent 5: Read accounts from Upluad FB.xlsx, compose Jira tasks (Summary + Description).
By default opens each task in browser for you to paste and publish; optional --api creates via API and writes Link.
"""
from __future__ import annotations

import os
import re
import sys
import webbrowser
from pathlib import Path

# Ensure project root is on path when run directly
_proj_root = Path(__file__).resolve().parents[2]
if str(_proj_root) not in sys.path:
    sys.path.insert(0, str(_proj_root))

from openpyxl import load_workbook

from config.paths import get_upload_fb_excel_path

# Jira Cloud
JIRA_BASE_URL = "https://mar-adopy.atlassian.net"
JIRA_PROJECT_KEY = "MAR"
# URL страницы создания новой задачи (пустая форма). Если у тебя сначала открывается другая страница и нужно жать Next — открой вручную пустую форму создания, скопируй адрес из браузера и задай переменную окружения JIRA_CREATE_ISSUE_URL.
def _get_create_issue_url() -> str:
    return (os.environ.get("JIRA_CREATE_ISSUE_URL") or "").strip() or f"{JIRA_BASE_URL.rstrip('/')}/secure/CreateIssue!default.jspa"
# Country code: only Colombia (CO) uses Suxxess FX, others use Algobi
COUNTRY_CODES = ("CO", "EC", "GT", "MX", "CL", "PE", "AR", "BR", "CR", "DO", "PA", "UY", "BO", "PY", "SV", "HN", "NI")
# Column names in Excel (Accounts sheet)
ACCOUNTS_ACCOUNT = "Account"
ACCOUNTS_ASSET = "Asset"
ACCOUNTS_LINK = "Link"
ACCOUNTS_DOMAIN = "Domain"
SHEET_ACCOUNTS = "Accounts"
SHEET_MAIN = "Main"
# Description: columns in Main to concatenate (1-based); if empty, use template from Accounts
MAIN_DESC_START_COL = 6
MAIN_DESC_END_COL = 30


def _col_index_by_header(ws, header_name: str, row: int = 1) -> int | None:
    """Return 1-based column index where row has header_name, or None."""
    for col in range(1, ws.max_column + 1):
        val = ws.cell(row=row, column=col).value
        if val is None:
            continue
        if str(val).strip().lower() == header_name.strip().lower():
            return col
    return None


def _extract_country_code(account_str: str) -> str:
    """
    Extract two-letter country code from account name (e.g. 'FacebookUL574 EC Fav' -> 'EC').
    Uses first occurrence of a known code as a whole word (so 'CO' in 'Banco' is ignored).
    """
    if not account_str:
        return "XX"
    s = str(account_str).upper()
    for code in COUNTRY_CODES:
        if re.search(r"\b" + re.escape(code) + r"\b", s):
            return code
    # Fallback: two consecutive uppercase letters as word (e.g. EC, GT)
    m = re.search(r"\b([A-Z]{2})\b", s)
    return m.group(1) if m else "XX"


def _brand_for_country(country_code: str) -> str:
    """Colombia (CO) -> Suxxess FX, else Algobi."""
    return "Suxxess FX" if (country_code or "").upper() == "CO" else "Algobi"


def _build_summary(account: str, asset: str, country_code: str) -> str:
    brand = _brand_for_country(country_code)
    return f"{brand} - {asset} LPs for {account} - {country_code} - Media"


def _description_from_main_row(ws_main, data_row: int, start_col: int = MAIN_DESC_START_COL, end_col: int = MAIN_DESC_END_COL) -> str:
    """Build description by concatenating cells in Main row from start_col to end_col."""
    parts = []
    for col in range(start_col, min(end_col + 1, ws_main.max_column + 1)):
        val = ws_main.cell(row=data_row, column=col).value
        if val is not None and str(val).strip():
            parts.append(str(val).strip())
    return "\n\n".join(parts) if parts else ""


def _description_fallback(account: str, asset: str, domain: str, country_code: str) -> str:
    """Fallback description when no matching Main row."""
    return (
        f"Scope\n\nPlease duplicate this page.\n\n"
        f"Account: {account}\n"
        f"Asset: {asset}\n"
        f"Country code: {country_code}\n"
        f"Requested domain: {domain or '—'}\n"
    )


def _main_account_col(ws_main) -> int | None:
    """Find 1-based column index of Account in Main (row 1)."""
    return _col_index_by_header(ws_main, ACCOUNTS_ACCOUNT) or _col_index_by_header(ws_main, "Account name") or 1


def _find_main_row_for_account(ws_main, account: str) -> int | None:
    """Return 1-based row index in Main where first column (or Account column) equals account, else None."""
    account_col = _main_account_col(ws_main)
    for row in range(2, ws_main.max_row + 1):
        val = ws_main.cell(row=row, column=account_col).value
        if val is not None and str(val).strip() == str(account).strip():
            return row
    return None


def _jira_description_to_adf(text: str) -> dict:
    """Convert plain text to Jira ADF (Atlassian Document Format) for description field."""
    if not text:
        return {"type": "doc", "version": 1, "content": []}
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    content = []
    for line in lines:
        content.append({
            "type": "paragraph",
            "content": [{"type": "text", "text": line or " "}],
        })
    if not content:
        content = [{"type": "paragraph", "content": [{"type": "text", "text": " "}]}]
    return {"type": "doc", "version": 1, "content": content}


def _get_team_field_id(auth: tuple, headers: dict) -> str | None:
    """Resolve Team (migrated) field id from Jira. Returns customfield_XXXXX or None."""
    env_id = (os.environ.get("JIRA_TEAM_FIELD_ID") or "").strip()
    if env_id:
        return env_id
    try:
        import requests
        r = requests.get(
            f"{JIRA_BASE_URL.rstrip('/')}/rest/api/3/field",
            auth=auth,
            headers=headers,
            timeout=15,
        )
        if r.status_code != 200:
            return None
        for f in r.json():
            name = (f.get("name") or "").strip()
            if "team" in name.lower() and "migrated" in name.lower():
                return f.get("id")
            if name == "Team":
                return f.get("id")
        return None
    except Exception:
        return None


def _create_jira_issue(summary: str, description: str, brand: str) -> str | None:
    """
    Create Jira issue via REST API. Returns issue key (e.g. MAR-38467) or None on failure.
    Uses JIRA_EMAIL and JIRA_API_TOKEN from environment.
    """
    token = (os.environ.get("JIRA_API_TOKEN") or "").strip()
    email = (os.environ.get("JIRA_EMAIL") or "").strip()
    if not token or not email:
        print("JIRA_API_TOKEN and JIRA_EMAIL must be set in environment.", file=sys.stderr)
        return None

    try:
        import requests
    except ImportError:
        print("Install requests: pip install requests", file=sys.stderr)
        return None

    url = f"{JIRA_BASE_URL.rstrip('/')}/rest/api/3/issue"
    auth = (email, token)
    headers = {"Accept": "application/json", "Content-Type": "application/json"}

    payload = {
        "fields": {
            "project": {"key": JIRA_PROJECT_KEY},
            "summary": summary,
            "description": _jira_description_to_adf(description),
            "issuetype": {"name": "Task"},
            "labels": ["Limited_QA"],
        }
    }
    # Team (migrated) = Media. Resolve field id from API or JIRA_TEAM_FIELD_ID env.
    team_field_id = _get_team_field_id(auth, headers)
    if team_field_id:
        payload["fields"][team_field_id] = {"value": "Media"}

    resp = requests.post(url, json=payload, auth=auth, headers=headers, timeout=30)
    if resp.status_code != 201:
        print(f"Jira API error {resp.status_code}: {resp.text}", file=sys.stderr)
        return None
    data = resp.json()
    return data.get("key")


def _issue_url(key: str) -> str:
    return f"{JIRA_BASE_URL.rstrip('/')}/browse/{key}"


def _copy_to_clipboard(text: str) -> bool:
    """Copy text to clipboard. Tries pyperclip, then on Windows uses PowerShell."""
    if not text:
        return False
    try:
        import pyperclip
        pyperclip.copy(text)
        return True
    except Exception:
        pass
    if sys.platform == "win32":
        try:
            import subprocess
            import tempfile
            with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8") as f:
                f.write(text)
                path = f.name
            try:
                # Escape path for PowerShell (single quotes for literal)
                path_esc = path.replace("'", "''")
                proc = subprocess.run(
                    ["powershell", "-NoProfile", "-Command", f"Get-Content -Raw -LiteralPath '{path_esc}' | Set-Clipboard"],
                    capture_output=True,
                    timeout=10,
                )
                if proc.returncode == 0:
                    return True
            finally:
                try:
                    os.unlink(path)
                except Exception:
                    pass
        except Exception:
            pass
    return False


def run(
    excel_path: str | None = None,
    dry_run: bool = False,
    use_api: bool = False,
    pause_between: bool = False,
) -> int:
    """
    Read Accounts from Excel; for each row (empty Link, non-empty Asset) compose Summary + Description.
    - Default (use_api=False): open one Jira create-issue tab per account (e.g. 5 accounts → 5 tabs), copy Description to clipboard for the last one; you paste and publish in each tab.
    - With --api: create issues via Jira API (Excel не изменяется, ссылки в док не вписываются).
    Returns 0 on success, 1 on error.
    """
    path = excel_path or get_upload_fb_excel_path()
    if not os.path.isfile(path):
        print(f"Excel file not found: {path}", file=sys.stderr)
        return 1

    wb = load_workbook(path, read_only=False, keep_vba=True)
    if SHEET_ACCOUNTS not in wb.sheetnames:
        print(f"Sheet '{SHEET_ACCOUNTS}' not found.", file=sys.stderr)
        return 1
    ws_acc = wb[SHEET_ACCOUNTS]
    # Main: read with data_only=True so we get computed values (text), not formulas (=VLOOKUP etc.)
    wb_values = load_workbook(path, read_only=False, data_only=True)
    ws_main = wb_values[SHEET_MAIN] if SHEET_MAIN in wb_values.sheetnames else (wb[SHEET_MAIN] if SHEET_MAIN in wb.sheetnames else None)

    col_account = _col_index_by_header(ws_acc, ACCOUNTS_ACCOUNT)
    col_asset = _col_index_by_header(ws_acc, ACCOUNTS_ASSET)
    col_link = _col_index_by_header(ws_acc, ACCOUNTS_LINK)
    col_domain = _col_index_by_header(ws_acc, ACCOUNTS_DOMAIN)

    if not col_account or not col_asset:
        print("Accounts sheet must have 'Account' and 'Asset' columns.", file=sys.stderr)
        return 1
    count = 0
    for row in range(2, ws_acc.max_row + 1):
        account_val = ws_acc.cell(row=row, column=col_account).value
        asset_val = ws_acc.cell(row=row, column=col_asset).value
        link_val = ws_acc.cell(row=row, column=col_link).value if col_link else None

        if not account_val or not str(account_val).strip():
            continue
        if not asset_val or not str(asset_val).strip():
            continue
        if link_val is not None and str(link_val).strip():
            continue

        account = str(account_val).strip()
        asset = str(asset_val).strip()
        domain = ""
        if col_domain:
            d = ws_acc.cell(row=row, column=col_domain).value
            domain = str(d).strip() if d is not None else ""

        country_code = _extract_country_code(account)
        summary = _build_summary(account, asset, country_code)
        brand = _brand_for_country(country_code)

        if ws_main:
            main_row = _find_main_row_for_account(ws_main, account)
            if main_row is not None:
                description = _description_from_main_row(ws_main, main_row)
            else:
                description = _description_fallback(account, asset, domain, country_code)
        else:
            description = _description_fallback(account, asset, domain, country_code)

        if dry_run:
            print(f"[DRY RUN] {summary}")
            count += 1
            continue

        if use_api:
            key = _create_jira_issue(summary, description, brand)
            if not key:
                print(f"Failed to create Jira issue for account: {account}", file=sys.stderr)
                continue
            url = _issue_url(key)
            print(f"Created {key}: {url}")
            webbrowser.open(url)  # open task in browser so you can review and press the button yourself
            count += 1
        else:
            # Browser mode: copy to clipboard and save to file (fallback), then open tab
            print("\n" + "=" * 60)
            ok_sum = _copy_to_clipboard(summary)
            if ok_sum:
                print("Summary copied to clipboard - paste (Ctrl+V) in Jira title.")
            else:
                print("Summary (copy manually to Jira title):")
                print(summary)
            print("=" * 60)
            # Save to file so you can copy from there if clipboard fails
            _out_dir = _proj_root / "output"
            _out_dir.mkdir(exist_ok=True)
            _task_file = _out_dir / "jira_task_paste.txt"
            try:
                with open(_task_file, "w", encoding="utf-8") as f:
                    f.write("=== Paste into Jira title (Summary) ===\n\n")
                    f.write(summary)
                    f.write("\n\n=== Paste into Jira body (Description) ===\n\n")
                    f.write(description)
                print(f"Text also saved to: {_task_file}")
                print("If nothing pasted in Jira - open this file and copy from there.")
            except Exception as e:
                print(f"Could not save to file: {e}")
            webbrowser.open(_get_create_issue_url())
            ok_desc = _copy_to_clipboard(description)
            if ok_desc:
                print("Description copied to clipboard - paste (Ctrl+V) in Jira body.")
            else:
                print("Description (copy from file above or from output below):")
                print("-" * 40)
                print(description[:3000] + ("..." if len(description) > 3000 else ""))
            count += 1
            if pause_between:
                try:
                    input("Press Enter for next account (or Ctrl+C to stop)...")
                except KeyboardInterrupt:
                    print("\nStopped.")
                    break

    if dry_run and count > 0:
        print(f"[DRY RUN] {count} task(s).")

    return 0


def main() -> None:
    import argparse
    p = argparse.ArgumentParser(
        description="Agent 5: Compose Jira tasks from Upluad FB.xlsx. Default: open in browser for you to paste and publish."
    )
    p.add_argument("--excel", "-e", help="Path to Upluad FB.xlsx (default: config/env)")
    p.add_argument("--dry-run", action="store_true", help="Only list tasks, do not open browser or call API")
    p.add_argument("--api", action="store_true", help="Create issues via Jira API (Excel не меняется; нужны JIRA_EMAIL + JIRA_API_TOKEN)")
    p.add_argument("--pause", action="store_true", help="Pause after each account (wait Enter before opening next tab); default: open all tabs at once")
    args = p.parse_args()
    sys.exit(run(
        excel_path=args.excel,
        dry_run=args.dry_run,
        use_api=args.api,
        pause_between=args.pause,
    ))


if __name__ == "__main__":
    main()
