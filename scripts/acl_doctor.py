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
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(_ROOT / ".env")
except Exception:  # pragma: no cover - .env is optional for the check itself
    pass

from config.paths import (  # noqa: E402
    get_ad_account_map_path,
    get_creatives_root,
    get_excel_path,
    get_local_config_dir,
    get_upload_fb_excel_path,
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
    try:
        import csv

        with open(path, newline="", encoding="utf-8-sig") as f:
            rows = [r for r in csv.DictReader(f) if (r.get("account_name") or "").strip()]
    except Exception as exc:
        report(MISSING, "Account map", f"{path} — unreadable: {exc}", "Fix the CSV or re-copy the example.")
        return
    no_profile = [r["account_name"] for r in rows if not (r.get("profile_id") or "").strip()]
    report(OK, "Account map", f"{len(rows)} account(s) — {path}")
    if no_profile:
        report(
            WARN,
            "Accounts without a Multilogin profile_id",
            ", ".join(no_profile[:5]) + ("..." if len(no_profile) > 5 else ""),
            "RPA cannot open a browser session for these until profile_id is set.",
        )


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


def main() -> int:
    print(f"ACL doctor — checkout {_ROOT}")
    print(f"Machine config dir: {get_local_config_dir()}")
    print()

    check_python()
    check_playwright()
    check_account_map()
    check_env_key(
        "MULTILOGIN_AUTOMATION_TOKEN_A",
        required=True,
        fix="Add it to .env — RPA opens browser profiles through the Multilogin API.",
    )
    check_multilogin()
    check_path(
        "Stats workbook",
        get_excel_path(),
        required=False,
        fix="Set FB_STATS_EXCEL in .env if your report workbook lives elsewhere.",
    )
    check_path(
        "Upload FB workbook",
        get_upload_fb_excel_path(),
        required=False,
        fix="Set UPLOAD_FB_EXCEL in .env if yours lives elsewhere.",
    )
    check_path(
        "Creatives folder",
        get_creatives_root(),
        required=False,
        fix="Set FB_CREATIVES_ROOT in .env — RPA looks up creative files by name here.",
    )

    print()
    if _failed:
        print(f"{_failed} required item(s) missing — ACL will not run correctly yet.")
        return 1
    print("All required items present.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
