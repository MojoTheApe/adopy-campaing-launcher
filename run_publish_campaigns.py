#!/usr/bin/env python3
"""Legacy Agent 9 plan/validation CLI.

Live Meta API creation is intentionally not exposed by the ACL distribution.
Campaign managers create drafts through the dashboard RPA and a human performs
the final Publish action in Meta.
"""
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
    from config.paths import load_env

    load_env()
except Exception:
    pass

from src.meta_campaign_agent.publish import build_dry_run_report, report_to_json


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Agent 9 legacy plan validation (live Meta API creation disabled)",
    )
    p.add_argument("-i", "--input", required=True, help="Agent 8 output .xlsx")
    p.add_argument("--account", help="Only one account sheet (partial name match)")
    p.add_argument("--dry-run", action="store_true", help="Print JSON plan; no Graph API calls")
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
            activate=False,
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

    print(
        "Live Meta API creation is disabled in ACL. Use --dry-run to validate "
        "the plan, then use Campaign Builder to create a draft for manual review.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
