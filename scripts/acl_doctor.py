"""Check that this machine is configured to run ACL, and say what is missing.

Written for distribution: ACL is moving from one repo on one machine to a
launcher installed on every media buyer's computer. The parts that differ per
machine — accounts, tokens, the Multilogin install, the stats workbook — are
deliberately NOT in git, so a fresh checkout runs only after someone puts them
in place. Without this, "it doesn't work" arrives as a stack trace from
whichever module happened to touch the missing thing first.

Prints one line per requirement and exits non-zero if anything required is
missing. Never prints secret values — only whether they are set.

Usage:
  python scripts/acl_doctor.py
"""
from __future__ import annotations

import os
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Windows consoles are not UTF-8 by default; without this a Russian line
# aborts the script instead of printing. See scripts/_console.py.
from scripts._console import use_utf8  # noqa: E402

use_utf8()

from config.paths import (  # noqa: E402
    get_ad_account_map_path,
    get_creatives_root,
    get_env_files,
    get_excel_path,
    get_google_oauth_client_path,
    get_local_config_dir,
    get_machine_env_path,
    get_upload_fb_excel_path,
    inspect_account_map,
    ensure_machine_config,
    load_env,
)

OK, MISSING, WARN = "OK", "MISSING", "WARN"
_failed = 0


def report(status: str, what: str, detail: str = "", fix: str = "") -> None:
    global _failed
    if status == MISSING:
        _failed += 1
    mark = {OK: "[ ok ]", MISSING: "[FAIL]", WARN: "[warn]"}[status]
    print(f"{mark} {what}" + (f" — {detail}" if detail else ""))
    if fix and status != OK:
        print(f"       -> {fix}")


def check_python() -> None:
    v = sys.version_info
    if v >= (3, 11):
        report(OK, "Python", f"{v.major}.{v.minor}.{v.micro}")
    else:
        report(MISSING, "Python", f"{v.major}.{v.minor}", "ACL needs Python 3.11 or newer.")


def check_path(label: str, path: str, *, required: bool, fix: str) -> None:
    if path and os.path.exists(path):
        report(OK, label, path)
    else:
        report(MISSING if required else WARN, label, path or "(not set)", fix)


def check_account_map() -> None:
    path = get_ad_account_map_path()
    if not os.path.exists(path):
        report(
            MISSING,
            "Account map",
            path,
            f"Copy {_ROOT / 'config' / 'ad_account_map.example.csv'} there and fill in your accounts.",
        )
        return
    info = inspect_account_map(path)
    if not info["valid"]:
        report(
            MISSING,
            "Account map",
            f"{path} — {'; '.join(info['errors'])}",
            "Replace the example values and use act_<digits> ad account IDs.",
        )
        return
    if info["missing_profile_rows"]:
        report(
            MISSING,
            "Account map",
            f"{info['missing_profile_rows']} of {info['rows']} account(s) have no Multilogin profile_id — {path}",
            "Fill profile_id for every account the campaign manager can select.",
        )
        return
    report(OK, "Account map", f"{info['rows']} complete account(s) — {path}")


def check_env_key(name: str, *, required: bool, fix: str) -> None:
    # Presence only — never the value.
    if (os.environ.get(name) or "").strip():
        report(OK, f"{name}", "set")
    else:
        report(MISSING if required else WARN, f"{name}", "not set", fix)


def check_multilogin() -> None:
    home = (os.environ.get("MLX_HOME") or "").strip() or os.path.join(os.path.expanduser("~"), "mlx")
    profiles = os.path.join(home, "profiles")
    if os.path.isdir(profiles):
        report(OK, "Multilogin profiles", profiles)
    else:
        report(
            MISSING,
            "Multilogin profiles",
            profiles,
            "Install Multilogin and open a profile once, or set MLX_HOME in .env.",
        )


def check_playwright() -> None:
    try:
        import playwright  # noqa: F401

        report(OK, "Playwright", "installed")
    except Exception:
        report(MISSING, "Playwright", "not installed", "pip install -r requirements.txt")


def check_form_parser() -> None:
    """Starlette accepts POST forms only when python-multipart is installed."""
    try:
        import multipart

        report(OK, "Dashboard form parser", getattr(multipart, "__version__", "installed"))
    except Exception:
        report(
            MISSING,
            "Dashboard form parser",
            "python-multipart is not installed",
            "Run install_acl.bat again (or pip install -r requirements.txt).",
        )


