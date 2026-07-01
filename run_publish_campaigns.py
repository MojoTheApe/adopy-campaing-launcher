#!/usr/bin/env python3
"""Agent 9: publish Agent 8 upload Excel via Meta Marketing API."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_root = Path(__file__).resolve().parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))
os.chdir(_root)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

try:
    from dotenv import load_dotenv

    load_dotenv(_root / ".env")
except ImportError:
    pass

from src.meta_campaign_agent.meta_client import MetaGraphError
from src.meta_campaign_agent.publish import build_dry_run_report, live_report_to_json, publish_live, report_to_json


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Agent 9: Meta API publish from Agent 8 upload Excel",
    )
    p.add_argument("-i", "--input", required=True, help="Agent 8 output .xlsx")
    p.add_argument("--account", help="Only one account sheet (partial name match)")
    p.add_argument("--dry-run", action="store_true", help="Print JSON plan; no Graph API calls")
    p.add_argument(
        "--via-multilogin",
        action="store_true",
        help="Per account: start ML profile, route Meta API via profile proxy, stop profile",
    )
    p.add_argument(
        "--confirm",
        action="store_true",
        help="Required for live publish (creates real campaigns in Meta)",
    )
    p.add_argument("--activate", action="store_true", help="Create objects as ACTIVE (default PAUSED)")
    p.add_argument("--limit", type=int, default=None, help="Publish at most N ready rows")
    p.add_argument("--creatives-root", help="Override FB_CREATIVES_ROOT")
    p.add_argument(
        "-e",
        "--upload-excel",
        help="Upluad FB.xlsx for Interests JSON + Ad copy lookup",
    )
    p.add_argument(
        "--map",
        dest="account_map",
        help="CSV account_name,ad_account_id,page_id (default: config/ad_account_map.csv)",
    )
    p.add_argument("-o", "--output", help="Write JSON report to file")
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    input_path = Path(args.input)
    if not input_path.is_file():
        print(f"Input not found: {input_path}", file=sys.stderr)
        return 1

    if args.dry_run:
        report = build_dry_run_report(
            str(input_path),
            account_filter=args.account,
            creatives_root=args.creatives_root,
            ad_account_map_path=args.account_map,
            upload_fb_excel=args.upload_excel,
            activate=args.activate,
        )
        text = report_to_json(report)
        if args.output:
            out = Path(args.output)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(text, encoding="utf-8")
            print(f"Dry-run saved: {out}")
        else:
            print(text)
        print(
            f"\nSummary: {report.ready_count}/{len(report.jobs)} rows ready, "
            f"{report.error_count} with errors.",
            file=sys.stderr,
        )
        return 0 if report.error_count == 0 else 2

    if not args.confirm:
        print(
            "Live publish creates real Meta objects. Re-run with --confirm (and optional --limit 1 for a safe test).",
            file=sys.stderr,
        )
        return 1

    try:
        live = publish_live(
            str(input_path),
            account_filter=args.account,
            creatives_root=args.creatives_root,
            ad_account_map_path=args.account_map,
            upload_fb_excel=args.upload_excel,
            activate=args.activate,
            limit=args.limit,
            via_multilogin=args.via_multilogin,
        )
    except MetaGraphError as e:
        print(f"Meta error: {e}", file=sys.stderr)
        return 1

    text = live_report_to_json(live)
    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(f"Publish report saved: {out}")
    else:
        print(text)

    print(
        f"\nPublished: {live.success_count}/{len(live.results)} ok. Log: {live.log_path}",
        file=sys.stderr,
    )
    return 0 if live.failure_count == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
