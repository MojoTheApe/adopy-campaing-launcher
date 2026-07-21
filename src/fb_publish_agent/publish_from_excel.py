#!/usr/bin/env python3
"""
Agent 10: publish campaigns from upload_plan.xlsx (Agent 8 output) into Facebook Ads Manager
via Playwright. Safety default is --dry-run behavior (no --live flag = fills the wizard,
screenshots it, does NOT click Publish) — pass --live only after calibrating selectors.json
against a real account. See docs/agent10_fb_ads_playwright_upload.md.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_proj_root = Path(__file__).resolve().parents[2]
if str(_proj_root) not in sys.path:
    sys.path.insert(0, str(_proj_root))

from config.paths import get_upload_fb_excel_path

from .runner import run


def main() -> None:
    parser = argparse.ArgumentParser(description="Agent 10: publish upload_plan.xlsx (Agent 8 output) into Facebook Ads Manager")
    parser.add_argument("plan", help="Path to upload_plan.xlsx (output of Agent 8 / run_upload_from_plan.py)")
    parser.add_argument("--account", default=None, help="Only publish rows from this account's sheet (default: all sheets)")
    parser.add_argument("--limit", type=int, default=None, help="Max rows to process this run")
    parser.add_argument("--live", action="store_true", help="Actually click Publish (default: dry-run — fills the wizard, screenshots before Publish, does not submit)")
    parser.add_argument("--headless", action="store_true", help="Run browser headless (default: headed — recommended, easier to debug and less likely to be flagged as a bot)")
    parser.add_argument("--slow-mo", type=int, default=250, help="Milliseconds of artificial delay per Playwright action (default 250)")
    parser.add_argument("--auth-state", default=None, help="Path to saved session (default: FB_ADS_AUTH_STATE / config)")
    parser.add_argument("--ads-manager-url", default=None, help="Ads Manager URL to start from (default: FB_ADS_MANAGER_URL / config)")
    parser.add_argument("--reference", default=None, help="Path to Upluad FB.xlsx, used to resolve ad copy text by 'Copy' name (default: UPLOAD_FB_EXCEL / config)")
    parser.add_argument("--output-dir", default=None, help="Where to write screenshots/trace/summary per run (default: FB_PUBLISH_OUTPUT_DIR / config)")
    args = parser.parse_args()

    if not os.path.isfile(args.plan):
        print(f"Plan file not found: {args.plan}")
        sys.exit(1)

    reference = (args.reference or "").strip() or get_upload_fb_excel_path()

    if args.live:
        print("*** --live: this WILL click Publish and create real campaigns in Facebook Ads Manager. ***")
    else:
        print("Dry-run mode (default): filling the wizard and screenshotting before Publish, not submitting. Use --live to actually publish.")

    try:
        run(
            plan_path=args.plan,
            account=args.account,
            limit=args.limit,
            dry_run=not args.live,
            headless=args.headless,
            slow_mo=args.slow_mo,
            auth_state_path=args.auth_state,
            ads_manager_url=args.ads_manager_url,
            reference_excel_path=reference,
            output_dir=args.output_dir,
        )
    except RuntimeError as e:
        print(f"Aborted: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
