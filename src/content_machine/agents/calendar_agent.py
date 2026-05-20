"""Content Calendar Agent.

Generates a weekly content plan as a list of ContentItem briefs,
balancing content pillars, audience segments, and campaign goals.

Supports optional WeeklyPlanInput (file / env): goals, caps, exclusions.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from src.content_machine.config import LLMConfig
from src.content_machine.content_language import get_content_language
from src.content_machine.llm import call_llm_json
from src.content_machine.plan_validation import (
    validate_plan_entries,
    validate_week_offer_cap,
    log_week_warnings,
)
from src.content_machine.policy.brand_voice import get_brand_voice_dict
from src.content_machine.schema import (
    AudienceSegment,
    CampaignGoal,
    ContentItem,
    ContentStatus,
    ContentType,
)
from src.content_machine.weekly_plan_input import WeeklyPlanInput

log = logging.getLogger(__name__)


def _weekly_rules_append(lang_es: bool, lang_ru: bool) -> str:
    if lang_es:
        return """\
Si hay un briefing operativo más abajo, respétalo incluso cuando choque parcialmente con las reglas habituales
(por ejemplo límites de ofertas o temas prohibidos).

JSON por entrada del plan debe incluir: content_type, campaign_goal, audience_segment, topic, brief.
Opcionalmente time_slot puede ser texto libre para la ventana horaria."""

    if lang_ru:
        return """\
Если ниже есть недельный операторский бриф — следуй ему даже если он частично пересекается с обычными правилами
(лимиты офферов, запреты тем и т.д.).

У каждой записи plan должны быть поля: content_type, campaign_goal, audience_segment, topic, brief.
Поле time_slot опционально (текстовое время слота)."""

    return """\
If an operator weekly briefing appears below, follow it even when it partly overrides default pacing rules
(topic bans, caps on aggressive posts, skipped dates).

Each plan entry must include: content_type, campaign_goal, audience_segment, topic, brief.
time_slot may be included as a free-form time window hint."""


def _day_system(
    pillars: str,
    date_iso: str,
    *,
    lang_es: bool,
    lang_ru: bool,
) -> str:
    extra = _weekly_rules_append(lang_es, lang_ru,)

    if lang_es:
        return f"""\
Eres estratega de contenidos para una comunidad de trading en Telegram (~3.500 miembros).
Productos: señales (suscripción) y copytrading.

Pilares: {pillars}

{extra}

Tarea: plan de contenido para UN día: {date_iso}.

Reglas:
- 2-3 publicaciones por este día concreto
- Equilibra tipos, pero prioriza lo práctico
- No pongas 2 publicaciones de ventas o copytrading seguidas; con 3 publicaciones, máx 1-2 ofertas (sales/copytrading_offer)
- Franja orientativa: mañana (09-10) education/market_commentary; tarde (13-15) proof/results/signal_proof; noche (19-21) sales, copytrading_offer o community
- `topic` y `brief` NO pueden estar vacíos y deben estar en español
- calendar_slot debe ser la fecha `{date_iso}` para cada entrada
- `content_type` ∈ education, signal_proof, result_recap, sales, testimonial, copytrading_offer, market_commentary, community, short_video
- `campaign_goal` ∈ trust, activation, conversion, retention, reactivation
- `audience_segment` ∈ free_subscribers, warm_leads, paid_clients, copytrading_prospects

Devuelve SOLO JSON válido sin markdown, sin ```:
{{"date":"{date_iso}","plan":[...]}}
"""

    if lang_ru:
        return f"""\
Ты — контент-стратег для трейдинг-сообщества в Telegram (3 500 подписчиков).
Продукты: торговые сигналы (подписка) и копитрейдинг.

Контентные столпы: {pillars}

{extra}

Твоя задача — составить контент-план на ОДИН день: {date_iso}.

Правила:
- 2–3 поста именно на эту дату
- Не ставь два sales или два copytrading_offer подряд по порядку плана
- Утро (09–10) — образование/market_commentary; день (13–15) — доказательства/результат; вечер (19–21) — sales/copytrading_offer или community
- topic и brief — непустые, на языке из настроек (по умолчанию см. столпы для аудитории; если просят иначе во внешнем контексте — следуй ему)
- calendar_slot каждого элемента = `{date_iso}`
- content_type, campaign_goal, audience_segment — см. допустимые значения ниже как в английской версии (те же литералы)

Разрешённые литералы:
- content_type: education, signal_proof, result_recap, sales, testimonial, copytrading_offer, market_commentary, community, short_video
- campaign_goal: trust, activation, conversion, retention, reactivation
- audience_segment: free_subscribers, warm_leads, paid_clients, copytrading_prospects

Верни ТОЛЬКО валидный JSON без markdown, без ```:
{{"date":"{date_iso}","plan":[...]}}
"""

    return f"""\
You are a content strategist for a Telegram trading community (~3,500 members).
Products: signal subscription and copytrading.

Pillars: {pillars}

{extra}

Task: build a ONE-day plan for {date_iso}.

Rules:
- 2-3 posts for this specific day
- Balance post types, but keep it practical
- Do not place two sales or two copytrading_offer entries back-to-back in the plan array
- Time slots: morning (09-10) education/market_commentary; afternoon (13-15) proof/results/signal_proof; evening (19-21) sales, copytrading_offer or community
- `topic` and `brief` must be non-empty and written in English
- Set `calendar_slot` to `{date_iso}` for every entry
- `content_type` is one of: education, signal_proof, result_recap, sales, testimonial, copytrading_offer, market_commentary, community, short_video
- `campaign_goal` is one of: trust, activation, conversion, retention, reactivation
- `audience_segment` is one of: free_subscribers, warm_leads, paid_clients, copytrading_prospects

