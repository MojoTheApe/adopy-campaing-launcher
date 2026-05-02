"""Research Agent.

Gathers context for a content brief from internal reports, market sources,
and previous successful posts.  Output enriches the brief before drafting.
"""

from __future__ import annotations

import logging
from pathlib import Path

from src.content_machine.config import LLMConfig
from src.content_machine.content_language import get_content_language
from src.content_machine.llm import call_llm
from src.content_machine.schema import ContentItem

log = logging.getLogger(__name__)

def _system_prompt() -> str:
    lang = get_content_language()
    if lang.is_spanish:
        return """\
Eres analista de contenido para un canal de trading en Telegram.
Recibes un brief y contexto de reportes internos.
Tu tarea: preparar un research pack para el copy:
1) Hechos y cifras clave a partir de los datos
2) Ángulo / hook
3) Formato sugerido (texto, texto+imagen, video)
4) Advertencias sobre claims potencialmente riesgosos

Responde en español, de forma breve y estructurada.
"""
    if lang.is_russian:
        return """\
Ты — аналитик контента для трейдинг-Telegram-канала.
Тебе даётся бриф поста и контекст из внутренних отчётов.
Твоя задача — подготовить research-пакет для копирайтера:
1. Ключевые факты и цифры из предоставленных данных.
2. Подходящий угол подачи (hook / angle).
3. Рекомендация по формату (текст, текст+картинка, видео).
4. Предупреждения о потенциально рискованных утверждениях.

Пиши кратко, структурированно, по-русски.
"""
    return """\
You are a content analyst for a trading Telegram channel.
You receive a post brief and context from internal reports.
Prepare a research pack for the copywriter:
1) Key facts/numbers
2) Hook/angle
3) Format recommendation (text, text+image, video)
4) Warnings about potentially risky claims

Be concise and structured, in English.
"""

_MORNING_REPORT_PATH = Path(__file__).resolve().parents[3] / "analysis_agent" / "morning_report.md"


def _load_morning_context() -> str:
    """Try to load the latest morning report as internal market context."""
    if _MORNING_REPORT_PATH.exists():
        text = _MORNING_REPORT_PATH.read_text(encoding="utf-8", errors="ignore")
        # Truncate to stay within context limits
        return text[:6000]
    return ""


def enrich_brief(config: LLMConfig, item: ContentItem) -> str:
    """Return a research packet string that enriches the item's brief."""
    morning = _load_morning_context()
    lang = get_content_language()
    if lang.is_spanish:
        user_msg = (
            f"Brief: {item.brief}\n"
            f"Tipo: {item.content_type}\n"
            f"Objetivo: {item.campaign_goal}\n"
            f"Tema: {item.topic}\n\n"
            f"--- Contexto del reporte matutino ---\n{morning or '(sin datos)'}\n"
        )
    elif lang.is_russian:
        user_msg = (
            f"Бриф: {item.brief}\n"
            f"Тип контента: {item.content_type}\n"
            f"Цель кампании: {item.campaign_goal}\n"
            f"Тема: {item.topic}\n\n"
            f"--- Контекст из утреннего отчёта ---\n{morning or '(нет данных)'}\n"
        )
    else:
        user_msg = (
            f"Brief: {item.brief}\n"
            f"Type: {item.content_type}\n"
            f"Goal: {item.campaign_goal}\n"
            f"Topic: {item.topic}\n\n"
            f"--- Morning report context ---\n{morning or '(no data)'}\n"
        )

    research = call_llm(config, _system_prompt(), user_msg)
    log.info("Research agent enriched brief for item %s", item.id)
    return research
