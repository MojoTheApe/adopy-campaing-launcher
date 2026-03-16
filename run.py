#!/usr/bin/env python3
"""Entry point: run from project root (fb_agents)."""
import os
import sys

_root = os.path.dirname(os.path.abspath(__file__))
if _root not in sys.path:
    sys.path.insert(0, _root)
os.chdir(_root)

from src.analysis_agent.analyze_fb_stats import main, _parse_args

if __name__ == "__main__":
    args = _parse_args()
    main(
        excel_path=args.excel,
        country_offer=args.country,
        top_n=args.top,
        output_path=args.output,
        short_report=getattr(args, "short", False),
        problematic_report=getattr(args, "problematic", False),
    )
