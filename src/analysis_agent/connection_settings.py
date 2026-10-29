"""Team connection settings from one private Google Sheet (ACL-2306).

The library token and the BI credentials live in ONE private Google Sheet that
the team lead shares with named people (or the adopy.com domain). Nobody
forwards a key. A buyer signs in with their own Google account (the same user
OAuth and the same read-only token file the accounts sheet already uses), the
dashboard reads the sheet and stores the values in the machine settings
(`%LOCALAPPDATA%\\ACL\\.env`, through `config.paths.update_machine_env`).

Why a Sheet and not a Drive file found by name: the existing read-only token
already has the `spreadsheets.readonly` scope, so no new consent is needed;
`drive.readonly` (a restricted scope) would be required to search Drive. The
sheet id is configured (`ACL_CONNECTION_SHEET_ID` or `config/connection_file.json`);
it is not a secret, sharing controls access.

Revoke = unshare the sheet: the next read fails with "no access". Rotate = edit
the sheet: machines re-read it at every dashboard start (`refresh_on_start`) and
keep the last values in the machine settings for offline work.

Values never go to logs, telemetry, URLs or page responses. Errors are fixed
texts; status answers carry only booleans and counters.
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Callable

from config import paths

log = logging.getLogger(__name__)

LIBRARY_KEYS = ("ACL_LIBRARY_TOKEN", "ACL_LIBRARY_REPO")
BI_KEYS = ("BI_MYSQL_HOST", "BI_MYSQL_PORT", "BI_MYSQL_USER", "BI_MYSQL_PASSWORD",
           "BI_MYSQL_DATABASE", "BI_MYSQL_TABLE")
BI_OPTIONAL_KEYS = ("BI_STATS_ALL_CMS", "BI_TIMEZONE", "BI_DAY_BOUNDARY", "BI_CURRENCY")
ALLOWED_KEYS = LIBRARY_KEYS + BI_KEYS + BI_OPTIONAL_KEYS

_TOKEN_SHAPE = re.compile(r"[A-Za-z0-9_\-]{20,255}")
_REPO_SHAPE = re.compile(r"[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+")
_HOST_SHAPE = re.compile(r"[A-Za-z0-9.\-]{1,253}")
_NAME_SHAPE = re.compile(r"[A-Za-z0-9_.\-]{1,64}")
_SIMPLE = re.compile(r"[A-Za-z0-9_.:+\- /]{1,64}")


class SettingsError(Exception):
    """Plain-language reason; never contains a setting value."""


# -- where the file is -------------------------------------------------------

def _config_file() -> dict:
    try:
        path = Path(__file__).resolve().parents[2] / "config" / "connection_file.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def sheet_id() -> str:
    explicit = (os.environ.get("ACL_CONNECTION_SHEET_ID") or "").strip()
    return explicit or str(_config_file().get("sheet_id") or "").strip()


def tab_name() -> str:
    return (os.environ.get("ACL_CONNECTION_SHEET_TAB") or "").strip() or str(_config_file().get("tab") or "connection")


# -- validation (before anything is saved) -----------------------------------

def _validate_one(key: str, value: str) -> str:
    value = (value or "").strip()
    if not value or any(ord(ch) < 32 for ch in value):
        raise SettingsError(f"The team connection file has an empty or malformed {key}.")
    ok = True
    if key == "ACL_LIBRARY_TOKEN":
        ok = bool(_TOKEN_SHAPE.fullmatch(value))
    elif key == "ACL_LIBRARY_REPO":
        ok = bool(_REPO_SHAPE.fullmatch(value))
    elif key == "BI_MYSQL_HOST":
        ok = bool(_HOST_SHAPE.fullmatch(value))
    elif key == "BI_MYSQL_PORT":
        ok = value.isdigit() and 0 < int(value) < 65536
    elif key == "BI_MYSQL_DATABASE":
        ok = bool(_NAME_SHAPE.fullmatch(value))
    elif key in ("BI_MYSQL_TABLE", "BI_TIMEZONE", "BI_DAY_BOUNDARY", "BI_CURRENCY", "BI_STATS_ALL_CMS"):
        ok = bool(_SIMPLE.fullmatch(value))
    elif key == "BI_MYSQL_USER":
        ok = len(value) <= 128
    elif key == "BI_MYSQL_PASSWORD":
        ok = len(value) <= 512
    if not ok:
        raise SettingsError(f"The team connection file has a malformed {key}. Ask your team lead to fix it.")
    return value


def validate(values: dict) -> dict:
    """Allowed keys only, every value well-formed, each connection complete."""
    clean: dict = {}
    for key, value in values.items():
        key = str(key).strip().upper()
        if key not in ALLOWED_KEYS:
            continue  # unknown rows are ignored, never applied
        if value is None or not str(value).strip():
            continue
        clean[key] = _validate_one(key, str(value))
    bi_core = [k for k in ("BI_MYSQL_HOST", "BI_MYSQL_USER", "BI_MYSQL_PASSWORD") if k in clean]
    if bi_core and len(bi_core) != 3:
        raise SettingsError("The team connection file has incomplete BI settings. Ask your team lead to fix it.")
    if not clean:
        raise SettingsError("The team connection file has no connection settings yet. Ask your team lead to fill it.")
    return clean


# -- reading the sheet -------------------------------------------------------

_READONLY = ["https://www.googleapis.com/auth/spreadsheets.readonly"]


def _token_path() -> Path:
    return Path(paths.get_google_oauth_token_path())


def has_google_token() -> bool:
    try:
        from config import token_vault

        data = json.loads(token_vault.read_secret_text(_token_path()))
        return bool(data.get("refresh_token"))
    except Exception:
        return False


def _service(interactive: bool):
    """Sheets service as the employee's own Google account; None when not signed in."""
    token = _token_path()
    if not interactive:
        if not has_google_token():
            return None
        try:
            import google.auth.transport.requests
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build

            from config import token_vault

            creds = Credentials.from_authorized_user_info(json.loads(token_vault.read_secret_text(token)), _READONLY)
            if not creds.valid:
                creds.refresh(google.auth.transport.requests.Request())
            return build("sheets", "v4", credentials=creds, cache_discovery=False)
        except Exception:
            return None
    from src.analysis_agent import gsheet_accounts as ga

    client = Path(paths.get_google_oauth_client_path())
    if not client.is_file():
        raise SettingsError("Google sign-in is not set up on this computer (oauth_client.json is missing). "
                            "Ask your team lead for it.")
    service = ga._build_sheets_service_oauth(str(client), str(token))
    if service is None:
        raise SettingsError("Google sign-in could not be completed. Try again.")
    return service


