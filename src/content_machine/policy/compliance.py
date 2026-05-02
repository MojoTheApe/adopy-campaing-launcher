"""Compliance rules for trading content.

Classifies content by risk tier and detects prohibited/review-required language.
"""

from __future__ import annotations

import re
from enum import Enum

from src.content_machine.content_language import get_content_language


class RiskTier(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    BLOCKED = "blocked"


# ── Phrase lists (raw regex strings) — merged by output language in classify_risk

_BANNED_RU: list[str] = [
    r"гарантирован\w*\s+(доход|прибыль|результат|заработок)",
    r"100\s*%\s*(гарантия|результат|сигнал|прибыль|точность)",
    r"без\s+риск\w*",
    r"безопасн\w+\s+(вложени|инвестиц)",
    r"пассивн\w+\s+доход\s+без\s+усилий",
    r"обязательно\s+заработ\w+",
    r"деньги\s+из\s+воздуха",
    r"секретн\w+\s+стратеги\w*",
]

_BANNED_EN: list[str] = [
    r"risk[\s-]*free",
    r"guaranteed\s+(profit|return|income|result)",
    r"100\s*%\s*(guaranteed|profit|return|signal|win\s*rate|accuracy)",
    r"no\s+risk",
    r"passive\s+income\s+with\s+no\s+effort",
]

_BANNED_ES: list[str] = [
    r"sin\s+riesgo",
    r"ganancia\s+asegurada|ingreso\s+asegurado|rentabilidad\s+asegurada",
    r"100\s*%\s*(garant[íi]a|ganancia|rentabilidad|precisi[óo]n|acierto)",
    r"inversi[óo]n\s+segura",
    r"ganar[áa]n?\s+seguro",
    r"ingreso\s+pasivo\s+sin\s+esfuerzo",
    r"dinero\s+f[áa]cil",
    r"estrategia\s+secreta",
]

_REVIEW_RU: list[str] = [
    r"(\+|плюс)\s*\d+\s*%",
    r"\d+\s*(пунктов|пипсов|pips|points)",
    r"(заработал|получил|вывел|профит)\s*\d",
    r"(результат|доходность|ROI)\s*[:=]?\s*\d",
    r"(скопируй|подключись|подписка|копитрейдинг)",
    r"(ограничен\w+\s+(мест|предложени|врем))",
    r"(только\s+сегодня|последн\w+\s+шанс|осталось\s+\d)",
    r"(отзыв|рекомендаци\w+|рекомендую)",
]

_REVIEW_EN: list[str] = [
    r"\$\s*\d{3,}",
    r"(testimonial|review|recommended|subscribe|copy\s*trading|signals)",
    r"(last\s+chance|only\s+today|limited\s+spots?)",
    r"\+\s*\d+\s*%",
]

_REVIEW_ES: list[str] = [
    r"(copia\s+trading|copytrading|se[ñn]ales)",
    r"(garant[íi]a|últim\w+\s+oportunidad|solo\s+hoy|plazas?\s+limitad)",
    r"(recomendaci[óo]n|testimonio|recomiendo)",
]


def _merge_patterns() -> tuple[list[re.Pattern[str]], list[re.Pattern[str]]]:
    lang = get_content_language()
    banned_raw = _BANNED_RU + _BANNED_EN
    review_raw = _REVIEW_RU + _REVIEW_EN
    if lang.is_russian:
        pass
    elif lang.is_spanish:
        banned_raw = _BANNED_ES + _BANNED_EN
        review_raw = _REVIEW_ES + _REVIEW_EN
    else:
        banned_raw = _BANNED_EN + _BANNED_RU
        review_raw = _REVIEW_EN + _REVIEW_RU

    banned = [re.compile(p, re.IGNORECASE) for p in banned_raw]
    review = [re.compile(p, re.IGNORECASE) for p in review_raw]
    return banned, review

# ── Content type → default risk tier ─────────────────────────────────────────

_CONTENT_TYPE_RISK: dict[str, RiskTier] = {
    "education": RiskTier.LOW,
    "community": RiskTier.LOW,
    "market_commentary": RiskTier.MEDIUM,
    "signal_proof": RiskTier.HIGH,
    "result_recap": RiskTier.HIGH,
    "testimonial": RiskTier.HIGH,
    "sales": RiskTier.HIGH,
    "copytrading_offer": RiskTier.HIGH,
    "short_video": RiskTier.MEDIUM,
}


def classify_risk(content_type: str, text: str) -> tuple[RiskTier, list[str]]:
    """Return (risk_tier, list_of_flags) for a draft text.

    Risk is escalated if banned/review phrases are found, regardless of content type.
    """
    flags: list[str] = []
    banned_phrases, review_phrases = _merge_patterns()

    for pattern in banned_phrases:
        if pattern.search(text):
            flags.append(f"BLOCKED: '{pattern.pattern}'")
            return RiskTier.BLOCKED, flags

    base_tier = _CONTENT_TYPE_RISK.get(content_type, RiskTier.MEDIUM)

    for pattern in review_phrases:
        match = pattern.search(text)
        if match:
            flags.append(f"REVIEW: '{match.group()}' matches '{pattern.pattern}'")

    if flags and base_tier == RiskTier.LOW:
        base_tier = RiskTier.MEDIUM
    if len(flags) >= 3 and base_tier == RiskTier.MEDIUM:
        base_tier = RiskTier.HIGH

    return base_tier, flags


# Back-compat: pattern lists for current CM_CONTENT_LANG at import time
BANNED_PHRASES, REVIEW_REQUIRED_PHRASES = _merge_patterns()
