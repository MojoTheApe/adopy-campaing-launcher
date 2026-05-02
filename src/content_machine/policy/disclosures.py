"""Required disclosures for trading/copytrading content.

Every high-risk post must include appropriate disclaimers.
"""

from __future__ import annotations

from src.content_machine.content_language import get_content_language

DISCLOSURES_RU: dict[str, str] = {
    "risk_warning": (
        "⚠️ Торговля на финансовых рынках сопряжена с высоким риском. "
        "Прошлые результаты не гарантируют будущую доходность. "
        "Инвестируйте только те средства, потерю которых можете себе позволить."
    ),
    "past_performance": (
        "📊 Показанные результаты — исторические данные. "
        "Прошлая доходность не является индикатором будущих результатов."
    ),
    "not_financial_advice": (
        "ℹ️ Данный контент носит информационный характер и не является "
        "индивидуальной инвестиционной рекомендацией."
    ),
    "copytrading_risk": (
        "⚠️ Копитрейдинг сопряжён с рисками. Вы можете потерять часть или все "
        "вложенные средства. Результаты зависят от рыночных условий и не "
        "гарантированы. Решение о подключении принимаете вы самостоятельно."
    ),
    "testimonial": (
        "💬 Отзыв реального пользователя. Индивидуальные результаты могут "
        "отличаться и зависят от множества факторов."
    ),
    "ai_generated": (
        "🤖 Контент создан с использованием AI и проверен редактором."
    ),
}

DISCLOSURES_ES: dict[str, str] = {
    "risk_warning": (
        "⚠️ Operar en mercados financieros implica un alto riesgo. "
        "Los resultados pasados no garantizan rentabilidad futura. "
        "Invierta solo el capital que pueda permitirse perder."
    ),
    "past_performance": (
        "📊 Los resultados mostrados son datos históricos. "
        "El rendimiento pasado no es un indicador de resultados futuros."
    ),
    "not_financial_advice": (
        "ℹ️ Este contenido es informativo y no constituye asesoramiento "
        "de inversión personalizado."
    ),
    "copytrading_risk": (
        "⚠️ El copytrading conlleva riesgos. Puedes perder una parte o la "
        "totalidad de tu capital. Los resultados dependen de las condiciones del "
        "mercado y no están garantizados. Tú decides si te conectas."
    ),
    "testimonial": (
        "💬 Testimonio de un usuario real. Los resultados individuales "
        "pueden variar y dependen de múltiples factores."
    ),
    "ai_generated": (
        "🤖 Contenido creado con IA y revisado por un editor."
    ),
}

DISCLOSURES_EN: dict[str, str] = {
    "risk_warning": (
        "⚠️ Trading financial markets involves high risk. "
        "Past results do not guarantee future returns. "
        "Only invest money you can afford to lose."
    ),
    "past_performance": (
        "📊 Shown results are historical. Past performance is not an "
        "indicator of future results."
    ),
    "not_financial_advice": (
        "ℹ️ This content is for information only and is not personalized "
        "investment advice."
    ),
    "copytrading_risk": (
        "⚠️ Copytrading involves risk. You may lose part or all of your capital. "
        "Outcomes depend on market conditions and are not guaranteed. "
        "The decision to connect is yours."
    ),
    "testimonial": (
        "💬 Testimonial from a real user. Individual results can vary and "
        "depend on many factors."
    ),
    "ai_generated": (
        "🤖 Content created with AI and reviewed by an editor."
    ),
}

# Maps content_type to list of disclosure keys that MUST be appended
_REQUIRED_MAP: dict[str, list[str]] = {
    "signal_proof": ["past_performance", "risk_warning"],
    "result_recap": ["past_performance", "risk_warning"],
    "sales": ["not_financial_advice", "risk_warning"],
    "copytrading_offer": ["copytrading_risk", "not_financial_advice"],
    "testimonial": ["testimonial", "past_performance"],
    "short_video": ["ai_generated"],
}


def get_disclosure_map() -> dict[str, str]:
    lang = get_content_language()
    if lang.is_spanish:
        return DISCLOSURES_ES
    if lang.is_russian:
        return DISCLOSURES_RU
    return DISCLOSURES_EN


# Used when appending disclosures to a draft (must match strip_disclosures_block)
DISCLOSURE_SEPARATOR = "\n\n---\n"


def get_required_disclosures(content_type: str) -> list[str]:
    """Return disclosure texts that must be appended to a post of this type."""
    m = get_disclosure_map()
    keys = _REQUIRED_MAP.get(content_type, [])
    return [m[k] for k in keys if k in m]


def strip_disclosures_block(body: str) -> str:
    """Remove appended disclosure block (same separator the orchestrator uses)."""
    if DISCLOSURE_SEPARATOR in body:
        return body.split(DISCLOSURE_SEPARATOR, 1)[0].rstrip()
    return body


# Back-compat / introspection: snapshot for current CM_CONTENT_LANG at import time
DISCLOSURES: dict[str, str] = get_disclosure_map()
