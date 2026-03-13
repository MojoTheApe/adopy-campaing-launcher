#!/usr/bin/env python3
"""
Agent 5: Read accounts from Upluad FB.xlsx, compose Jira tasks (Summary + Description).
By default opens each task in browser for you to paste and publish; optional --api creates via API and writes Link.
"""
from __future__ import annotations

import base64
import os
import re
import sys
import webbrowser
from pathlib import Path

# Ensure project root is on path when run directly
_proj_root = Path(__file__).resolve().parents[2]
if str(_proj_root) not in sys.path:
    sys.path.insert(0, str(_proj_root))

# Load .env from project root so JIRA_EMAIL / JIRA_API_TOKEN need not be set each time
try:
    from dotenv import load_dotenv
    load_dotenv(_proj_root / ".env")
except ImportError:
    pass

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
SHEET_LPS = "LP's"


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
    asset_clean = (asset or "").strip()
    # Avoid "... Lp's for LPs for" — drop trailing " for" / " Lp's for" from asset
    for suffix in (" Lp's for", " LPs for", " for"):
        if asset_clean.endswith(suffix):
            asset_clean = asset_clean[:-len(suffix)].strip() or asset_clean
            break
    # Avoid "Brand - Brand - ..." if Asset already starts with brand
    if asset_clean and asset_clean.upper().startswith((brand + " - ").upper()):
        effective_asset = asset_clean
    else:
        effective_asset = f"{brand} - {asset_clean}" if asset_clean else brand
    account_clean = (account or "").strip()
    # Avoid "LPs for LPs for ..." if Account already starts with that
    # Strip "LPs for " / "Lp's for " prefix if present (any whitespace)
    for prefix in ("LPs for ", "Lp's for ", "LPS FOR ", "lps for "):
        if account_clean.startswith(prefix):
            account_clean = account_clean[len(prefix):].strip() or account_clean
            break
    return f"{effective_asset} LPs for {account_clean} - {country_code} - Media"


def _normalize_asset_for_lookup(asset: str) -> str:
    """Normalize asset string for matching LP's Brand offer (strip trailing ' for' etc.)."""
    s = (asset or "").strip()
    for suffix in (" Lp's for", " LPs for", " for"):
        if s.endswith(suffix):
            s = s[:-len(suffix)].strip() or s
            break
    return s


def _lookup_lps(ws_lps, asset: str) -> tuple[str, str, str]:
    """Look up LP's sheet by Asset (Brand offer). Returns (lp_url, stock, country). Col 1=Brand offer, 4=Lp's, 5=Stock, 3=Country 1."""
    lp_url, stock, country = "", "", ""
    if ws_lps is None or ws_lps.max_row < 2:
        return lp_url, stock, country
    asset_norm = _normalize_asset_for_lookup(asset)
    asset_upper = asset_norm.upper()
    for row in range(2, ws_lps.max_row + 1):
        brand_offer = (ws_lps.cell(row=row, column=1).value or "")
        brand_str = str(brand_offer).strip()
        if not brand_str or not asset_norm:
            continue
        brand_norm = _normalize_asset_for_lookup(brand_str)
        # Match like VLOOKUP: normalized Asset equals normalized Brand offer, or one starts with the other
        if brand_norm.upper() == asset_upper or brand_norm.upper().startswith(asset_upper) or asset_upper.startswith(brand_norm.upper()):
            lp_url = str(ws_lps.cell(row=row, column=4).value or "").strip()
            stock = str(ws_lps.cell(row=row, column=5).value or "").strip()
            country = str(ws_lps.cell(row=row, column=3).value or "").strip()
            break
    return lp_url, stock, country


def _build_description_from_sheets(account: str, domain: str, asset: str, country_code: str, ws_lps) -> str:
    """
    Build Jira Description by repeating the same logic as the three tabs (Accounts, Main, LP's).
    Order and blocks match Main columns 7–30.
    """
    lp_url, stock, country = _lookup_lps(ws_lps, asset)
    if not country and country_code:
        country = country_code
    domain = (domain or "").strip()
    account = (account or "").strip()
    # Replicate Main structure: Scope, LP URL, Requested domain, Account, https://lpa.+domain, steps, Meta title, Default, Feature, Stock name, Country
    parts = [
        "Scope",
        "",
        "Please duplicate this page:",
        lp_url,
        "Requested domain",
        account,
        "https://lpa." + domain if domain else "https://",
        "Step 1: Stepfooter",
        "https://" + domain if domain else "",
        "Meta title: " + (domain or ""),
        "Default",
        "Feature: " + (domain or ""),
        "default GIF Thank you pop up",
        "Stock name: " + stock,
        "Country: " + country,
    ]
    return "\n".join(p for p in parts if p is not None and str(p).strip())


