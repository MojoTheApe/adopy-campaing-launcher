#!/usr/bin/env python3
"""Unified entry point from project root (fb_agents).

Subcommands (recommended):

  python run.py analyze …     — Agent 1 only (Markdown / console report).
  python run.py upload-bridge … — Stats Excel → combos → upload .xlsx (Agents 1+8).
  python run.py upload-plan … — Agent 8 only from a plan CSV.

Legacy: if the first argument is not a subcommand name, all arguments are passed
to **analyze** (same as before this file gained subcommands).
"""
from __future__ import annotations

import os
import sys

_root = os.path.dirname(os.path.abspath(__file__))
if _root not in sys.path:
    sys.path.insert(0, _root)
os.chdir(_root)

_ANALYZE_ALIASES = frozenset({"analyze", "report"})
_UPLOAD_BRIDGE_ALIASES = frozenset({"upload-bridge", "bridge", "analysis-upload"})
_UPLOAD_PLAN_ALIASES = frozenset({"upload-plan", "plan"})
_MORNING_ALIASES = frozenset({"morning", "morning-report"})


def _is_subcommand(token: str) -> bool:
    t = (token or "").strip().lower()
    return t in _ANALYZE_ALIASES | _UPLOAD_BRIDGE_ALIASES | _UPLOAD_PLAN_ALIASES | _MORNING_ALIASES


def _run_analyze(argv_tail: list[str]) -> None:
    sys.argv = [sys.argv[0], *argv_tail]
    from src.analysis_agent.analyze_fb_stats import (  # noqa: PLC0415
        _merge_account_substrings,
        _parse_args,
        _parse_focus_countries,
        main,
    )

    args = _parse_args()
    acc_subs = _merge_account_substrings(
        getattr(args, "accounts", None),
        getattr(args, "account_substr", None),
    )
    fc = _parse_focus_countries(getattr(args, "focus_countries", None))
    main(
        excel_path=args.excel,
        country_offer=args.country,
        top_n=args.top,
        output_path=args.output,
        short_report=getattr(args, "short", False),
        with_context=getattr(args, "with_context", False),
        asset_filter=getattr(args, "asset", None),
        account_substrings=acc_subs,
        focus_countries=fc,
    )


def _run_upload_bridge(argv_tail: list[str]) -> None:
    from run_analysis_then_upload import main as bridge_main  # noqa: PLC0415

    bridge_main(argv_tail)


def _run_morning(argv_tail: list[str]) -> None:
    from run_morning import main as morning_main  # noqa: PLC0415

    sys.argv = [sys.argv[0], *argv_tail]
    morning_main()


def _run_upload_plan(argv_tail: list[str]) -> None:
    from src.upload_agent.build_upload_excel import (  # noqa: PLC0415
        build_upload_plan_argument_parser,
        run_upload_plan_from_args,
    )

    args = build_upload_plan_argument_parser().parse_args(argv_tail)
    code = run_upload_plan_from_args(args)
    if code != 0:
        sys.exit(code)


def _print_cli_help() -> None:
    print(
        """fb_agents - unified CLI

  python run.py morning [OPTIONS]     Daily morning report (11 sections per country)
                                      (same flags as: python run_morning.py -h)

  python run.py analyze [OPTIONS]     Agent 1: FB stats -> Markdown report
                                      (same flags as: python src/analysis_agent/analyze_fb_stats.py -h)

  python run.py upload-bridge [OPTIONS]   Agents 1+8: stats -> upload workbook
                                      (same flags as: python run_analysis_then_upload.py -h)

  python run.py upload-plan [OPTIONS] PLAN.csv   Agent 8: plan CSV -> upload workbook
                                      (positional plan + -o / -e like build_upload_excel)

Environment:
  FB_STATS_EXCEL          - default stats workbook path
  UPLOAD_FB_EXCEL         - Upload FB template path
  MY_CM_NAME              - your CM name (for morning report)
  GSHEET_ACCOUNTS_ID      - Google Sheet ID for accounts tab
  GSHEET_ACCOUNTS_SHEET   - tab name or "auto_last" (morning report)

Legacy: python run.py --country all ...  is treated as analyze --country all ...
"""
    )


def main() -> None:
    argv = sys.argv[1:]
    if not argv:
        _print_cli_help()
        return
    if argv[0].strip().lower() in ("-h", "--help", "help"):
        _print_cli_help()
        return

    head = argv[0].strip().lower()
    if not _is_subcommand(head):
        _run_analyze(argv)
        return

    tail = argv[1:]
    if head in _MORNING_ALIASES:
        _run_morning(tail)
        return
    if head in _ANALYZE_ALIASES:
        if not tail or tail[0].strip().lower() in ("-h", "--help"):
            _run_analyze(["-h"])
            return
        _run_analyze(tail)
        return
    if head in _UPLOAD_BRIDGE_ALIASES:
        if not tail or tail[0].strip().lower() in ("-h", "--help"):
            from run_analysis_then_upload import build_bridge_argument_parser  # noqa: PLC0415

            build_bridge_argument_parser().print_help()
            return
        _run_upload_bridge(tail)
        return
    if head in _UPLOAD_PLAN_ALIASES:
        if not tail or tail[0].strip().lower() in ("-h", "--help"):
            from src.upload_agent.build_upload_excel import (  # noqa: PLC0415
                build_upload_plan_argument_parser,
            )

            build_upload_plan_argument_parser().print_help()
            return
        _run_upload_plan(tail)
        return

    _print_cli_help()
    sys.exit(2)


if __name__ == "__main__":
    main()
