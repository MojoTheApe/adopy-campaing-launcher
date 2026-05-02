"""Sales Copy Agent.

Writes higher-risk conversion posts for signals and copytrading offers.
Always routed through compliance before approval.
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

Eres copywriter de posts de conversión para una comunidad de trading.
Productos: suscripción a señales y copytrading.

Reglas:
1. Nunca prometas beneficio garantizado.
2. Menciona siempre que el trading conlleva riesgos.
3. Usa prueba social (cifras reales si existen) sin prometer resultados “típicos”.
4. Un CTA claro: suscripción, conectar copytrading, o DM.
5. Valor, no presión agresiva.
6. Formato HTML de Telegram, hasta 1000 caracteres.

Responde SOLO con el texto del post.
"""

    if lang.is_russian:
        return """\

Ты — копирайтер, специализирующийся на продающих постах для трейдинг-сообщества.
Продукты: подписка на торговые сигналы и копитрейдинг.

Правила:
1. Никогда не обещай гарантированную прибыль.
2. Всегда упоминай, что торговля связана с рисками.
3. Используй социальное доказательство (реальные цифры, если даны), но без обещания типичности.
4. Один чёткий CTA: подписка, подключение к копитрейдингу, обращение в ЛС.
5. Создавай ощущение ценности, а не давления.
6. Формат — Telegram HTML, до 1000 символов.

Верни ТОЛЬКО текст поста.
"""

    return """

You are a conversion copywriter for a trading community.
Products: signal subscription and copytrading.

Rules:
1. Never promise guaranteed profit.
2. Always mention trading involves risk.
3. Use social proof (real numbers if provided) without promising typicality.
4. One clear CTA: subscribe, connect copytrading, or DM.
5. Create value, not high-pressure pitch.
6. Telegram HTML, up to 1000 characters.

Return ONLY the post text.
"""


def write_sales_draft(
    config: LLMConfig,
    item: ContentItem,
    research_context: str = "",
) -> ContentItem:
    """Generate a sales-oriented draft."""
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
            "Escribe un post de venta en español."
        )
    elif lang.is_russian:
        user_msg = (
            f"Тип: {item.content_type}\n"
            f"Цель: {item.campaign_goal}\n"
            f"Аудитория: {item.audience_segment}\n"
            f"Тема: {item.topic}\n"
            f"Бриф: {item.brief}\n\n"
            f"Research-контекст:\n{research_context or '(нет)'}\n\n"
            "Напиши продающий пост."
        )
    else:
        user_msg = (
            f"Type: {item.content_type}\n"
            f"Goal: {item.campaign_goal}\n"
            f"Audience: {item.audience_segment}\n"
            f"Topic: {item.topic}\n"
            f"Brief: {item.brief}\n\n"
            f"Research context:\n{research_context or '(none)'}\n\n"
            "Write a sales post."
        )

    body = call_llm(config, system, user_msg)

    item.body = body
    item.status = ContentStatus.DRAFT.value
    item.model_used = config.model
    log.info("Sales copy agent produced draft (%d chars) for item %s", len(body), item.id)
    return item
