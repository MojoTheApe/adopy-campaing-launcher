"""Ship this machine's RPA telemetry to one shared Google Sheet.

Why a central copy at all: ACL is being installed on every media buyer's
computer, and the single most expensive class of failure is Meta changing the
Ads Manager UI. On 2026-08-24 one redesign broke four separate steps and cost a
full day of one-at-a-time diagnosis. With many machines reporting, the same
event is unmistakable within the hour — one step's failure rate jumping to 100%
everywhere on the same day is a UI change, not a local problem.

**Transport: user OAuth**, after Adopy's Workspace policy closed both of the
routes that need no human. The service account could not be given access —
"prohibited ... not a Google Account in a compatible allowlisted domain" — and
an Apps Script web app cannot be deployed for "Anyone", only "Only myself" or
"Anyone within Adopy", while a buyer's machine posts anonymously. Since the
sheet is shared company-wide, any Adopy account may write to it, so each
machine authorises as the person using it: one browser consent at install
time, the same kind of one-off step as entering the Multilogin token. Both
other transports are still implemented and pick up automatically if the policy
ever changes.

Two more deliberate choices:

* **Never during a run.** Uploading is a separate command. A publish run must
  not gain a network dependency, and telemetry that can delay or break a
  campaign is worse than no telemetry.
* **Disabled until configured.** With no sheet id this module is a no-op that
  reports why. Nothing changes for installs that never set it up.

Config (machine-local — see config.paths.get_local_config_dir):
    GSHEET_TELEMETRY_ID        target spreadsheet id — the only required one
    GSHEET_TELEMETRY_SHEET     tab name, default "rpa_telemetry"
    GSHEET_OAUTH_CLIENT        OAuth client file, shared with the dashboard
    ACL_MACHINE_LABEL          human name for this machine, default hostname
    ACL_TELEMETRY_WEBHOOK_URL  optional Apps Script sink, if ever permitted
    GSHEET_TELEMETRY_SA_JSON   optional service-account key, if ever permitted

The write token lives in <local config dir>/token_telemetry.json, separate
from the dashboard's read-only token so widening scope here cannot cost the
dashboard its access.
"""
from __future__ import annotations

import json
import os
import re
import socket
import uuid
from pathlib import Path
from typing import Any

from config.paths import get_local_config_dir
from src.fb_ads_rpa import run_telemetry

_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
_DEFAULT_TAB = "rpa_telemetry"
_TOKEN_FILE = "token_telemetry.json"

# Column order is part of the sheet's contract — append new columns at the END
# so existing rows keep their meaning.
HEADER = [
    "ts",
    "machine",
    "machine_id",
    "flow_version_at_upload",
    "run_id",
    "campaign",
    "step",
    "ok",
    "duration_s",
    "diag",
    "note",
]


def _local(name: str) -> Path:
    return Path(get_local_config_dir()) / name


def machine_identity() -> tuple[str, str]:
    """(label, machine_id) — who this data came from.

    The id is generated once and kept next to the machine's other local config,
    so a buyer's rows stay attributable across reinstalls of the checkout even
    if they rename the machine.
    """
    label = (os.environ.get("ACL_MACHINE_LABEL") or "").strip() or socket.gethostname()
    id_file = _local("machine_id.txt")
    try:
        if id_file.is_file():
            # First line only, and only if it still looks like an id. This file
            # sits in a folder people are told to drop things into, and on
            # 2026-08-26 a service-account address ended up pasted onto a second
            # line — which then travelled into 2584 uploaded rows as part of the
            # machine id. Trust the file's shape, not its mere existence.
            first = id_file.read_text(encoding="utf-8").strip().splitlines()
            candidate = first[0].strip() if first else ""
            if re.fullmatch(r"[0-9a-f]{6,32}", candidate):
                return label, candidate
        new_id = uuid.uuid4().hex[:12]
        id_file.parent.mkdir(parents=True, exist_ok=True)
        id_file.write_text(new_id, encoding="utf-8")
        return label, new_id
    except Exception:
        return label, "unknown"


def _cursor_file() -> Path:
    return _local("telemetry_cursor.json")


def read_cursor() -> int:
    """How many telemetry records this machine has already shipped."""
    try:
        data = json.loads(_cursor_file().read_text(encoding="utf-8"))
        return int(data.get("uploaded", 0))
    except Exception:
        return 0


def write_cursor(n: int) -> None:
    try:
        f = _cursor_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"uploaded": int(n)}), encoding="utf-8")
    except Exception:
        pass