def _description_fallback(account: str, asset: str, domain: str, country_code: str) -> str:
    """Fallback description when no matching Main row."""
    return (
        f"Scope\nPlease duplicate this page.\n"
        f"Account: {account}\n"
        f"Asset: {asset}\n"
        f"Country code: {country_code}\n"
        f"Requested domain: {domain or '—'}\n"
    )


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


def _jira_auth_headers(email: str, token: str) -> dict:
    """Build Authorization header with UTF-8 so .env with any encoding/BOM works."""
    credentials = (email + ":" + token).encode("utf-8")
    b64 = base64.b64encode(credentials).decode("ascii")
    return {"Authorization": "Basic " + b64}


def _get_team_field_id(headers: dict) -> str | None:
    """Resolve Team (migrated) field id from Jira. Returns customfield_XXXXX or None."""
    env_id = (os.environ.get("JIRA_TEAM_FIELD_ID") or "").strip()
    if env_id:
        return env_id
    try:
        import requests
        r = requests.get(
            f"{JIRA_BASE_URL.rstrip('/')}/rest/api/3/field",
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
    token = (os.environ.get("JIRA_API_TOKEN") or "").strip().strip("\ufeff")
    email = (os.environ.get("JIRA_EMAIL") or "").strip().strip("\ufeff")
    if not token or not email:
        print("JIRA_API_TOKEN and JIRA_EMAIL must be set in environment.", file=sys.stderr)
        return None

    try:
        import requests
    except ImportError:
        print("Install requests: pip install requests", file=sys.stderr)
        return None

    url = f"{JIRA_BASE_URL.rstrip('/')}/rest/api/3/issue"
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    headers.update(_jira_auth_headers(email, token))

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
    team_field_id = _get_team_field_id(headers)
    if team_field_id:
        payload["fields"][team_field_id] = {"value": "Media"}

    resp = requests.post(url, json=payload, headers=headers, timeout=30)
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

    if use_api:
        email = (os.environ.get("JIRA_EMAIL") or "").strip()
        token = (os.environ.get("JIRA_API_TOKEN") or "").strip()
        if not email or not token:
            print("For --api you need JIRA_EMAIL and JIRA_API_TOKEN.", file=sys.stderr)
            print("(Check: is .env in the fb_agents folder? Run: python -m pip install python-dotenv)", file=sys.stderr)
            print("  Option 1: create .env in the project folder with:", file=sys.stderr)
            print("    JIRA_EMAIL=your@email.com", file=sys.stderr)
            print("    JIRA_API_TOKEN=your_token", file=sys.stderr)
            print("  Then run: python -m pip install python-dotenv", file=sys.stderr)
            print("  Option 2: in the terminal before running:", file=sys.stderr)
            print('    $env:JIRA_EMAIL="your@email.com"; $env:JIRA_API_TOKEN="token"; python run_jira_from_accounts.py --api', file=sys.stderr)
            return 1
        print("Jira API: creating tasks from Excel...")

    wb = load_workbook(path, read_only=False, keep_vba=True)
    if SHEET_ACCOUNTS not in wb.sheetnames:
        print(f"Sheet '{SHEET_ACCOUNTS}' not found.", file=sys.stderr)
        return 1
    ws_acc = wb[SHEET_ACCOUNTS]
    wb_values = load_workbook(path, read_only=False, data_only=True)
    ws_lps = wb_values[SHEET_LPS] if SHEET_LPS in wb_values.sheetnames else None

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

        description = _build_description_from_sheets(account, domain, asset, country_code, ws_lps)
        if not description.strip():
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

    if use_api and not dry_run:
        if count == 0:
            print("No tasks created. (All rows have Link filled or Asset empty? Run with --dry-run to list rows.)")
        else:
            print(f"Done. Created {count} task(s).")

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