def _http_status(exc: BaseException) -> int:
    resp = getattr(exc, "resp", None)
    try:
        return int(getattr(resp, "status", 0) or 0)
    except (TypeError, ValueError):
        return 0


def read_remote(service=None, *, interactive: bool = False) -> dict:
    """Rows `KEY | value` of the team connection sheet, validated."""
    sid = sheet_id()
    if not sid:
        raise SettingsError("The team connection file is not configured in this build. Ask your team lead.")
    if service is None:
        service = _service(interactive)
    if service is None:
        raise SettingsError("Not signed in with Google. Press Sign in with Google.")
    try:
        result = service.spreadsheets().values().get(
            spreadsheetId=sid, range=f"'{tab_name()}'!A:B").execute()
    except Exception as exc:  # noqa: BLE001 - Google error text can carry ids; use fixed texts
        status = _http_status(exc)
        if status in (403, 404):
            raise SettingsError("No access to the ACL connection file. Ask your team lead to share it "
                                "with your Adopy Google account.") from None
        if status == 400:
            raise SettingsError("The ACL connection file has no connection tab. Ask your team lead to fix it.") from None
        raise SettingsError("Could not read the ACL connection file. Check the internet connection and try again.") from None
    rows = (result or {}).get("values") or []
    values = {str(r[0]).strip(): str(r[1]) for r in rows if len(r) >= 2 and str(r[0]).strip()}
    return validate(values)


# -- writing the machine settings --------------------------------------------

def apply(values: dict, writer: Callable[[dict], Any] | None = None) -> dict:
    """Validate, then save to the machine settings only. Nothing is saved on error."""
    clean = validate(values)
    changed = {k: v for k, v in clean.items() if (os.environ.get(k) or "").strip() != v}
    if changed:
        try:
            (writer or paths.update_machine_env)(changed)
        except Exception:
            raise SettingsError("The settings are valid, but they could not be saved in this machine's settings.") from None
        if "ACL_LIBRARY_TOKEN" in changed or "ACL_LIBRARY_REPO" in changed:
            try:
                from src.analysis_agent import team_library as tl

                tl.use_backend(None)
            except Exception:
                pass
    return {"updated": len(changed), "library": "ACL_LIBRARY_TOKEN" in clean, "bi": "BI_MYSQL_HOST" in clean}


def sign_in(writer: Callable[[dict], Any] | None = None, service=None) -> dict:
    """Interactive: Google sign-in, read the sheet, save the values. Checks are separate."""
    values = read_remote(service, interactive=True)
    return apply(values, writer)


def refresh_on_start() -> dict | None:
    """Silent re-read at dashboard start. No sign-in, no popups; failures keep the stored values."""
    try:
        if not sheet_id() or not has_google_token():
            return None
        return apply(read_remote(interactive=False))
    except SettingsError as exc:
        log.info("Team connection settings were not refreshed: %s", exc)
    except Exception:
        log.info("Team connection settings were not refreshed.")
    return None


# -- checks (one read per connection) ----------------------------------------