Return ONLY valid JSON, no markdown, no ```:
{{"date":"{date_iso}","plan":[...]}}
"""


def _user_message(
    day: str,
    context: str,
    weekly: WeeklyPlanInput,
    *,
    lang_es: bool,
    lang_ru: bool,
    retry_issues: list[str] | None = None,
) -> str:
    block = weekly.to_prompt_block(lang_es=lang_es, lang_ru=lang_ru)
    lines = [block, ""]

    if lang_es:
        lines.append(f"Contexto libre:\n{context or 'ninguno'}")
        if retry_issues:
            lines.append("")
            lines.append("IMPORTANTE — corrige estos problemas y vuelve a generar:")
            lines.extend(f"- {x}" for x in retry_issues)
        lines.append("")
        lines.append(f"Genera el plan del día {day} siguiendo el system prompt.")
    elif lang_ru:
        lines.append(f"Свободный контекст:\n{context or 'нет'}")
        if retry_issues:
            lines.append("")
            lines.append("ВАЖНО — исправь и перегенерируй:")
            lines.extend(f"- {x}" for x in retry_issues)
        lines.append("")
        lines.append(f"Сгенерируй план на {day} по system prompt.")
    else:
        lines.append(f"Free-form context:\n{context or 'none'}")
        if retry_issues:
            lines.append("")
            lines.append("IMPORTANT — fix and regenerate:")
            lines.extend(f"- {x}" for x in retry_issues)
        lines.append("")
        lines.append(f"Generate the plan for {day} per the system prompt.")

    return "\n".join(lines)


def _entries_from_response(data: dict, day: str) -> list[ContentItem]:
    items: list[ContentItem] = []
    for entry in data.get("plan", []):
        date_iso = str(entry.get("date") or data.get("date") or day).strip()
        topic = entry.get("topic", "") or ""
        brief_raw = entry.get("brief", "")
        ts = entry.get("time_slot", "")
        if ts and isinstance(ts, str) and ts.strip():
            brief = f"{brief_raw}\n[historial de slot: {ts.strip()}]".strip() if brief_raw else f"Ventana horaria sugerida: {ts.strip()}"
        else:
            brief = str(brief_raw or "")
        slot = entry.get("calendar_slot", "") or ""
        calendar_slot = str(slot).strip() or date_iso
        items.append(
            ContentItem(
                status=ContentStatus.BRIEF.value,
                content_type=entry.get("content_type", ContentType.EDUCATION.value),
                campaign_goal=entry.get("campaign_goal", CampaignGoal.TRUST.value),
                audience_segment=entry.get("audience_segment", AudienceSegment.FREE_SUBSCRIBERS.value),
                topic=str(topic).strip() or f"post_{date_iso}",
                brief=brief.strip() or f"Content brief for {date_iso}",
                calendar_slot=calendar_slot,
            ),
        )
    return items


def _generate_one_day(
    config: LLMConfig,
    *,
    pillars: str,
    day: str,
    context: str,
    weekly: WeeklyPlanInput,
    lang_es: bool,
    lang_ru: bool,
) -> list[ContentItem]:
    """Call LLM once; retry once when validation finds blocking issues."""

    max_tokens = max(2048, int(config.max_tokens * 1.2))

    retry_issues: list[str] | None = None

    for attempt in range(2):
        system = _day_system(pillars, day, lang_es=lang_es, lang_ru=lang_ru)
        user_msg = _user_message(day, context, weekly, lang_es=lang_es, lang_ru=lang_ru, retry_issues=retry_issues)
        try:
            data = call_llm_json(
                config,
                system,
                user_msg,
                max_tokens=max_tokens,
            )
        except Exception:
            log.exception("Calendar LLM call failed for %s attempt %s", day, attempt)
            return []

        items = _entries_from_response(data, day)
        issues = validate_plan_entries(
            day,
            items,
            max_offer_posts_per_day=weekly.max_offer_posts_per_day,
        )

        if not issues:
            if attempt:
                log.info("Calendar day %s passed validation after retry", day)
            return items

        log.warning(
            "Calendar day %s validation issues%s: %s",
            day,
            " (retrying once)" if attempt == 0 else " (giving up)",
            "; ".join(issues),
        )
        if attempt == 0:
            retry_issues = issues
            continue
        return items

    return []


def generate_weekly_plan(
    config: LLMConfig,
    start_date: date | None = None,
    context: str = "",
    weekly: WeeklyPlanInput | None = None,
) -> list[ContentItem]:
    """Generate a week of content briefs starting from *start_date*."""
    wk = weekly or WeeklyPlanInput.empty()

    start = start_date or date.today()
    week_dates = [(start + timedelta(days=i)).isoformat() for i in range(7)]
    exclude = {d.strip() for d in wk.exclude_dates}

    bv = get_brand_voice_dict()
    pillars = "; ".join(str(p) for p in bv["content_pillars"])

    lang = get_content_language()
    lang_es = lang.is_spanish
    lang_ru = lang.is_russian

    items: list[ContentItem] = []

    for day in week_dates:
        if day in exclude:
            log.info("Skipping calendar generation for excluded date %s", day)
            continue
        day_items = _generate_one_day(
            config,
            pillars=pillars,
            day=day,
            context=context,
            weekly=wk,
            lang_es=lang_es,
            lang_ru=lang_ru,
        )
        items.extend(day_items)

    week_issues = validate_week_offer_cap(items, wk.max_offer_posts_per_week)
    log_week_warnings(week_issues)

    log.info("Calendar agent generated %d briefs for week starting %s", len(items), start)
    return items
