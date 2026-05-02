"""Editor / Brand QA Agent.

Polishes draft text for readability, tone consistency, and Telegram formatting
while preserving compliance-safe language.
"""

from __future__ import annotations

import logging

from src.content_machine.config import LLMConfig
from src.content_machine.content_language import get_content_language
from src.content_machine.llm import call_llm
from src.content_machine.schema import ContentItem

log = logging.getLogger(__name__)

def _system_prompt() -> str:
    lang = get_content_language()
    if lang.is_spanish:
        return """\
Eres editor de un canal de trading en Telegram. Te dan un borrador.

Tarea:
1) Mejorar legibilidad y ritmo
2) Revisar HTML de Telegram (<b>, <i>, <a href="">)
3) Quitar repeticiones y clichés
4) Conservar TODOS los disclaimers/avisos de riesgo: no los borres ni los debilites
5) Si supera 900 caracteres, acorta sin perder el sentido

Responde SOLO con el texto editado.
"""
    if lang.is_russian:
        return """\
Ты — редактор Telegram-канала о трейдинге. Тебе дают черновик поста.

Твоя задача:
1. Улучшить читаемость и ритм текста.
2. Проверить Telegram HTML-разметку (<b>, <i>, <a href="">).
3. Убрать повторы и клише.
4. Сохранить ВСЕ дисклеймеры и предупреждения о рисках — не удаляй и не ослабляй их.
5. Если пост длиннее 900 символов, сократи без потери смысла.

Верни ТОЛЬКО отредактированный текст поста.
"""
    return """\
You edit drafts for a trading Telegram channel.

Task:
1) Improve readability and pacing
2) Check Telegram HTML (<b>, <i>, <a href="">)
3) Remove repetition and empty clichés
4) Keep ALL risk disclaimers — do not remove or weaken them
5) If longer than 900 characters, trim without losing meaning

Return ONLY the edited post text.
"""


def edit_draft(config: LLMConfig, item: ContentItem) -> ContentItem:
    """Return the item with an edited body. Preserves compliance text."""
    lang = get_content_language()
    if lang.is_spanish:
        user_msg = f"Tipo: {item.content_type}\nBorrador:\n{item.body}\n"
    elif lang.is_russian:
        user_msg = f"Тип: {item.content_type}\nЧерновик:\n{item.body}\n"
    else:
        user_msg = f"Type: {item.content_type}\nDraft:\n{item.body}\n"

    edited = call_llm(config, _system_prompt(), user_msg, temperature=0.3)
    item.body = edited
    item.version += 1
    log.info("Editor agent polished item %s (v%d, %d chars)", item.id, item.version, len(edited))
    return item