def _resolve_checkout_path(raw: str) -> str:
    return raw if os.path.isabs(raw) else str(_ROOT / raw)


def _valid_google_json(path: str) -> bool:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return False
        if data.get("type") == "service_account":
            return bool(data.get("client_email") and data.get("private_key"))
        client = data.get("installed") or data.get("web")
        return isinstance(client, dict) and bool(client.get("client_id") and client.get("client_secret"))
    except Exception:
        return False


def check_google_client() -> None:
    """Accept either supported Google credential mode and validate its shape.

    It is in .gitignore, so a fresh clone has nothing to authorise with: neither
    the dashboard's sheet reads nor telemetry upload would work, and both fail
    far from here with a message about credentials rather than about a missing
    file. Someone has to hand this file to each machine.
    """
    oauth = get_google_oauth_client_path()
    service = _resolve_checkout_path(
        os.environ.get("GOOGLE_CREDENTIALS_JSON", "").strip()
        or "config/google_credentials.json"
    )
    for label, path in (("OAuth", oauth), ("service account", service)):
        if os.path.isfile(path) and _valid_google_json(path):
            report(OK, "Google credentials", f"{label} — {path}")
            return
    present_invalid = [path for path in (oauth, service) if os.path.exists(path)]
    detail = "invalid JSON/credential shape: " + ", ".join(present_invalid) if present_invalid else f"not found: {oauth} or {service}"
    report(
        MISSING,
        "Google credentials",
        detail,
        "Copy a valid OAuth client or service-account JSON to this machine; it is not shipped in git.",
    )


def check_telemetry() -> None:
    """Optional, never required: a machine that never reports still runs fine."""
    try:
        from src.fb_ads_rpa import telemetry_sync
    except Exception as exc:
        report(WARN, "Telemetry upload", f"unavailable: {exc}")
        return
    label, machine_id = telemetry_sync.machine_identity()
    status = telemetry_sync.config_status()
    if status["configured"]:
        pending, _ = telemetry_sync.pending(limit=None)
        needs_auth = status.get("transport") == "oauth" and not status.get("oauth_authorised")
        report(
            WARN if needs_auth else OK,
            "Telemetry upload",
            ("Google authorization needed" if needs_auth else "transport configured; access is checked on upload")
            + f" — {label} ({machine_id}), {len(pending)} record(s) pending",
        )
        print("       -> manual upload only; no automatic sender is running")
        print("       -> run: python scripts/acl_telemetry_upload.py (Google sign-in on first upload)")
        print("       -> which steps ran, whether each passed, how long it took")
        if status.get("transport") == "oauth":
            print("       -> uploaded rows are written under YOUR Google account")
        print("       -> no ad accounts, creatives, links or ad text are sent")
        return
    missing = []
    if not status["sheet_id"]:
        missing.append("GSHEET_TELEMETRY_ID not set")
    if not status["key_exists"] and not status.get("oauth_client_exists"):
        missing.append("no Google credentials for the upload")
    report(
        WARN,
        "Telemetry upload",
        "off — " + "; ".join(missing),
        "Local records stay on this machine until a manual upload is configured.",
    )


def check_env_location() -> None:
    """Production installs must keep their settings outside the checkout."""
    files = get_env_files()
    local = get_machine_env_path()
    if local in files:
        report(OK, "Settings location", f"machine-local — {local}")
        return
    if not files:
        report(
            MISSING,
            "Settings location",
            "no machine-local .env",
            "Run python scripts/acl_migrate_config.py --provision.",
        )
        return
    report(
        MISSING,
        "Settings location",
        f"only inside the checkout — {files[0]}",
        f"Run python scripts/acl_migrate_config.py --provision to move it to {local}.",
    )


def check_runtime_safety() -> None:
    backend = (os.environ.get("FB_PUBLISH_BACKEND") or "rpa").strip().lower()
    if backend == "rpa":
        report(OK, "Publish backend", "draft-only RPA")
    else:
        report(
            MISSING,
            "Publish backend",
            backend,
            "Set FB_PUBLISH_BACKEND=rpa; the legacy live Meta API backend is disabled.",
        )

    engine = (os.environ.get("FB_RPA_ENGINE") or "").strip().lower()
    if engine == "recorded_flow":
        report(OK, "RPA engine", engine)
    else:
        report(
            MISSING,
            "RPA engine",
            engine or "empty",
            "Set FB_RPA_ENGINE=recorded_flow; it is the only production-verified full flow.",
        )


