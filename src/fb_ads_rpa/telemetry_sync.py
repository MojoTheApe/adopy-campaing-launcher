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


_VERSION_CACHE: str | None = None


def _git(*args: str) -> str:
    """One git command in the checkout, or "" — never raises, never blocks long.

    Popen flags keep a console window from flashing on a buyer's screen every
    time the launcher starts.
    """
    import subprocess

    root = Path(__file__).resolve().parents[2]
    kwargs: dict[str, Any] = {}
    if hasattr(subprocess, "CREATE_NO_WINDOW"):  # Windows
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        out = subprocess.run(
            ["git", *args],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=5,
            **kwargs,
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


def flow_version() -> str:
    """What code this run actually executed — not just what it calls itself.

    RPA_FLOW_VERSION is a hand-written label: it says which flow logic is
    intended, and it deliberately does not move on every commit. That makes it
    useless on its own for telling two installs apart. On 2026-08-29 the
    constant read "v96" while the tag rpa-v96-spend-confirmed-2026-08-24 was
    33 commits behind HEAD, 8 of them in RPA code — two different things both
    calling themselves v96.

    That is survivable with one machine and a git prompt. With ACL on ten
    buyers' machines, telemetry is the only channel back, and ten reports all
    reading "v96" cannot be compared: one buyer has not launched in a month,
    one updated last night, and one had an update rolled back by the check in
    start_dashboard.bat. The commit hash is what makes their reports
    comparable, and it is the one thing a person cannot mistype.

    Returns e.g. "v96+8@22aea04" — intended flow, commits touching RPA since
    the last tagged-good point, actual commit. A trailing "*" means the
    checkout has uncommitted edits, i.e. a developer machine, not a buyer's.

    Falls back to the bare label when git is unavailable: a buyer may well be
    handed a folder rather than a clone, and a version string is not worth an
    exception on the way to a run. Computed once per process.
    """
    global _VERSION_CACHE
    if _VERSION_CACHE is not None:
        return _VERSION_CACHE

    try:
        from src.fb_ads_rpa.ads_manager_flow import RPA_FLOW_VERSION

        base = RPA_FLOW_VERSION
    except Exception:
        base = ""

    from src.packaged_runtime import build_identity
    packaged = build_identity()
    if packaged:
        _VERSION_CACHE = f"{base or '?'}@{packaged['short']}"
        return _VERSION_CACHE

    head = _git("rev-parse", "--short", "HEAD")
    if not head:
        _VERSION_CACHE = base
        return _VERSION_CACHE

    version = base or "?"
    tag = _git("describe", "--tags", "--abbrev=0")
    if tag:
        # Scoped to the RPA tree on purpose: this string describes the flow,
        # so dashboard-only commits should not inflate it.
        ahead = _git("rev-list", "--count", f"{tag}..HEAD", "--", "src/fb_ads_rpa")
        if ahead and ahead != "0":
            version = f"{version}+{ahead}"
    version = f"{version}@{head}"
    # Local edits to CODE, which is the only thing the flag is meant to say.
    #
    # The scope was originally a workaround: runs rewrote tracked files in
    # output/, so a plain dirty check raised the flag on every machine and meant
    # nothing. Those files left git on 2026-08-31, so the workaround is no
    # longer needed — but the scope stays, because "the code differs" is a
    # sharper statement than "something in the folder differs", and it is what
    # the flag claims.
    if _git(
        "status", "--porcelain", "--untracked-files=no",
        "--", "src", "config", "scripts", "serve.py", "start_dashboard.bat",
    ):
        version += "*"

    _VERSION_CACHE = version
    return version


def to_row(rec: dict[str, Any], label: str, machine_id: str, version: str) -> list[str]:
    diag = rec.get("diag")
    ok = rec.get("ok")
    # ACL-1358: last gate before the shared sheet - an older journal line may
    # still hold the value of a token this machine has since stored.
    return [run_telemetry._hide_configured_secrets(cell) for cell in [
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
    ]]


def pending(limit: int | None = None, *, since: int | None = None) -> tuple[list[list[str]], int]:
    """(rows ready to send, index they end at)."""
    records = run_telemetry.load()
    start = read_cursor() if since is None else since
    start = max(0, min(start, len(records)))
    slice_ = records[start:]
    if limit is not None:
        slice_ = slice_[:limit]
    label, machine_id = machine_identity()
    version = flow_version()
    return [to_row(r, label, machine_id, version) for r in slice_], start + len(slice_)


def fetch_rows(*, local: bool = False) -> tuple[list[dict[str, str]], str]:
    """(rows, human-readable source) for anything that reads the aggregate.

    Shared by the report and the proposals tool so the sheet's shape is
    understood in exactly one place. Falls back to this machine's JSONL when
    the sheet cannot be read — a analyst without network still gets an answer,
    clearly labelled as covering one machine only.
    """
    if not local:
        status = config_status()
        if status["sheet_id"]:
            try:
                service = _service_oauth(status["sheet_id"])
                values = (
                    service.spreadsheets()
                    .values()
                    .get(spreadsheetId=status["sheet_id"], range=status["tab"])
                    .execute()
                    .get("values", [])
                )
                if values:
                    header, *body = values
                    rows = [
                        dict(zip(header, row + [""] * (len(header) - len(row)))) for row in body
                    ]
                    return rows, "общая таблица"
            except Exception:
                pass  # fall through to local

    label, machine_id = machine_identity()
    version = flow_version()
    rows = []
    for rec in run_telemetry.load():
        ok = rec.get("ok")
        rows.append(
            {
                "ts": str(rec.get("ts", "")),
                "machine": label,
                "machine_id": machine_id,
                "flow_version_at_upload": version,
                "run_id": str(rec.get("run_id", "")),
                "campaign": str(rec.get("campaign", "")),
                "step": str(rec.get("step", "")),
                "ok": "" if ok is None else ("1" if ok else "0"),
                "duration_s": "" if rec.get("duration_s") is None else str(rec["duration_s"]),
                "diag": json.dumps(rec.get("diag"), ensure_ascii=False) if rec.get("diag") else "",
                "note": str(rec.get("note", "")),
            }
        )
    return rows, "локальный JSONL этой машины"


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
        "oauth_authorised": _has_oauth_token(),
        "automatic_upload": False,
        "transport": transport,
        "configured": bool(transport),
    }


