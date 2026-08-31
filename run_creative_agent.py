#!/usr/bin/env python3
"""Creative Intelligence Agent — analyze an ad creative and generate a new creative brief.

Usage:
    python run_creative_agent.py --file creatives/input/MX_Amz_All_img_Finance_35.jpg
    python run_creative_agent.py --all                  # process all files in creatives/input/
    python run_creative_agent.py --file <path> --no-vision  # skip GPT-4o Vision (faster)

Output:
    creatives/output/<stem>_brief.md  — full Markdown brief + AI/designer prompt
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
os.chdir(_ROOT)

# Ensure UTF-8 output on Windows so Cyrillic KB text doesn't crash
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

try:
    from config.paths import load_env

    load_env()
except Exception:
    pass

from src.creative_agent.creative_scanner import scan_file, scan_input_dir, SUPPORTED_EXTS
from src.creative_agent.audience_builder import build_audience_profile
from src.creative_agent.stats_correlator import correlate
from src.creative_agent.vision_analyzer import analyze as vision_analyze
from src.creative_agent.prompt_generator import generate_brief


def _process_file(path: Path, *, no_vision: bool = False) -> None:
    print(f"\n{'─' * 60}")
    print(f"  File: {path.name}")
    print(f"{'─' * 60}")

    # 1. Scan
    scan = scan_file(path)
    print(f"  Interest:     {scan['interest'] or '(not parsed)'}")
    print(f"  Text segment: {scan['text_segment']}")
    print(f"  Offer:        {scan['offer_raw'] or '(not parsed)'}")
    print(f"  Country:      {scan['country_code'] or '(not parsed)'}")

    # 2. Audience profile
    print("  [1/3] Building audience profile from KB...")
    audience = build_audience_profile(scan["interest"], scan["country_code"])
    kb_status = "✓ found in KB" if audience["kb_found"] else "⚠ not in KB"
    print(f"        {kb_status}: {audience['label']}")

    # 3. Stats correlation
    print("  [2/3] Correlating with Excel stats...")
    stats = correlate(
        scan["interest"],
        scan["text_segment"],
        scan["country_code"],
    )
    if stats["excel_found"]:
        cell = stats.get("cell", {})
        deps = cell.get("deposits", 0)
        by_int = len(stats.get("by_interest", []))
        print(f"        Cell deposits: {deps} | Related combos: {by_int}")
    else:
        print("        Excel not found — stats skipped")

    # 4. Vision analysis
    vision: dict
    if no_vision:
        from src.creative_agent.vision_analyzer import _empty_analysis
        vision = _empty_analysis("--no-vision flag set")
        print("  [3/3] Vision analysis skipped (--no-vision)")
    else:
        print("  [3/3] Running GPT-4o Vision analysis...")
        vision = vision_analyze(path)
        if vision.get("error"):
            print(f"        ⚠ {vision['error']}")
        else:
            print(f"        Style: {vision.get('style')} | Pattern: {vision.get('visual_pattern')}")

    # 5. Generate brief
    result = generate_brief(scan, vision, stats, audience)
    out_path = result["output_path"]
    print(f"\n  ✓ Brief saved: {out_path.relative_to(_ROOT)}")
    print("\n  ── Creative Prompt ──────────────────────────────────────")
    for line in result["prompt"].splitlines():
        print(f"  {line}")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Analyze ad creatives and generate new creative briefs."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--file", metavar="PATH", help="Single creative file to analyze")
    group.add_argument("--all", action="store_true", help="Process all files in creatives/input/")
    parser.add_argument(
        "--no-vision",
        action="store_true",
        help="Skip GPT-4o Vision (KB + stats analysis only)",
    )
    args = parser.parse_args()

    if args.file:
        p = Path(args.file)
        if not p.is_absolute():
            p = _ROOT / p
        if not p.is_file():
            print(f"ERROR: File not found: {p}")
            sys.exit(1)
        if p.suffix.lower() not in SUPPORTED_EXTS:
            print(f"ERROR: Unsupported file type: {p.suffix}")
            sys.exit(1)
        _process_file(p, no_vision=args.no_vision)

    else:  # --all
        files = scan_input_dir()
        if not files:
            print("No creative files found in creatives/input/")
            print("Add files (.jpg, .png, .mp4, etc.) and retry.")
            sys.exit(0)
        print(f"Found {len(files)} file(s) in creatives/input/")
        for meta in files:
            _process_file(meta["path"], no_vision=args.no_vision)

    print("Done.")


if __name__ == "__main__":
    main()
