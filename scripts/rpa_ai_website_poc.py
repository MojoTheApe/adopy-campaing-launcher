#!/usr/bin/env python3
"""PoC: Conversion location → Website recovery on an open Multilogin profile (CDP).

Prereq:
  1) Ads Manager wizard open on Ad set (Conversion location visible)
  2) Multilogin profile launched with remote debugging (RPA/Dashboard publish does this)
  3) Optional: ANTHROPIC_API_KEY or OPENAI_API_KEY for vision fallback

Usage:
  python scripts/rpa_ai_website_poc.py --discover-cdp
  python scripts/rpa_ai_website_poc.py --cdp-port 50558
  python scripts/rpa_ai_website_poc.py --cdp-url http://127.0.0.1:50558
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description="AI Website conversion recovery over MLX CDP")
    parser.add_argument("--cdp-port", type=int, default=0)
    parser.add_argument("--cdp-url", default="")
    parser.add_argument(
        "--discover-cdp",
        action="store_true",
        help="List Mimic remote-debugging ports and use the first one",
    )
    parser.add_argument("--list-only", action="store_true", help="Only print discovered ports")
    args = parser.parse_args()

    from src.fb_ads_rpa.ai_website_recovery import discover_cdp_ports, run_website_recovery

    if args.discover_cdp or args.list_only or not args.cdp_url and not args.cdp_port:
        ports = discover_cdp_ports()
        print("CDP ports:", ports or "(none — open MLX profile via dashboard RPA first)")
        if args.list_only:
            return 0 if ports else 1
        if not ports and not args.cdp_port and not args.cdp_url:
            return 1
        if ports and not args.cdp_port and not args.cdp_url:
            args.cdp_port = ports[0]
            print("Using port", args.cdp_port)

    cdp_url = args.cdp_url.strip() or f"http://127.0.0.1:{args.cdp_port}"
    print("Connecting", cdp_url)
    result = run_website_recovery(cdp_url=cdp_url)
    print(
        json.dumps(
            {
                "ok": result.ok,
                "strategy": result.strategy,
                "detail": result.detail,
                "screenshot": result.screenshot,
                "actions": result.actions,
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    if result.ok:
        print(
            "\nRisky win logged as pending_approval in "
            "config/rpa_variations/strategy_ranks.json — approve before promoting."
        )
    return 0 if result.ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
