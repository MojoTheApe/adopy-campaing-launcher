"""Post Writer Agent.

Produces Telegram-native draft posts from briefs and research context.
"""

from __future__ import annotations

import logging

from src.content_machine.config import LLMConfig
from src.content_machine.content_language import get_content_language
from src.content_machine.llm import call_llm
from src.content_machine.policy.brand_voice import get_writer_system_prompt
from src.content_machine.schema import ContentItem, ContentStatus

log = logging.getLogger(__name__)


def _system_suffix() -> str:
    lang = get_content_language()
    if lang.is_spanish:
        return """

Escribes publicaciones para un canal de Telegram. Formato HTML (Telegram admite
<b>, <i>, <u>, <s>, <code>, <a href="">).

Para cada publicación:
1. Empieza con un gancho (la primera frase capta atención).
2. Cuerpo: 2-4 párrafos cortos.
3. Termina con un CTA claro.
4. Longitud total hasta 800 caracteres (salvo que el formato requiera más).

Responde SOLO con el texto de la publicación, sin metadatos ni explicaciones.
"""

    if lang.is_russian:
        return """\

Ты пишешь посты для Telegram-канала. Формат — HTML (Telegram поддерживает
<b>, <i>, <u>, <s>, <code>, <a href="">).

Для каждого поста:
1. Начни с яркого хука (первое предложение цепляет внимание).
2. Основная часть — 2-4 коротких абзаца.
3. Завершай одним чётким CTA.
4. Общая длина — до 800 символов (если формат не требует больше).

Верни ТОЛЬКО текст поста, без метаданных и пояснений.
"""

    return """

You write posts for a Telegram channel. Use HTML (Telegram supports
<b>, <i>, <u>, <s>, <code>, <a href="">).

For each post:
1. Start with a strong hook (first sentence grabs attention).
2. Main body: 2-4 short paragraphs.
3. End with one clear CTA.
4. Keep total length under 800 characters unless the format needs more.

Return ONLY the post text, no metadata or explanations.
"""


def write_draft(
    config: LLMConfig,
    item: ContentItem,
    research_context: str = "",
) -> ContentItem:
    """Generate a draft post body and update the ContentItem in-place."""
    lang = get_content_language()
    system = get_writer_system_prompt() + _system_suffix()
    if lang.is_spanish:
        user_msg = (
            f"Tipo: {item.content_type}\n"
            f"Objetivo: {item.campaign_goal}\n"
            f"Audiencia: {item.audience_segment}\n"
            f"Tema: {item.topic}\n"
            f"Brief: {item.brief}\n\n"
            f"Contexto de research:\n{research_context or '(ninguno)'}\n\n"
            "Escribe la publicación en español."
        )
    elif lang.is_russian:
        user_msg = (
            f"Тип: {item.content_type}\n"
            f"Цель: {item.campaign_goal}\n"
            f"Аудитория: {item.audience_segment}\n"
            f"Тема: {item.topic}\n"
            f"Бриф: {item.brief}\n\n"
            f"Research-контекст:\n{research_context or '(нет)'}\n\n"
            "Напиши пост."
        )
    else:
        user_msg = (
            f"Type: {item.content_type}\n"
            f"Goal: {item.campaign_goal}\n"
            f"Audience: {item.audience_segment}\n"
            f"Topic: {item.topic}\n"
            f"Brief: {item.brief}\n\n"
            f"Research context:\n{research_context or '(none)'}\n\n"
            "Write the post."
        )

    body = call_llm(config, system, user_msg)

    item.body = body
    item.status = ContentStatus.DRAFT.value
    item.model_used = config.model
    log.info("Post writer produced draft (%d chars) for item %s", len(body), item.id)
    return item