def _flow_version() -> str:
    try:
        from src.fb_ads_rpa.ads_manager_flow import RPA_FLOW_VERSION

        return RPA_FLOW_VERSION
    except Exception:
        return ""


def to_row(rec: dict[str, Any], label: str, machine_id: str, version: str) -> list[str]:
    diag = rec.get("diag")
    ok = rec.get("ok")
    return [
        str(rec.get("ts", "")),
        label,
        machine_id,
        version,
        str(rec.get("run_id", "")),
        str(rec.get("campaign", "")),
        str(rec.get("step", "")),
        "" if ok is None else ("1" if ok else "0"),
        "" if rec.get("duration_s") is None else str(rec.get("duration_s")),
        json.dumps(diag, ensure_ascii=False)[:900] if diag else "",
        str(rec.get("note", "")),
    ]


def pending(limit: int | None = None, *, since: int | None = None) -> tuple[list[list[str]], int]:
    """(rows ready to send, index they end at)."""
    records = run_telemetry.load()
    start = read_cursor() if since is None else since
    start = max(0, min(start, len(records)))
    slice_ = records[start:]
    if limit is not None:
        slice_ = slice_[:limit]
    label, machine_id = machine_identity()
    version = _flow_version()
    return [to_row(r, label, machine_id, version) for r in slice_], start + len(slice_)


def config_status() -> dict[str, Any]:
    """Which transport (if any) this machine can use, in preference order.

    * **oauth** — writes as the Adopy account using this machine. The only one
      the Workspace policy actually permits, so it wins whenever available.
    * **webhook** — Apps Script bound to the sheet. Blocked here: the "Anyone"
      access level is not offered, and buyer machines post anonymously.
    * **service_account** — the textbook route. Blocked here: the sheet cannot
      be shared with a non-allowlisted domain.

    The blocked two are kept wired up rather than deleted — either becomes the
    better option the moment IT allowlists the domain or the sheet moves.
    """
    sheet_id = (os.environ.get("GSHEET_TELEMETRY_ID") or "").strip()
    key_path = (os.environ.get("GSHEET_TELEMETRY_SA_JSON") or "").strip() or str(
        _local("telemetry_service_account.json")
    )
    webhook = (os.environ.get("ACL_TELEMETRY_WEBHOOK_URL") or "").strip()
    client_path = _oauth_client_path()
    oauth_ready = bool(sheet_id) and os.path.exists(client_path)
    sa_ready = bool(sheet_id) and os.path.exists(key_path)
    # oauth first: it is the one Adopy's policy actually permits.
    transport = (
        "oauth" if oauth_ready else ("webhook" if webhook else ("service_account" if sa_ready else ""))
    )
    return {
        "sheet_id": sheet_id,
        "tab": (os.environ.get("GSHEET_TELEMETRY_SHEET") or "").strip() or _DEFAULT_TAB,
        "key_path": key_path,
        "key_exists": os.path.exists(key_path),
        "webhook": webhook,
        "oauth_client": client_path,
        "oauth_client_exists": os.path.exists(client_path),
        "oauth_token": str(_local(_TOKEN_FILE)),
        "oauth_authorised": os.path.exists(_local(_TOKEN_FILE)),
        "transport": transport,
        "configured": bool(transport),
    }


def _oauth_client_path() -> str:
    raw = (os.environ.get("GSHEET_OAUTH_CLIENT") or "config/oauth_client.json").strip()
    if os.path.isabs(raw):
        return raw
    return str(Path(__file__).resolve().parents[2] / raw)


