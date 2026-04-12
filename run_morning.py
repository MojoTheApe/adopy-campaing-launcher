#!/usr/bin/env python3
"""Morning report entry point — generates analysis_agent/morning_report.md.

Reads stats Excel + last tab of Google Sheet accounts, builds a structured
11-section Markdown report per country, and saves to analysis_agent/morning_report.md.

Usage:
    python run_morning.py
    python run_morning.py -e "C:\\path\\to\\cursor example.xlsx"
    python run_morning.py --no-gsheet          # skip Google Sheet read
    python run_morning.py --country Mexico     # one country only
    python run_morning.py --cm "My Name"       # override CM name
    python run_morning.py --trend-days 14      # wider trend window

Environment variables (.env):
    FB_STATS_EXCEL          path to stats Excel (cursor example.xlsx)
    MY_CM_NAME              your name in CM column (default: Evgeni Sedin)
    GSHEET_ACCOUNTS_ID      Google Sheet ID for accounts tab
    GSHEET_ACCOUNTS_SHEET   tab name or "auto_last" (default: auto_last)
    GOOGLE_CREDENTIALS_JSON path to service-account JSON (default: config/google_credentials.json)
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

# Ensure UTF-8 output on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

try:
    from dotenv import load_dotenv
    load_dotenv(_root / ".env")
except ImportError:
    pass

from config.paths import get_excel_path
from src.analysis_agent.gsheet_accounts import fetch_account_rows
from src.analysis_agent.morning_report import build_morning_report
from src.analysis_agent.morning_markdown import render_morning_report_md

_DEFAULT_OUTPUT = str(_root / "analysis_agent" / "morning_report.md")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate morning_report.md: 11-section analysis per country.",
    )
    parser.add_argument(
        "--excel", "-e",
        default=None,
        help="Path to stats Excel (default: FB_STATS_EXCEL env / config/paths.py)",
    )
    parser.add_argument(
        "--output", "-o",
        default=_DEFAULT_OUTPUT,
        help=f"Output path (default: {_DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--country", "-c",
        default=None,
        help="Filter to one country (exact Excel Country value, e.g. 'Mexico')",
    )
    parser.add_argument(
        "--cm",
        default=None,
        help="Your CM name as in Excel (default: MY_CM_NAME env / 'Evgeni Sedin')",
    )
    parser.add_argument(
        "--trend-days",
        type=int,
        default=7,
        dest="trend_days",
        help="Days window for recent-trend section (default: 7)",
    )
    parser.add_argument(
        "--no-gsheet",
        action="store_true",
        dest="no_gsheet",
        help="Skip Google Sheet read (sections 8-9 will be empty)",
    )
    return parser.parse_args()


def main(argv: list[str] | None = None) -> None:
    args = _parse_args() if argv is None else _parse_args()

    excel_path = (args.excel or "").strip() or get_excel_path()
    if not os.path.isfile(excel_path):
        print(f"[run_morning] ERROR: Excel not found: {excel_path}", file=sys.stderr)
        print("  Set FB_STATS_EXCEL in .env or pass -e path", file=sys.stderr)
        sys.exit(1)

    my_cm = (args.cm or "").strip() or os.environ.get("MY_CM_NAME", "Evgeni Sedin").strip()

    # ── Load Google Sheet account rows ─────────────────────────────────────────
    account_rows: list = []
    if not args.no_gsheet:
        print("[run_morning] Reading Google Sheet accounts...", file=sys.stderr)
        account_rows = fetch_account_rows(my_cm=my_cm)
    else:
        print("[run_morning] --no-gsheet: skipping GSheet read", file=sys.stderr)

    # ── Build report model ─────────────────────────────────────────────────────
    print(f"[run_morning] Analysing: {excel_path}", file=sys.stderr)
    report = build_morning_report(
        excel_path=excel_path,
        account_rows=account_rows,
        my_cm=my_cm,
        trend_days=args.trend_days,
        country_filter=args.country,
    )

    # ── Render to Markdown ─────────────────────────────────────────────────────
    md = render_morning_report_md(report)

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(md, encoding="utf-8")

    n_countries = len(report.get("countries", []))
    n_combos = report.get("total_combos", 0)
    print(
        f"[run_morning] Done. {n_countries} countries, {n_combos} combos.",
        file=sys.stderr,
    )
    print(f"[run_morning] Report saved: {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
