"""ACL-2180: язык Instant Form «Auto» - из языка и страны набора.

Locale формы задаёт лишь язык встроенных подписей Meta (кнопки, стандартные поля, текст
про приватность), не тексты владельца. Шаблон с `locale = AUTO` получает конкретный код при
построении плана (`creator.plan_from_draft`), ДО любой записи; пара язык/страна без кода
из `form_templates.LOCALES` - остановка с понятным текстом (молчаливого английского нет).

Несколько языков набора (ACL-1767): форму задаёт ПЕРВИЧНЫЙ язык кампании (`plan.language`,
поле Language набора); дополнительные языки (`languages`) на форму не влияют.

Зеркало таблицы для билдера - `FORM_LOCALE_RULES` в report.html (сверяет тест).
"""
from __future__ import annotations

import re

from src.fb_api import form_templates as ft

AUTO_LOCALE = "AUTO"

#: Читаемые названия для выпадающих списков (Forms tab и панель билдера).
LOCALE_NAMES: dict[str, str] = {
    "AR_AR": "Arabic", "CS_CZ": "Czech", "DA_DK": "Danish", "DE_DE": "German", "EL_GR": "Greek",
    "EN_GB": "English (UK)", "EN_US": "English (US)", "ES_ES": "Spanish (Spain)",
    "ES_LA": "Spanish (Latin America)", "FI_FI": "Finnish", "FR_FR": "French", "HE_IL": "Hebrew",
    "HI_IN": "Hindi", "HU_HU": "Hungarian", "ID_ID": "Indonesian", "IT_IT": "Italian",
    "JA_JP": "Japanese", "KO_KR": "Korean", "NB_NO": "Norwegian (Bokmal)", "NL_NL": "Dutch",
    "PL_PL": "Polish", "PT_BR": "Portuguese (Brazil)", "PT_PT": "Portuguese (Portugal)",
    "RO_RO": "Romanian", "RU_RU": "Russian", "SV_SE": "Swedish", "TH_TH": "Thai",
    "TR_TR": "Turkish", "VI_VN": "Vietnamese", "ZH_CN": "Chinese (Simplified)",
    "ZH_HK": "Chinese (Hong Kong)", "ZH_TW": "Chinese (Taiwan)",
}
AUTO_LABEL = "Auto (from the ad set language and country)"

LATAM = frozenset("AR BO CL CO CR CU DO EC SV GT HN MX NI PA PY PE UY VE".split())

#: язык -> {"*": код для любой страны} или {страна: код, ...} (без "*" другие страны - отказ).
RULES: dict[str, dict[str, str]] = {
    "english": {"GB": "EN_GB", "*": "EN_US"},
    "spanish": {"ES": "ES_ES", **{c: "ES_LA" for c in sorted(LATAM)}},
    "portuguese": {"BR": "PT_BR", "*": "PT_PT"},
    "chinese": {"CN": "ZH_CN", "HK": "ZH_HK", "TW": "ZH_TW"},
    "arabic": {"*": "AR_AR"}, "french": {"*": "FR_FR"}, "german": {"*": "DE_DE"},
    "italian": {"*": "IT_IT"}, "turkish": {"*": "TR_TR"}, "russian": {"*": "RU_RU"},
    "czech": {"*": "CS_CZ"}, "danish": {"*": "DA_DK"}, "greek": {"*": "EL_GR"},
    "finnish": {"*": "FI_FI"}, "hebrew": {"*": "HE_IL"}, "hindi": {"*": "HI_IN"},
    "hungarian": {"*": "HU_HU"}, "indonesian": {"*": "ID_ID"}, "japanese": {"*": "JA_JP"},
    "korean": {"*": "KO_KR"}, "norwegian": {"*": "NB_NO"}, "dutch": {"*": "NL_NL"},
    "polish": {"*": "PL_PL"}, "romanian": {"*": "RO_RO"}, "swedish": {"*": "SV_SE"},
    "thai": {"*": "TH_TH"}, "vietnamese": {"*": "VI_VN"},
}


class LocaleRefused(ValueError):
    """Пара язык/страна не даёт кода Instant Form; текст - для оператора."""


def _language_key(language: str) -> str:
    """«Spanish (All)» / «Portuguese (Brazil)» -> «spanish»."""
    return re.sub(r"\s*\(.*?\)\s*", " ", str(language or "")).strip().casefold()


def country_codes(country: str) -> list[str]:
    """«Colombia», «CO», «Colombia, Mexico» -> ['CO', 'MX'] (неизвестное возвращается как есть)."""
    from src.analysis_agent.country_names import COUNTRY_CODES
    out: list[str] = []
    for part in re.split(r"[,;|]", str(country or "")):
        part = part.strip()
        if not part:
            continue
        code = COUNTRY_CODES.get(part.casefold()) or (part.upper() if len(part) == 2 and part.isalpha() else "")
        out.append(code or part)
    return out


def resolve_locale(language: str, country: str) -> str:
    """Код Instant Form для языка и страны набора; `LocaleRefused` при неоднозначности."""
    shown = f"language {str(language).strip() or '(empty)'!r}, country {str(country).strip() or '(empty)'!r}"
    rule = RULES.get(_language_key(language))
    if rule is None:
        raise LocaleRefused(
            f"Form locale Auto: no Instant Form language for {shown}. Pick a Language in the ad set "
            "that Instant Forms support, or choose the form locale by hand in the template")
    codes = country_codes(country)
    if "*" in rule and (not codes or len({rule.get(c, rule["*"]) for c in codes}) == 1):
        return rule.get(codes[0], rule["*"]) if codes else rule["*"]
    if not codes:
        raise LocaleRefused(f"Form locale Auto: the country is needed to pick the form language for {shown}")
    found = {rule.get(c, rule.get("*")) for c in codes}
    if None in found:
        raise LocaleRefused(
            f"Form locale Auto: no Instant Form language for {shown} (this language has a form "
            "locale only for some countries). Choose the form locale by hand in the template")
    if len(found) > 1:
        raise LocaleRefused(
            f"Form locale Auto: {shown} would need different form languages ({', '.join(sorted(found))}). "
            "Split the countries into separate ad sets or choose the form locale by hand")
    return found.pop()


def describe(locale: str, language: str, country: str) -> str:
    """Строка для Check plan: «Form locale: ES_LA (Spanish, Colombia)»."""
    lang = re.sub(r"\s*\(.*?\)\s*", " ", str(language)).strip()
    return f"Form locale: {locale} ({lang}, {str(country).strip()})"


def resolve_template(tpl: dict, language: str, country: str) -> tuple[dict, str]:
    """Шаблон с конкретным кодом. Ручной locale возвращается как есть (тот же хэш и имя).

    У Auto-шаблона `id` получает суффикс `@<код>`: один шаблон в наборах с разными языками
    даёт две формы на Странице (хэш содержимого включает locale), а шаги публикации не
    сливаются по общему id. Эти `id`/`name` в хэш не входят.
    """
    if str(tpl.get("locale") or "").upper() != AUTO_LOCALE:
        return tpl, ""
    loc = resolve_locale(language, country)
    assert loc in ft.LOCALES
    return {**tpl, "locale": loc, "id": f"{tpl.get('id')}@{loc}"}, describe(loc, language, country)