def _service_oauth(sheet_id: str):
    """Sheets client authorised as the person sitting at this machine.

    This is the transport Adopy's Workspace actually allows. Both alternatives
    were closed by policy on 2026-08-25/26:
      * service account — "prohibited ... not a Google Account in a compatible
        allowlisted domain", so the sheet cannot be shared with it;
      * Apps Script web app — the "Anyone" access level is not offered, only
        "Only myself" / "Anyone within Adopy", and a buyer's machine posts
        anonymously.
    Since the sheet is already shared company-wide, any Adopy account may write
    to it — so we authorise as one. Costs one browser consent per machine, at
    install time, which is the same kind of one-off step as entering the
    Multilogin token.

    Deliberately a SEPARATE token file from the dashboard's
    config/token_accounts.json: that one is read-only and works today, and
    widening its scope would force a re-consent before the dashboard could
    read anything again.
    """
    import google.auth.transport.requests
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    token_file = _local(_TOKEN_FILE)
    creds = None
    if token_file.is_file():
        try:
            creds = Credentials.from_authorized_user_file(str(token_file), _SCOPES)
        except Exception:
            creds = None

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(google.auth.transport.requests.Request())
            except Exception:
                creds = None
        if not creds:
            client_path = _oauth_client_path()
            if not os.path.exists(client_path):
                raise RuntimeError(f"OAuth client file not found: {client_path}")
            flow = InstalledAppFlow.from_client_secrets_file(client_path, _SCOPES)
            creds = flow.run_local_server(port=0)
        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text(creds.to_json(), encoding="utf-8")

    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def _service(key_path: str):
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build

    creds = Credentials.from_service_account_file(key_path, scopes=_SCOPES)
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def _ensure_tab(service, sheet_id: str, tab: str) -> None:
    """Create the tab with a header row if it isn't there yet."""
    meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    names = [s["properties"]["title"] for s in meta.get("sheets", [])]
    if tab in names:
        return
    service.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id,
        body={"requests": [{"addSheet": {"properties": {"title": tab}}}]},
    ).execute()
    service.spreadsheets().values().append(
        spreadsheetId=sheet_id,
        range=f"{tab}!A1",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": [HEADER]},
    ).execute()


def _send_webhook(url: str, tab: str, rows: list[list[str]]) -> None:
    """POST rows to the sheet's own Apps Script. Raises on any failure.

    Chunked: Apps Script caps both request size and execution time, and one
    machine's backlog on first upload is thousands of rows. A partial batch
    that fails leaves the cursor where it was, so the next run resends it —
    duplicates are preferable to silent gaps, and the cursor only advances
    once every chunk is acknowledged.
    """
    import urllib.request

    token = (os.environ.get("ACL_TELEMETRY_WEBHOOK_TOKEN") or "").strip()
    chunk = 400
    for i in range(0, len(rows), chunk):
        payload = {
            "token": token,
            "tab": tab,
            "header": HEADER,
            "rows": rows[i : i + chunk],
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = resp.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(body)
        except Exception:
            # Apps Script answers HTML when the deployment is misconfigured
            # (most often "who has access" left as restricted) — say so plainly
            # instead of reporting a JSON parse error.
            raise RuntimeError(
                "webhook did not return JSON — check the Apps Script deployment "
                f"is set to run as you and be accessible to Anyone. First bytes: {body[:120]}"
            ) from None
        if not parsed.get("ok"):
            raise RuntimeError(f"webhook refused: {parsed.get('error', body[:120])}")


def upload(*, limit: int | None = 2000, dry_run: bool = False) -> dict[str, Any]:
    """Send un-shipped records. Never raises — returns what happened."""
    status = config_status()
    rows, end = pending(limit)
    result: dict[str, Any] = {
        "sent": 0,
        "pending": len(rows),
        "cursor_before": read_cursor(),
        "cursor_after": read_cursor(),
        "configured": status["configured"],
        "error": "",
    }
    if not rows:
        return result
    if dry_run:
        return result
    if not status["configured"]:
        result["error"] = (
            "not configured: set ACL_TELEMETRY_WEBHOOK_URL (Apps Script inside the sheet), "
            "or GSHEET_TELEMETRY_ID plus a service-account key at "
            f"{status['key_path']}"
        )
        return result
    try:
        if status["transport"] == "oauth":
            service = _service_oauth(status["sheet_id"])
            _ensure_tab(service, status["sheet_id"], status["tab"])
            service.spreadsheets().values().append(
                spreadsheetId=status["sheet_id"],
                range=f"{status['tab']}!A1",
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body={"values": rows},
            ).execute()
        elif status["transport"] == "webhook":
            _send_webhook(status["webhook"], status["tab"], rows)
        else:
            service = _service(status["key_path"])
            _ensure_tab(service, status["sheet_id"], status["tab"])
            service.spreadsheets().values().append(
                spreadsheetId=status["sheet_id"],
                range=f"{status['tab']}!A1",
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body={"values": rows},
            ).execute()
    except Exception as exc:  # noqa: BLE001 - reported, never raised
        result["error"] = f"{type(exc).__name__}: {exc}"[:300]
        return result
    # Advance the cursor only after the write is acknowledged: a failed upload
    # must be retried, not silently skipped.
    write_cursor(end)
    result["sent"] = len(rows)
    result["cursor_after"] = end
    return result