def _has_oauth_token() -> bool:
    """Local evidence only; the next explicit upload verifies remote access."""
    try:
        from config import token_vault

        token = json.loads(token_vault.read_secret_text(_local(_TOKEN_FILE)))
        scopes = token.get("scopes") or []
        if isinstance(scopes, str):
            scopes = scopes.split()
        return bool(token.get("refresh_token") and token.get("client_id") and token.get("client_secret") and set(_SCOPES).issubset(scopes))
    except (OSError, ValueError, AttributeError, RuntimeError):
        return False


def _oauth_client_path() -> str:
    from config.paths import get_google_oauth_client_path

    return get_google_oauth_client_path()


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
    <local config dir>/token_accounts.json: that one is read-only and works today, and
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
            from config import token_vault

            creds = Credentials.from_authorized_user_info(
                json.loads(token_vault.read_secret_text(token_file)), _SCOPES
            )
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
        from config import token_vault

        token_vault.write_secret_text(token_file, creds.to_json())

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
        result["error"] = run_telemetry._hide_configured_secrets(f"{type(exc).__name__}: {exc}")[:300]
        return result
    # Advance the cursor only after the write is acknowledged: a failed upload
    # must be retried, not silently skipped.
    write_cursor(end)
    result["sent"] = len(rows)
    result["cursor_after"] = end
    return result


# Kept so anything importing the old private name keeps working.
_flow_version = flow_version