def check_campaign_manager_identity() -> None:
    value = (os.environ.get("MY_CM_NAME") or "").strip()
    if value and value.lower() not in {"your name", "your_name", "example"}:
        report(OK, "Campaign manager identity", "MY_CM_NAME is set")
    else:
        report(
            MISSING,
            "Campaign manager identity",
            "MY_CM_NAME is not configured",
            "Set it exactly as the campaign manager name appears in the stats workbook.",
        )


def check_accounts_sheet() -> None:
    """The accounts spreadsheet — from .env, or the built-in company default.

    The sheet is the same one for every campaign manager, so the id now ships in
    the code and no install has to paste it. This check used to demand the env
    var regardless, which failed a machine that was in fact perfectly able to
    read the sheet — a red line for a problem that did not exist.
    """
    value = (os.environ.get("GSHEET_ACCOUNTS_ID") or "").strip()
    if value and "your_" not in value.lower() and "example" not in value.lower():
        report(OK, "Accounts Google Sheet", "GSHEET_ACCOUNTS_ID is set")
        return
    try:
        from src.analysis_agent.gsheet_accounts import DEFAULT_ACCOUNTS_SHEET_ID

        if DEFAULT_ACCOUNTS_SHEET_ID:
            report(OK, "Accounts Google Sheet", "using the built-in company sheet")
            return
    except Exception:
        pass
    report(
        MISSING,
        "Accounts Google Sheet",
        "GSHEET_ACCOUNTS_ID is missing or still an example",
        "Set the real spreadsheet ID in the machine-local .env.",
    )


def check_multilogin_token() -> None:
    from src.fb_ads_rpa.config import get_browser_provider

    provider = get_browser_provider()
    required = provider in {"multilogin", "multilogin_attach"}
    # ACL-1335: машина, подключённая входом, токена автоматизации не держит.
    from src.meta_campaign_agent.mlx_credentials import has_saved_session

    if not (os.environ.get("MULTILOGIN_AUTOMATION_TOKEN_A") or "").strip() and has_saved_session("A"):
        report(OK, "Multilogin A", "signed in (renews automatically)")
        return
    check_env_key(
        "MULTILOGIN_AUTOMATION_TOKEN_A",
        required=required,
        fix=f"Provider {provider} needs an automation token; connect Multilogin in the dashboard.",
    )


def main() -> int:
    global _failed
    _failed = 0
    try:
        notes = ensure_machine_config()
    except Exception as exc:
        notes = [f"machine config provisioning failed: {exc}"]
    loaded = load_env()

    print(f"ACL doctor — checkout {_ROOT}")
    print(f"Machine config dir: {get_local_config_dir()}")
    # Which .env actually took effect. Two possible locations is one too many to
    # keep in your head when a setting "isn't being picked up".
    for f in loaded:
        print(f"Settings loaded:    {f}")
    if not loaded:
        print("Settings loaded:    none — no .env in either location")
    for note in notes:
        print(f"Migration:          {note}")
    print()

    check_python()
    check_playwright()
    check_form_parser()
    check_account_map()
    check_env_location()
    check_runtime_safety()
    check_multilogin_token()
    check_multilogin()
    check_campaign_manager_identity()
    check_accounts_sheet()
    check_path(
        "Stats workbook",
        get_excel_path(),
        required=True,
        fix="Set FB_STATS_EXCEL in .env if your report workbook lives elsewhere.",
    )
    check_path(
        "Upload FB workbook",
        get_upload_fb_excel_path(),
        required=True,
        fix="Set UPLOAD_FB_EXCEL in .env if yours lives elsewhere.",
    )
    check_path(
        "Creatives folder",
        get_creatives_root(),
        required=True,
        fix="Set FB_CREATIVES_ROOT in .env — RPA looks up creative files by name here.",
    )
    check_google_client()
    check_telemetry()

    print()
    if _failed:
        print(f"{_failed} required item(s) missing — ACL will not run correctly yet.")
        return 1
    print("All required items present.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
