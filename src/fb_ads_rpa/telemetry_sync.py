"""Ship this machine's RPA telemetry to one shared Google Sheet.

Why a central copy at all: ACL is being installed on every media buyer's
computer, and the single most expensive class of failure is Meta changing the
Ads Manager UI. On 2026-08-24 one redesign broke four separate steps and cost a
full day of one-at-a-time diagnosis. With many machines reporting, the same
event is unmistakable within the hour — one step's failure rate jumping to 100%
everywhere on the same day is a UI change, not a local problem.

Three deliberate choices:

* **Service account, not the user OAuth used elsewhere.** The existing GSheet
  integrations hold a `spreadsheets.readonly` token obtained through an
  interactive consent screen. Widening that scope would force every existing
  install to re-consent, and interactive consent does not scale to N buyer
  machines at all. A service account with the target sheet shared to it needs
  no human at the keyboard.
* **Never during a run.** Uploading is a separate command. A publish run must
  not gain a network dependency, and telemetry that can delay or break a
  campaign is worse than no telemetry.
* **Disabled until configured.** With no sheet id or key file this module is a
  no-op that reports why. Nothing changes for installs that never set it up.

Config (machine-local — see config.paths.get_local_config_dir):
    GSHEET_TELEMETRY_ID        target spreadsheet id
    GSHEET_TELEMETRY_SHEET     tab name, default "rpa_telemetry"
    GSHEET_TELEMETRY_SA_JSON   service-account key file, default
                               <local config dir>/telemetry_service_account.json
    ACL_MACHINE_LABEL          human name for this machine, default hostname
"""
from __future__ import annotations

import json
import os
import socket
import uuid
from pathlib import Path
from typing import Any

from config.paths import get_local_config_dir
from src.fb_ads_rpa import run_telemetry

_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
_DEFAULT_TAB = "rpa_telemetry"

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
            existing = id_file.read_text(encoding="utf-8").strip()
            if existing:
                return label, existing
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
    sheet_id = (os.environ.get("GSHEET_TELEMETRY_ID") or "").strip()
    key_path = (os.environ.get("GSHEET_TELEMETRY_SA_JSON") or "").strip() or str(
        _local("telemetry_service_account.json")
    )
    return {
        "sheet_id": sheet_id,
        "tab": (os.environ.get("GSHEET_TELEMETRY_SHEET") or "").strip() or _DEFAULT_TAB,
        "key_path": key_path,
        "key_exists": os.path.exists(key_path),
        "configured": bool(sheet_id) and os.path.exists(key_path),
    }


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
            "not configured: set GSHEET_TELEMETRY_ID and put the service-account key at "
            f"{status['key_path']}"
        )
        return result
    try:
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
