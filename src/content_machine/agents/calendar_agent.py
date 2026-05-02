"""Content Calendar Agent.

Generates a weekly content plan as a list of ContentItem briefs,
balancing content pillars, audience segments, and campaign goals.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from src.content_machine.config import LLMConfig
from src.content_machine.llm import call_llm_json
from src.content_machine.content_language import get_content_language
from src.content_machine.policy.brand_voice import get_brand_voice_dict
from src.content_machine.schema import (
    AudienceSegment,
    CampaignGoal,
    ContentItem,
    ContentStatus,
    ContentType,
)

log = logging.getLogger(__name__)

def _day_system(pillars: str, date_iso: str) -> str:
    lang = get_content_language()
    if lang.is_spanish:
        return f"""\
Eres estratega de contenidos para una comunidad de trading en Telegram (~3.500 miembros).
Productos: señales (suscripción) y copytrading.

Pilares: {pillars}

Tarea: plan de contenido para UN día: {date_iso}.

Reglas:
- 2-3 publicaciones
- Equilibra tipos, pero prioriza lo práctico
- No pongas 2 publicaciones de ventas/copytrading seguidas; con 3 publicaciones, máx 1-2 ofertas
- Franja: mañana (09-10) education/market; tarde (13-15) proof/results; noche (19-21) sales o community
- `topic` y `brief` NO pueden estar vacíos y deben estar en español
- `content_type` ∈ education, signal_proof, result_recap, sales, testimonial, copytrading_offer, market_commentary, community
- `campaign_goal` ∈ trust, activation, conversion, retention, reactivation
- `audience_segment` ∈ free_subscribers, warm_leads, paid_clients, copytrading_prospects

Devuelve SOLO JSON válido sin markdown, sin ```:
{{"date":"{date_iso}","plan":[...]}}
"""
    if lang.is_russian:
        return f"""\
Ты — контент-стратег для трейдинг-сообщества в Telegram (3 500 подписчиков).
Продукты: торговые сигналы (подписка) и копитрейдинг.

Контентные столпы: {pillars}

Твоя задача — составить контент-план на ОДИН день: {date_iso}.

Правила:
- Создай 2-3 поста.
- Старайся сбалансировать типы внутри дня, но важнее практичность, чем идеальная математика.
- Не ставь 2 sales/copytrading подряд, если делаешь 3 поста, то 1-2 “продающих/офферных” в день максимум.
- Слоты: утро (09-10) — education/market; день (13-15) — proof/results; вечер (19-21) — sales или community.
- Каждое поле `topic` и `brief` обязаны быть НЕ пустыми.
- `content_type` — одна из: education, signal_proof, result_recap, sales, testimonial, copytrading_offer, market_commentary, community.
- `campaign_goal` — одна из: trust, activation, conversion, retention, reactivation
- `audience_segment` — одна из: free_subscribers, warm_leads, paid_clients, copytrading_prospects

Верни ТОЛЬКО валидный JSON без markdown, без ```:
{{"date":"{date_iso}","plan":[...]}}
"""
    return f"""\
You are a content strategist for a Telegram trading community (~3,500 members).
Products: signal subscription and copytrading.

Pillars: {pillars}

Task: build a ONE-day plan for {date_iso}.

Rules:
- 2-3 posts
- Balance post types, but keep it practical
- Do not place two sales/copytrading offers back-to-back; with 3 posts, at most 1-2 offer/sales
- Time slots: morning (09-10) education/market; afternoon (13-15) proof/results; evening (19-21) sales or community
- `topic` and `brief` must be non-empty and written in English
- `content_type` is one of: education, signal_proof, result_recap, sales, testimonial, copytrading_offer, market_commentary, community
- `campaign_goal` is one of: trust, activation, conversion, retention, reactivation
- `audience_segment` is one of: free_subscribers, warm_leads, paid_clients, copytrading_prospects

Return ONLY valid JSON, no markdown, no ```:
{{"date":"{date_iso}","plan":[...]}}
"""


def generate_weekly_plan(
    config: LLMConfig,
    start_date: date | None = None,
    context: str = "",
) -> list[ContentItem]:
    """Generate a week of content briefs starting from *start_date*."""
    start = start_date or date.today()
    week_dates = [(start + timedelta(days=i)).isoformat() for i in range(7)]

    bv = get_brand_voice_dict()
    pillars = "; ".join(str(p) for p in bv["content_pillars"])
    items: list[ContentItem] = []

    # Split week into per-day calls: smaller JSON = fewer parse failures in Gemini.
    for day in week_dates:
        system = _day_system(pillars, day)
        lang = get_content_language()
        if lang.is_spanish:
            user_msg = (
                f"Contexto:\n{context or 'ninguno'}\n"
                f"Genera el plan del día {day} siguiendo el system prompt."
            )
        elif lang.is_russian:
            user_msg = (
                f"Контекст:\n{context or 'нет'}\n"
                f"Сгенерируй план на {day} согласно system prompt."
            )
        else:
            user_msg = (
                f"Context:\n{context or 'none'}\n"
                f"Generate the plan for {day} per the system prompt."
            )

        data = call_llm_json(
            config,
            system,
            user_msg,
            max_tokens=max(2048, int(config.max_tokens * 1.2)),
        )

        for entry in data.get("plan", []):
            date_iso = (entry.get("date") or data.get("date") or day).strip()
            item = ContentItem(
                status=ContentStatus.BRIEF.value,
                content_type=entry.get("content_type", ContentType.EDUCATION.value),
                campaign_goal=entry.get("campaign_goal", CampaignGoal.TRUST.value),
                audience_segment=entry.get("audience_segment", AudienceSegment.FREE_SUBSCRIBERS.value),
                topic=entry.get("topic", "") or f"post_{date_iso}",
                brief=entry.get("brief", "") or f"Content brief for {date_iso} ({entry.get('time_slot', '')})",
                calendar_slot=date_iso,
            )
            items.append(item)

    log.info("Calendar agent generated %d briefs for week starting %s", len(items), start)
    return items
