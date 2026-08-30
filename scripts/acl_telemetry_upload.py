"""Ship this machine's RPA telemetry to the shared sheet.

Run it after a publish run (or on a schedule). Deliberately NOT part of the
run itself: a campaign must never wait on, or fail because of, a network call
made for diagnostics.

  python scripts/acl_telemetry_upload.py --dry-run   # what would go, send nothing
  python scripts/acl_telemetry_upload.py             # send what's new
  python scripts/acl_telemetry_upload.py --status    # is this machine set up?

Setup is a one-time job on the Google side, done once for the whole team:
  1. Create a Google Cloud service account, download its JSON key.
  2. Save the key as <machine config dir>\\telemetry_service_account.json
     (see `python scripts/acl_doctor.py` for that folder).
  3. Create the spreadsheet, share it with the service account's e-mail as
     Editor, and put its id in .env as GSHEET_TELEMETRY_ID.
The tab and its header row are created automatically on the first upload.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Windows consoles are not UTF-8 by default; without this a Russian line
# aborts the script instead of printing. See scripts/_console.py.
from scripts._console import use_utf8  # noqa: E402

use_utf8()

try:
    from dotenv import load_dotenv

    load_dotenv(_ROOT / ".env")
except Exception:
    pass

from src.fb_ads_rpa import telemetry_sync  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true", help="show what would be sent")
    ap.add_argument("--status", action="store_true", help="show configuration and exit")
    ap.add_argument("--limit", type=int, default=2000, help="max records per upload")
    ap.add_argument(
        "--resend-all",
        action="store_true",
        help="ignore the cursor and send this machine's whole history again",
    )
    args = ap.parse_args()

    label, machine_id = telemetry_sync.machine_identity()
    status = telemetry_sync.config_status()
    print(f"machine   : {label} ({machine_id})")
    print(f"sheet     : {status['sheet_id'] or '(GSHEET_TELEMETRY_ID not set)'}")
    print(f"tab       : {status['tab']}")
    # Show only the transport actually in use. Printing the service-account key
    # path while writing over OAuth invites the reader to debug the wrong thing.
    print(f"transport : {status['transport'] or '(none configured)'}")
    if status["transport"] == "oauth":
        print(
            f"auth      : {'authorised' if status['oauth_authorised'] else 'browser consent needed'}"
            f" — {status['oauth_token']}"
        )
    elif status["transport"] == "service_account":
        print(f"key file  : {status['key_path']}")
    elif status["transport"] == "webhook":
        print(f"webhook   : {status['webhook']}")

    if args.status:
        print("configured:", "yes" if status["configured"] else "no")
        return 0 if status["configured"] else 1

    if args.resend_all:
        telemetry_sync.write_cursor(0)
        print("cursor reset — the whole local history will be sent")

    rows, _ = telemetry_sync.pending(args.limit)
    print(f"pending   : {len(rows)} record(s)")
    if args.dry_run:
        for r in rows[:5]:
            print("   ", r[:8])
        if len(rows) > 5:
            print(f"    ... and {len(rows) - 5} more")
        return 0

    res = telemetry_sync.upload(limit=args.limit)
    if res["error"]:
        print(f"NOT SENT: {res['error']}")
        return 1
    print(f"sent      : {res['sent']} record(s), cursor {res['cursor_before']} -> {res['cursor_after']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
