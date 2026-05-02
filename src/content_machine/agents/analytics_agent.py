"""Analytics Agent.

Summarizes post performance and generates recommendations for future
content calendar briefs.  Does NOT auto-publish or change live content.
"""

from __future__ import annotations

import logging
from typing import Sequence

from src.content_machine.config import LLMConfig
from src.content_machine.content_language import get_content_language
from src.content_machine.llm import call_llm
from src.content_machine.schema import ContentItem

log = logging.getLogger(__name__)

def _system_prompt() -> str:
    lang = get_content_language()
    if lang.is_spanish:
        return """\
Eres analista de content marketing de un canal de trading en Telegram.
Recibes métricas de publicaciones de un periodo.

Tarea:
1) Top 3 por engagement (views, reacciones, comentarios, reenvíos)
2) Qué content_type y campaign_goal funcionaron mejor
3) Qué franjas horarias dieron más alcance
4) 3-5 recomendaciones concretas para la próxima semana

Responde en español, breve y estructurado.
"""
    if lang.is_russian:
        return """\
Ты — аналитик контент-маркетинга для трейдинг-Telegram-канала.
Тебе даются метрики опубликованных постов за период.

Задача:
1. Выдели топ-3 поста по вовлечённости (views, reactions, comments, forwards).
2. Определи, какие content_type и campaign_goal работали лучше.
3. Какие временные слоты дали лучший охват.
4. Предложи 3-5 конкретных рекомендаций для следующей недели.

Пиши кратко, структурированно, по-русски.
"""
    return """\
You are a content marketing analyst for a trading Telegram channel.
You receive published post metrics for a time window.

Task:
1) Top 3 by engagement
2) Which content_type and campaign_goal worked best
3) Which time slots had best reach
4) 3-5 concrete recommendations for next week

Be concise, structured, in English.
"""


def _format_metrics(items: Sequence[ContentItem]) -> str:
    lines: list[str] = []
    for it in items:
        m = it.metrics
        lines.append(
            f"- [{it.content_type}] {it.topic[:60]} | "
            f"views={m.views} react={m.reactions} comments={m.comments} "
            f"fwd={m.forwards} clicks={m.link_clicks}"
        )
    lang = get_content_language()
    empty = "(sin datos)" if lang.is_spanish else ("(нет данных)" if lang.is_russian else "(no data)")
    return "\n".join(lines) or empty


def generate_summary(
    config: LLMConfig,
    published_items: Sequence[ContentItem],
) -> str:
    """Return a text summary with recommendations for the next calendar cycle."""
    lang = get_content_language()
    if lang.is_spanish:
        user_msg = (
            f"Publicaciones ({len(published_items)}):\n"
            f"{_format_metrics(published_items)}\n\n"
            "Haz el análisis y recomendaciones."
        )
    elif lang.is_russian:
        user_msg = (
            f"Опубликованные посты ({len(published_items)}):\n"
            f"{_format_metrics(published_items)}\n\n"
            "Сделай анализ и дай рекомендации."
        )
    else:
        user_msg = (
            f"Published posts ({len(published_items)}):\n"
            f"{_format_metrics(published_items)}\n\n"
            "Analyze and give recommendations."
        )
    summary = call_llm(config, _system_prompt(), user_msg)
    log.info("Analytics agent produced summary (%d chars)", len(summary))
    return summary