def check_library() -> dict:
    token = (os.environ.get("ACL_LIBRARY_TOKEN") or "").strip()
    if not token:
        return {"name": "Team library", "ok": False, "configured": False,
                "line": "Team library: not connected"}
    from src.analysis_agent import team_library as tl

    repo = (os.environ.get("ACL_LIBRARY_REPO") or "").strip() or tl.DEFAULT_REPO
    branch = (os.environ.get("ACL_LIBRARY_BRANCH") or "").strip() or tl.DEFAULT_BRANCH
    try:
        be = tl.GitHubBackend(repo, token, branch)
        counts = {}
        for kind in ("targets", "ad_texts"):
            counts[kind] = len(tl._parse(kind, be.get(tl.FILES[kind][0]))[tl.FILES[kind][1]])
    except tl.LibraryError as exc:
        return {"name": "Team library", "ok": False, "configured": True,
                "line": "Team library: " + str(exc).replace(token, "[redacted]")}
    except Exception:
        return {"name": "Team library", "ok": False, "configured": True,
                "line": "Team library: could not be checked. Try again in a moment."}
    return {"name": "Team library", "ok": True, "configured": True,
            "line": f"Team library: connected ({counts['targets']} targets, {counts['ad_texts']} texts)"}


def check_bi() -> dict:
    from src.analysis_agent import bi_stats

    if not bi_stats.bi_enabled():
        return {"name": "BI", "ok": False, "configured": False, "line": "BI: not connected"}
    try:
        cfg = bi_stats._settings()
        conn = bi_stats._connect(cfg)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.execute(f"SELECT MAX(`DATE`) FROM {bi_stats._table_ref(cfg)}")
                last = cur.fetchone()[0]
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        return {"name": "BI", "ok": False, "configured": True,
                "line": "BI: could not connect (" + bi_stats.describe_error(exc) + ")"}
    return {"name": "BI", "ok": True, "configured": True,
            "line": f"BI: connected (last data date {str(last)[:10] if last else 'unknown'})"}


def check_all() -> list:
    return [check_library(), check_bi()]


def bi_accounts_for_manager(name: str) -> list:
    """Account aliases of one campaign manager, read from BI (CM_ID mapped by name)."""
    from src.analysis_agent import bi_stats

    codes = [c for c, n in bi_stats.load_cm_map().items() if n.casefold() == (name or "").strip().casefold()]
    if not codes:
        return []
    cfg = bi_stats._settings()
    conn = bi_stats._connect(cfg)
    try:
        with conn.cursor() as cur:
            marks = ", ".join(["%s"] * len(codes))
            cur.execute(f"SELECT DISTINCT `ACCOUNT_ALIAS` FROM {bi_stats._table_ref(cfg)} "
                        f"WHERE `CM_ID` IN ({marks})", codes)
            rows = cur.fetchall()
    finally:
        conn.close()
    return sorted({str(r[0]).strip() for r in rows if r and str(r[0] or "").strip()}, key=str.casefold)


# -- Connections status for the dashboard (no values) ------------------------

def status() -> dict:
    """Which connections are configured here. Booleans and masks only, never values."""
    from src.analysis_agent import bi_stats

    token = (os.environ.get("ACL_LIBRARY_TOKEN") or "").strip()
    bi = bi_stats.bi_enabled()
    return {
        "file_configured": bool(sheet_id()),
        "signed_in": has_google_token(),
        "library": {"configured": bool(token), "masked": "********" if token else ""},
        "bi": {"configured": bi, "masked": "********" if bi else ""},
    }


# -- lead: publish what the RUNNING dashboard loaded -------------------------

def shareable() -> dict:
    """Values the running dashboard actually loaded (effective environment)."""
    out = {k: (os.environ.get(k) or "").strip() for k in ALLOWED_KEYS}
    return {k: v for k, v in out.items() if v}


def shareable_report() -> dict:
    """What this machine can publish, without values; says clearly what is missing."""
    have = shareable()
    lib = "ACL_LIBRARY_TOKEN" in have
    bi = all(k in have for k in ("BI_MYSQL_HOST", "BI_MYSQL_USER", "BI_MYSQL_PASSWORD"))
    notes = []
    if not lib:
        notes.append("This computer has no team library token to share.")
    if not bi:
        notes.append("This computer has no BI connection to share.")
    return {"library": lib, "bi": bi, "notes": notes, "can_publish": lib or bi}


def publish(service=None) -> dict:
    """Write the effective values into the team sheet (lead only; needs edit access)."""
    report = shareable_report()
    if not report["can_publish"]:
        raise SettingsError(" ".join(report["notes"]))
    have = shareable()
    if not report["bi"]:
        have = {k: v for k, v in have.items() if k not in BI_KEYS + BI_OPTIONAL_KEYS}
    if not report["library"]:
        have = {k: v for k, v in have.items() if k not in LIBRARY_KEYS}
    validate(have)
    sid = sheet_id()
    if not sid:
        raise SettingsError("The team connection file is not configured in this build.")
    if service is None:
        from src.fb_ads_rpa import telemetry_sync as ts

        service = ts._service_oauth(sid)  # write-scope user token, separate from the read token
    try:
        service.spreadsheets().values().update(
            spreadsheetId=sid, range=f"'{tab_name()}'!A1", valueInputOption="RAW",
            body={"values": [[k, v] for k, v in sorted(have.items())]}).execute()
    except Exception:
        raise SettingsError("Could not write the ACL connection file. You need edit access to it.") from None
    return {"published": True, "library": report["library"], "bi": report["bi"], "notes": report["notes"]}
