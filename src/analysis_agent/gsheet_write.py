"""A Google Sheets client that can WRITE, shared by everything that needs one.

The dashboard's existing Sheets access is read-only: `gsheet_accounts` and
`gsheet_targeting` hold a `spreadsheets.readonly` token. Anything the operator
edits from the dashboard — the shared targeting library, later the account
registry — has to write back, which needs a wider scope.

It deliberately reuses the SAME token file the telemetry uploader created
(`<ACL config>/token_telemetry.json`, scope `spreadsheets`). One browser
consent per machine covers both; a second token file would mean a second
consent for no benefit. The file keeps its original name so existing installs
keep working — read it as "the machine's Sheets write token", not as something
telemetry owns.

Why not widen the existing read-only token instead: that one works today and
the dashboard cannot read anything without it. Re-consenting would take the
dashboard offline until the operator finished a browser flow.
"""
from __future__ import annotations

import os
from pathlib import Path

_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
_TOKEN_FILE = "token_telemetry.json"


def _token_path() -> Path:
    from config.paths import get_local_config_dir

    return Path(get_local_config_dir()) / _TOKEN_FILE


def _oauth_client_path() -> str:
    raw = (os.environ.get("GSHEET_OAUTH_CLIENT") or "config/oauth_client.json").strip()
    if os.path.isabs(raw):
        return raw
    return str(Path(__file__).resolve().parents[2] / raw)


def is_authorised() -> bool:
    """True when a write token already exists — no browser needed.

    Callers use this to decide whether an action is available right now,
    instead of discovering mid-request that a consent screen is required.
    """
    return _token_path().is_file()


def get_write_service():
    """Sheets service authorised to write, as the person using this machine.

    Raises RuntimeError with an actionable message rather than opening a
    browser from inside a web request — a consent window nobody is looking at
    would simply hang the request until it times out.
    """
    import google.auth.transport.requests
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    token_file = _token_path()
    if not token_file.is_file():
        raise RuntimeError(
            "Google write access is not set up on this machine. Run "
            "`python scripts/acl_telemetry_upload.py` once and complete the "
            "browser consent — the dashboard reuses that token."
        )

    creds = Credentials.from_authorized_user_file(str(token_file), _SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(google.auth.transport.requests.Request())
            token_file.write_text(creds.to_json(), encoding="utf-8")
        else:
            raise RuntimeError(
                "The Google write token has expired and cannot refresh. Delete "
                f"{token_file} and run `python scripts/acl_telemetry_upload.py` "
                "to sign in again."
            )
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def resolve_tab(service, sheet_id: str, wanted: str) -> str | None:
    """Actual tab title matching `wanted`, tolerating case/whitespace drift."""
    meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    titles = [s["properties"]["title"] for s in meta.get("sheets", [])]
    return next((t for t in titles if t.strip().lower() == wanted.strip().lower()), None)
