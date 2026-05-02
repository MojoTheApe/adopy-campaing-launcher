"""Durable Scheduler.

Uses APScheduler backed by the content DB to fire posts at their scheduled time.
The DB is the source of truth — scheduler state is rebuilt on every restart.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from src.content_machine.config import ContentMachineConfig
from src.content_machine.db import ContentDB
from src.content_machine.schema import ContentItem, ContentStatus
from src.content_machine.telegram.publisher import publish_item

log = logging.getLogger(__name__)

_CHECK_INTERVAL_SECONDS = 60


async def _publish_due_items(config: ContentMachineConfig, db: ContentDB) -> int:
    """Find and publish all items whose scheduled_time has passed.

    Returns the number of successfully published items.
    """
    scheduled = db.list_scheduled()
    now = datetime.now(timezone.utc)
    published = 0

    for item in scheduled:
        sched_time = item.publish.scheduled_time
        if not sched_time:
            continue

        try:
            dt = datetime.fromisoformat(sched_time.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except ValueError:
            log.warning("Item %s has unparseable scheduled_time: %s", item.id, sched_time)
            continue

        if dt > now:
            continue

        if config.shadow_mode:
            log.info("Shadow mode: item %s due but NOT publishing (scheduled %s)", item.id, sched_time)
            continue

        log.info("Publishing scheduled item %s (due %s)", item.id, sched_time)
        item.status = ContentStatus.PUBLISHING.value
        db.save(item)

        success = await publish_item(config.telegram, item)
        if success:
            item.status = ContentStatus.PUBLISHED.value
            published += 1
        else:
            if item.publish.retries >= 3:
                item.status = ContentStatus.FAILED.value
                log.error("Item %s failed after %d retries", item.id, item.publish.retries)
            else:
                item.status = ContentStatus.SCHEDULED.value
        db.save(item)

    return published


async def run_scheduler_loop(config: ContentMachineConfig, db: ContentDB) -> None:
    """Infinite loop that checks for due posts every minute.

    Designed to run as an asyncio task alongside the review bot.
    """
    log.info(
        "Scheduler started (interval=%ds, shadow_mode=%s)",
        _CHECK_INTERVAL_SECONDS, config.shadow_mode,
    )

    while True:
        try:
            count = await _publish_due_items(config, db)
            if count:
                log.info("Scheduler published %d items this cycle", count)
        except Exception:
            log.exception("Scheduler cycle error")

        await asyncio.sleep(_CHECK_INTERVAL_SECONDS)


def start_scheduler(config: ContentMachineConfig, db: ContentDB) -> None:
    """Blocking entry point — runs the scheduler in a new event loop."""
    asyncio.run(run_scheduler_loop(config, db))
