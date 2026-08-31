#!/usr/bin/env python3
"""Content Machine — entry point.

Usage (from repo root fb_agents):

    # Start the Telegram review bot + scheduler (main mode)
    python run_content_machine.py bot

    # Generate a weekly content plan (no bot needed)
    python run_content_machine.py plan
    python run_content_machine.py plan -w config/content_plan_week.example.json

    # Process all pending briefs into drafts
    python run_content_machine.py process

    # Generate a video script for an item
    python run_content_machine.py video <item_id>

    # Show recent items
    python run_content_machine.py list

    # Show review queue (needs_review) — same as Telegram /queue, but in terminal
    python run_content_machine.py queue

    # Run analytics summary
    python run_content_machine.py analytics

    # After changing CM_CONTENT_LANG: rebuild drafts (e.g. needs_review → BRIEF, then process)
    python run_content_machine.py relocalize --status needs_review
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT))

from config.paths import load_env

load_env()

from src.content_machine.config import ContentMachineConfig
from src.content_machine.content_language import get_content_language
from src.content_machine.db import ContentDB
from src.content_machine.schema import ContentStatus

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
log = logging.getLogger("content_machine")


def _get_config_and_db() -> tuple[ContentMachineConfig, ContentDB]:
    cfg = ContentMachineConfig.from_env()
    db = ContentDB(cfg.db_path)
    return cfg, db


def cmd_bot(args: argparse.Namespace) -> None:
    """Start the Telegram review bot with the scheduler running alongside."""
    cfg, db = _get_config_and_db()
    errors = cfg.validate()
    if errors:
        for e in errors:
            log.error("Config error: %s", e)
        sys.exit(1)

    from src.content_machine.telegram.bot import run_bot

    run_bot(cfg, db)


def cmd_plan(args: argparse.Namespace) -> None:
    """Generate a weekly content plan."""
    from src.content_machine.weekly_plan_input import (
        WeeklyPlanInput,
        load_weekly_plan_input,
        load_weekly_plan_input_from_env,
    )

    cfg, db = _get_config_and_db()
    from src.content_machine.agents.orchestrator import Orchestrator

    orch = Orchestrator(cfg, db)

    if getattr(args, "week_plan_file", None):
        weekly = load_weekly_plan_input(args.week_plan_file)
    else:
        env_wk = load_weekly_plan_input_from_env()
        weekly = env_wk if env_wk is not None else WeeklyPlanInput.empty()

    items = orch.generate_weekly_plan(
        context=args.context or "",
        weekly_plan=weekly,
    )
    print(f"\n✅ Created {len(items)} briefs:")
    for it in items:
        print(f"  [{it.calendar_slot}] {it.content_type:20s} {it.topic}")


def cmd_process(args: argparse.Namespace) -> None:
    """Process all pending briefs through the full pipeline."""
    cfg, db = _get_config_and_db()
    from src.content_machine.agents.orchestrator import Orchestrator
    orch = Orchestrator(cfg, db)
    results = orch.process_all_briefs()
    print(f"\n✅ Processed {len(results)} items:")
    for it in results:
        risk = it.compliance.risk_tier if it.compliance else "?"
        print(f"  {it.id} [{it.status}] risk={risk} {it.topic[:50]}")


def cmd_video(args: argparse.Namespace) -> None:
    """Generate a video script and run the pipeline for an item."""
    cfg, db = _get_config_and_db()
    item = db.get(args.item_id)
    if not item:
        print(f"❌ Item {args.item_id} not found")
        sys.exit(1)

    from src.content_machine.video.script_agent import generate_script
    from src.content_machine.video.pipeline import run_video_job

    script = generate_script(cfg.llm, item)
    print(f"🎬 Script: {script.title} ({len(script.scenes)} scenes, {script.total_duration:.0f}s)")

    job = asyncio.run(run_video_job(script))
    print(f"📹 Job {job.job_id}: status={job.status}")
    if job.output_path:
        print(f"   Output: {job.output_path}")
        item.media_paths = [job.output_path]
        db.save(item)


def cmd_list(args: argparse.Namespace) -> None:
    """Show recent content items."""
    _, db = _get_config_and_db()
    items = db.list_recent(limit=args.limit)
    if not items:
        print("📭 No items found.")
        return
    print(f"\n📋 Last {len(items)} items:")
    for it in items:
        risk = it.compliance.risk_tier if it.compliance else "—"
        print(f"  {it.id} [{it.status:16s}] risk={risk:8s} {it.content_type:20s} {it.topic[:40]}")


def cmd_queue(args: argparse.Namespace) -> None:
    """Items waiting for review (status needs_review) — same thing as /queue in Telegram bot."""
    _, db = _get_config_and_db()
    items = db.list_by_status(ContentStatus.NEEDS_REVIEW.value)[: int(args.limit)]
    lang = get_content_language()
    if not items:
        if lang.is_spanish:
            print("📭 Cola vacía (needs_review).")
        elif lang.is_russian:
            print("📭 Очередь пуста (needs_review).")
        else:
            print("📭 Review queue is empty (needs_review).")
        return
    if lang.is_spanish:
        print(f"\n⏳ En revisión (mostrando {len(items)}):")
    elif lang.is_russian:
        print(f"\n⏳ Очередь на ревью (показано {len(items)}):")
    else:
        print(f"\n⏳ Review queue (showing {len(items)}):")
    for it in items:
        risk = it.compliance.risk_tier if it.compliance else "—"
        print(f"  {it.id} [risk={risk:8s}] {it.content_type:20s} {it.topic[:50]}")


def cmd_relocalize(args: argparse.Namespace) -> None:
    """Move items in a status back to BRIEF and re-run the pipeline (for language / prompt changes)."""
    cfg, db = _get_config_and_db()
    from src.content_machine.agents.orchestrator import Orchestrator
    orch = Orchestrator(cfg, db)
    count = orch.relocalize_status(args.status)
    results = orch.process_all_briefs()
    print(f"Reset {count} item(s) from {args.status} → BRIEF, reprocessed {len(results)} brief(s).")
    for it in results:
        risk = it.compliance.risk_tier if it.compliance else "?"
        print(f"  {it.id} [{it.status}] risk={risk} {it.topic[:50]}")


def cmd_analytics(args: argparse.Namespace) -> None:
    """Run analytics on published posts."""
    cfg, db = _get_config_and_db()
    published = db.list_by_status(ContentStatus.PUBLISHED.value)
    if not published:
        print("📭 No published items to analyze.")
        return

    from src.content_machine.agents.analytics_agent import generate_summary
    summary = generate_summary(cfg.llm, published)
    print(f"\n📊 Analytics summary:\n{summary}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Content Machine — AI-driven Telegram content pipeline",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("bot", help="Start Telegram review bot + scheduler")

    p_plan = sub.add_parser("plan", help="Generate weekly content plan")
    p_plan.add_argument("--context", "-c", default="", help="Extra context for planning")
    p_plan.add_argument(
        "--week-plan",
        "-w",
        dest="week_plan_file",
        default=None,
        metavar="PATH",
        help="JSON or YAML file with weekly strategy (overrides CM_WEEK_PLAN_PATH / CM_WEEK_PLAN_JSON if set)",
    )

    sub.add_parser("process", help="Process all pending briefs")

    p_video = sub.add_parser("video", help="Generate video for an item")
    p_video.add_argument("item_id", help="Content item ID")

    p_list = sub.add_parser("list", help="Show recent items")
    p_list.add_argument("--limit", "-n", type=int, default=20)

    p_queue = sub.add_parser("queue", help="Show items in review queue (needs_review)")
    p_queue.add_argument("--limit", "-n", type=int, default=50)

    sub.add_parser("analytics", help="Run analytics on published posts")

    p_reloc = sub.add_parser(
        "relocalize",
        help="Reset items in a status to BRIEF and regenerate drafts (e.g. after CM_CONTENT_LANG change)",
    )
    p_reloc.add_argument(
        "--status", "-s",
        default=ContentStatus.NEEDS_REVIEW.value,
        help="Which status to reset to BRIEF (default: needs_review)",
    )

    args = parser.parse_args()

    commands = {
        "bot": cmd_bot,
        "plan": cmd_plan,
        "process": cmd_process,
        "video": cmd_video,
        "list": cmd_list,
        "queue": cmd_queue,
        "analytics": cmd_analytics,
        "relocalize": cmd_relocalize,
    }

    func = commands.get(args.command)
    if func is None:
        parser.print_help()
        sys.exit(1)
    func(args)


if __name__ == "__main__":
    main()
