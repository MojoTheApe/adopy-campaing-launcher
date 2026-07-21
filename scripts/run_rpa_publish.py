#!/usr/bin/env python3
"""PoC: RPA publish one campaign tree via Multilogin/Dolphin + Playwright.

Usage:
  python scripts/run_rpa_publish.py --account "EC Fav" --country MX --dry-run
  python scripts/run_rpa_publish.py --account "EC Fav" --country MX --slots-json path.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv

load_dotenv(_ROOT / ".env")

from src.fb_ads_rpa.preview import preview_campaign_drafts
from src.fb_ads_rpa.rpa_publisher import RpaPublisher


def _demo_slots() -> list[dict]:
    """Two ads in one campaign — PoC default."""
    return [
        {
            "name": "RPA_PoC_Test",
            "targeting": "MX Broad",
            "os": "All",
            "age": "35-65",
            "budget": "10",
            "creative_type": "Image",
            "creative_id": "1",
            "copy": "Copy 1",
        },
        {
            "name": "RPA_PoC_Test",
            "targeting": "MX Broad",
            "os": "All",
            "age": "35-65",
            "budget": "10",
            "creative_type": "Image",
            "creative_id": "2",
            "copy": "Copy 2",
        },
    ]


def main() -> int:
    p = argparse.ArgumentParser(description="RPA publish PoC (Playwright + antidetect)")
    p.add_argument("--account", required=True, help="Account label from ad_account_map.csv")
    p.add_argument("--country", default="MX", help="Country code")
    p.add_argument("--slots-json", help="JSON file with dashboard slot list")
    p.add_argument("--dry-run", action="store_true", help="Open Ads Manager, stop before Publish")
    p.add_argument("--preview-only", action="store_true", help="No browser — validate drafts only")
    p.add_argument("--activate", action="store_true", help="Set status ACTIVE instead of PAUSED")
    p.add_argument("--browser", choices=("multilogin", "dolphin"), help="Override FB_BROWSER_PROVIDER")
    args = p.parse_args()

    slots = _demo_slots()
    if args.slots_json:
        slots = json.loads(Path(args.slots_json).read_text(encoding="utf-8"))

    publisher = RpaPublisher(browser_provider=args.browser, dry_run=args.dry_run or args.preview_only)

    if args.preview_only:
        drafts = publisher.build_drafts(
            slots,
            account_label=args.account,
            country=args.country,
            activate=args.activate,
            preview_mode=True,
        )
        print(json.dumps(preview_campaign_drafts(drafts), indent=2, ensure_ascii=False))
        has_err = any(d.errors for d in drafts)
        return 1 if has_err else 0

    results = publisher.publish_slots(
        slots,
        account_label=args.account,
        country=args.country,
        activate=args.activate,
    )
    for r in results:
        print(json.dumps(r.to_job_dict(), indent=2, ensure_ascii=False))
    return 0 if all(r.success for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
