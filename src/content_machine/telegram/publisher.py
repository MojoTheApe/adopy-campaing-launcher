"""Telegram Publisher.

Sends approved content to the target channel/group via Bot API.
Handles text, photo, and video posts with retry logic.
"""

from __future__ import annotations

import logging
from pathlib import Path

from src.content_machine.config import TelegramConfig
from src.content_machine.schema import ContentItem, ContentStatus

log = logging.getLogger(__name__)

_MAX_CAPTION = 1024
_MAX_TEXT = 4096


async def publish_item(tg_config: TelegramConfig, item: ContentItem) -> bool:
    """Publish a single ContentItem to the target Telegram chat.

    Returns True on success, False on failure (item is NOT marked published here;
    the caller is responsible for status transitions).
    """
    try:
        from telegram import Bot
    except ImportError as exc:
        raise RuntimeError("pip install python-telegram-bot") from exc

    bot = Bot(token=tg_config.bot_token)
    chat_id = tg_config.publish_chat_id

    try:
        if item.media_paths:
            media_path = item.media_paths[0]
            ext = Path(media_path).suffix.lower()

            if ext in (".mp4", ".mov", ".avi", ".webm"):
                with open(media_path, "rb") as f:
                    msg = await bot.send_video(
                        chat_id=chat_id,
                        video=f,
                        caption=item.body[:_MAX_CAPTION],
                        parse_mode="HTML",
                    )
            else:
                with open(media_path, "rb") as f:
                    msg = await bot.send_photo(
                        chat_id=chat_id,
                        photo=f,
                        caption=item.body[:_MAX_CAPTION],
                        parse_mode="HTML",
                    )

            item.publish.telegram_message_id = str(msg.message_id)
            item.publish.telegram_chat_id = str(chat_id)

        else:
            msg = await bot.send_message(
                chat_id=chat_id,
                text=item.body[:_MAX_TEXT],
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            item.publish.telegram_message_id = str(msg.message_id)
            item.publish.telegram_chat_id = str(chat_id)

        log.info("Published item %s to chat %s (msg_id=%s)",
                 item.id, chat_id, msg.message_id)
        return True

    except Exception:
        item.publish.retries += 1
        log.exception("Failed to publish item %s (retry #%d)", item.id, item.publish.retries)
        item.publish.last_error = "see logs"
        return False
