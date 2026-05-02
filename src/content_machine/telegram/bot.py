"""Telegram Review Bot.

Sends drafts to the review chat with inline approve/edit/reject/schedule buttons.
Uses python-telegram-bot (async) for clean callback handling.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from src.content_machine.agents.orchestrator import Orchestrator
from src.content_machine.config import ContentMachineConfig
from src.content_machine.content_language import get_content_language
from src.content_machine.db import ContentDB
from src.content_machine.schema import ContentItem, ContentStatus

log = logging.getLogger(__name__)

# Telegram message length limit
_MAX_MSG_LEN = 4096


def _t(es: str, ru: str, en: str) -> str:
    lang = get_content_language()
    if lang.is_spanish:
        return es
    if lang.is_russian:
        return ru
    return en


def _truncate(text: str, limit: int = _MAX_MSG_LEN - 200) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n\n<i>" + _t("... (recortado)", "... (обрезано)", "... (truncated)") + "</i>"


def _format_review_message(item: ContentItem) -> str:
    """Build the review message body sent to the admin."""
    risk = "?"
    flags_text = ""
    if item.compliance:
        risk = item.compliance.risk_tier
        if item.compliance.flags:
            flags_text = "\n".join(f"  ⚠️ {f}" for f in item.compliance.flags[:5])

    h_title = _t("Borrador en revisión", "Черновик на ревью", "Draft for review")
    h_type = _t("Tipo", "Тип", "Type")
    h_goal = _t("Objetivo", "Цель", "Goal")
    h_risk = _t("Riesgo", "Риск", "Risk")
    h_ver = _t("Versión", "Версия", "Version")
    h_date = _t("Fecha", "Дата", "Date")
    h_flags = _t("Marcas", "Флаги", "Flags")
    h_appr = _t("Aprobación requerida", "Нужно одобрение", "Approval required")

    header = (
        f"📝 <b>{h_title}</b>\n"
        f"ID: <code>{item.id}</code>\n"
        f"{h_type}: {item.content_type} | {h_goal}: {item.campaign_goal}\n"
        f"{h_risk}: <b>{risk}</b> | {h_ver}: {item.version}\n"
        f"{h_date}: {item.calendar_slot or '—'}\n"
    )
    if flags_text:
        header += f"\n{h_flags}:\n{flags_text}\n"
    if item.required_approvers:
        header += f"{h_appr}: {', '.join(item.required_approvers)}\n"

    body = f"\n{'─' * 30}\n{item.body}\n{'─' * 30}"
    return _truncate(header + body)


def run_bot(config: ContentMachineConfig, db: ContentDB) -> None:
    """Blocking entry point — starts the Telegram review bot + scheduler.

    Uses `Application.run_polling()` (recommended in python-telegram-bot v22+).
    """
    try:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
        from telegram.ext import (
            Application,
            CallbackQueryHandler,
            CommandHandler,
            ContextTypes,
        )
    except ImportError as exc:
        raise RuntimeError(
            "pip install python-telegram-bot  is required for the review bot"
        ) from exc

    orch = Orchestrator(config, db)

    # ── Keyboard ─────────────────────────────────────────────────────────

    def _review_keyboard(item_id: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    _t("✅ Aprobar", "✅ Одобрить", "✅ Approve"),
                    callback_data=f"approve:{item_id}",
                ),
                InlineKeyboardButton(
                    _t("✏️ Editar", "✏️ Редактировать", "✏️ Edit"),
                    callback_data=f"edit:{item_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    _t("🔄 Regenerar", "🔄 Перегенерировать", "🔄 Regenerate"),
                    callback_data=f"regen:{item_id}",
                ),
                InlineKeyboardButton(
                    _t("❌ Rechazar", "❌ Отклонить", "❌ Reject"),
                    callback_data=f"reject:{item_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    _t("📅 Programar", "📅 Запланировать", "📅 Schedule"),
                    callback_data=f"schedule:{item_id}",
                ),
                InlineKeyboardButton(
                    _t("🚀 Publicar ahora", "🚀 Опубликовать сейчас", "🚀 Publish now"),
                    callback_data=f"publish_now:{item_id}",
                ),
            ],
        ])

    # ── Command handlers ─────────────────────────────────────────────────

    async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(
            _t(
                "🤖 Content Machine — bot de revisión\n\n"
                "Comandos:\n"
                "/plan — plan semanal\n"
                "/process — generar borradores desde briefs\n"
                "/queue — cola de revisión\n"
                "/published — últimas publicaciones\n"
                "/stats — analítica",
                "🤖 Content Machine — бот ревью\n\n"
                "Команды:\n"
                "/plan — сгенерировать план на неделю\n"
                "/process — обработать все брифы\n"
                "/queue — показать очередь на ревью\n"
                "/published — последние опубликованные\n"
                "/stats — аналитика",
                "🤖 Content Machine — review bot\n\n"
                "Commands:\n"
                "/plan — weekly plan\n"
                "/process — draft all briefs\n"
                "/queue — review queue\n"
                "/published — latest published\n"
                "/stats — analytics",
            )
        )

    async def cmd_plan(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(
            _t("⏳ Generando el plan de contenido…", "⏳ Генерирую контент-план на неделю…", "⏳ Generating weekly plan…")
        )
        items = orch.generate_weekly_plan()
        await update.message.reply_text(
            _t(
                f"✅ Creados {len(items)} briefs.\n"
                f"Usa /process para generar borradores.",
                f"✅ Создано {len(items)} брифов.\n"
                f"Используй /process чтобы сгенерировать черновики.",
                f"✅ Created {len(items)} briefs.\n"
                f"Run /process to generate drafts.",
            )
        )

    async def cmd_process(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        briefs = db.list_by_status(ContentStatus.BRIEF.value)
        if not briefs:
            await update.message.reply_text(
                _t("📭 No hay briefs pendientes.", "📭 Нет брифов для обработки.", "📭 No briefs to process.")
            )
            return
        await update.message.reply_text(
            _t(f"⏳ Procesando {len(briefs)} briefs…", f"⏳ Обрабатываю {len(briefs)} брифов…", f"⏳ Processing {len(briefs)} briefs…")
        )

        results = orch.process_all_briefs()
        for item in results:
            if item.status == ContentStatus.NEEDS_REVIEW.value:
                msg = _format_review_message(item)
                await update.message.reply_html(msg, reply_markup=_review_keyboard(item.id))
            elif item.status == ContentStatus.CANCELLED.value:
                await update.message.reply_text(
                    _t(
                        f"🚫 Publicación {item.id} bloqueada por compliance.\n"
                        f"Tema: {item.topic}",
                        f"🚫 Пост {item.id} заблокирован compliance.\n"
                        f"Тема: {item.topic}",
                        f"🚫 Post {item.id} blocked by compliance.\n"
                        f"Topic: {item.topic}",
                    )
                )
        await update.message.reply_text(
            _t(
                f"✅ Procesado(s) {len(results)} publicación(es).",
                f"✅ Обработано {len(results)} постов.",
                f"✅ Processed {len(results)} post(s).",
            )
        )

    async def cmd_queue(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        items = db.list_by_status(ContentStatus.NEEDS_REVIEW.value)
        if not items:
            await update.message.reply_text(
                _t("📭 La cola está vacía.", "📭 Очередь пуста.", "📭 Review queue is empty.")
            )
            return
        for item in items[:10]:
            msg = _format_review_message(item)
            await update.message.reply_html(msg, reply_markup=_review_keyboard(item.id))

    async def cmd_published(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        items = db.list_by_status(ContentStatus.PUBLISHED.value)[:10]
        if not items:
            await update.message.reply_text(
                _t("📭 No hay publicaciones recientes.", "📭 Нет опубликованных постов.", "📭 No published posts.")
            )
            return
        lines = []
        for it in items:
            lines.append(f"• [{it.content_type}] {it.topic[:40]} — {it.publish.scheduled_time or '—'}")
        await update.message.reply_text(
            _t("📋 Últimas publicaciones:\n", "📋 Последние публикации:\n", "📋 Latest publications:\n")
            + "\n".join(lines)
        )

    # ── Callback handlers ────────────────────────────────────────────────

    async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        data = query.data or ""
        action, _, item_id = data.partition(":")

        if not item_id:
            await query.edit_message_text(
                _t("⚠️ Formato de callback desconocido.", "⚠️ Неизвестный формат callback.", "⚠️ Unknown callback format.")
            )
            return

        if action == "approve":
            user = query.from_user
            name = user.full_name if user else "unknown"
            item = orch.approve(item_id, role="founder", approved_by=name)
            if item and item.status == ContentStatus.APPROVED.value:
                await query.edit_message_text(
                    _t(
                        f"✅ Publicación {item_id} aprobada → APPROVED\n"
                        f"Usa Programar o Publicar ahora.",
                        f"✅ Пост {item_id} одобрен → APPROVED\n"
                        f"Используй 📅 Запланировать или 🚀 Опубликовать.",
                        f"✅ Post {item_id} approved → APPROVED\n"
                        f"Use Schedule or Publish now.",
                    )
                )
            elif item:
                await query.edit_message_text(
                    _t(
                        f"👍 Aprobado ({name}), falta: {item.required_approvers}",
                        f"👍 Одобрено ({name}), ещё нужно: {item.required_approvers}",
                        f"👍 Approved ({name}), still need: {item.required_approvers}",
                    )
                )
            else:
                await query.edit_message_text(
                    _t(f"❌ No se encontró {item_id}.", f"❌ Пост {item_id} не найден.", f"❌ Post {item_id} not found.")
                )

        elif action == "reject":
            orch.reject(item_id, reason="rejected via bot")
            await query.edit_message_text(
                _t(
                    f"❌ Publicación {item_id} rechazada → vuelve a BRIEF.",
                    f"❌ Пост {item_id} отклонён → возвращён в BRIEF.",
                    f"❌ Post {item_id} rejected → back to BRIEF.",
                )
            )

        elif action == "regen":
            orch.reject(item_id, reason="regeneration requested")
            item = db.get(item_id)
            if item:
                item = orch.process_brief(item)
                if item.status == ContentStatus.NEEDS_REVIEW.value:
                    msg = _format_review_message(item)
                    await query.edit_message_text(
                        _t("🔄 Regenerado:", "🔄 Перегенерирован:", "🔄 Regenerated:")
                    )
                    await query.message.reply_html(msg, reply_markup=_review_keyboard(item.id))

        elif action == "schedule":
            item = db.get(item_id)
            if item and item.status in (ContentStatus.APPROVED.value, ContentStatus.NEEDS_REVIEW.value):
                item.status = ContentStatus.SCHEDULED.value
                item.publish.scheduled_time = item.calendar_slot or datetime.utcnow().isoformat()
                db.save(item)
                await query.edit_message_text(
                    _t(
                        f"📅 Publicación {item_id} programada: {item.publish.scheduled_time}",
                        f"📅 Пост {item_id} запланирован на {item.publish.scheduled_time}",
                        f"📅 Post {item_id} scheduled: {item.publish.scheduled_time}",
                    )
                )
            else:
                await query.edit_message_text(
                    _t(
                        f"⚠️ No se puede programar {item_id} (estado: {item.status if item else '?'})",
                        f"⚠️ Пост {item_id} нельзя запланировать (статус: {item.status if item else '?'})",
                        f"⚠️ Cannot schedule {item_id} (status: {item.status if item else '?'})",
                    )
                )

        elif action == "publish_now":
            item = db.get(item_id)
            if item:
                if config.shadow_mode:
                    await query.edit_message_text(
                        _t(
                            f"⚠️ Shadow mode activo. {item_id} NO publicado.\n"
                            f"Texto listo para publicación manual.",
                            f"⚠️ Shadow mode включён. Пост {item_id} НЕ опубликован.\n"
                            f"Текст готов к ручной публикации.",
                            f"⚠️ Shadow mode on. Post {item_id} NOT published.\n"
                            f"Text is ready for manual posting.",
                        )
                    )
                else:
                    from src.content_machine.telegram.publisher import publish_item
                    success = await publish_item(config.telegram, item)
                    if success:
                        item.status = ContentStatus.PUBLISHED.value
                        db.save(item)
                        await query.edit_message_text(
                            _t(f"🚀 Publicación {item_id} publicada.", f"🚀 Пост {item_id} опубликован!", f"🚀 Post {item_id} published!")
                        )
                    else:
                        await query.edit_message_text(
                            _t(
                                f"❌ Error al publicar {item_id}",
                                f"❌ Ошибка публикации поста {item_id}",
                                f"❌ Publish error for {item_id}",
                            )
                        )

        elif action == "edit":
            await query.edit_message_text(
                _t(
                    f"✏️ Envía el texto editado en respuesta a este mensaje.\n"
                    f"ID: <code>{item_id}</code>",
                    f"✏️ Отправь отредактированный текст в ответ на это сообщение.\n"
                    f"ID: <code>{item_id}</code>",
                    f"✏️ Send edited text in reply to this message.\n"
                    f"ID: <code>{item_id}</code>",
                ),
                parse_mode="HTML",
            )

    # ── Build and run ────────────────────────────────────────────────────

    sched_task: asyncio.Task[None] | None = None

    async def post_init(app: Application) -> None:  # noqa: ARG001
        nonlocal sched_task
        me = await app.bot.get_me()
        log.info("Telegram bot online: @%s (id=%s)", me.username, me.id)

        # Background scheduler loop (publishing due items)
        from src.content_machine.telegram.scheduler import run_scheduler_loop

        sched_task = asyncio.create_task(
            run_scheduler_loop(config, db),
            name="content_machine_scheduler",
        )

        # Optional: ping review chat to verify chat id and that bot can message you
        try:
            if config.telegram.review_chat_id:
                await app.bot.send_message(
                    chat_id=config.telegram.review_chat_id,
                    text=_t(
                        "✅ Content Machine iniciado.\n\n"
                        "Prueba: /start /queue /plan /process",
                        "✅ Content Machine бот запущен.\n\n"
                        "Попробуй команды: /start /queue /plan /process",
                        "✅ Content Machine started.\n\n"
                        "Try: /start /queue /plan /process",
                    ),
                )
        except Exception:
            log.exception(
                "Failed to send startup ping to TG_REVIEW_CHAT_ID=%r",
                config.telegram.review_chat_id,
            )

    async def post_shutdown(app: Application) -> None:  # noqa: ARG001
        if sched_task is not None and not sched_task.done():
            sched_task.cancel()
            try:
                await sched_task
            except asyncio.CancelledError:
                pass

    async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        log.exception("PTB error: %s", context.error)

    app = (
        Application.builder()
        .token(config.telegram.bot_token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    app.add_error_handler(on_error)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("plan", cmd_plan))
    app.add_handler(CommandHandler("process", cmd_process))
    app.add_handler(CommandHandler("queue", cmd_queue))
    app.add_handler(CommandHandler("published", cmd_published))
    app.add_handler(CallbackQueryHandler(on_callback))

    log.info("Review bot run_polling (shadow_mode=%s)", config.shadow_mode)
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
        close_loop=False,
    )
