"""Страница «Automatic»: одно правило для API и RPA (ACL-1880, решение владельца 2026-10-13).

Порядок `promote_pages` у Meta ничего не значит, поэтому «первая страница списка» —
это угадывание, и кампания могла тихо уйти на чужую Страницу. Пустой выбор разрешается
только так:

1. страница из карты аккаунтов есть среди страниц кабинета — она (источник `map`);
2. иначе страница в кабинете ровно одна — она (источник `account default`);
3. иначе страниц несколько — отказ до любой записи, выбрать должен человек;
4. страниц нет — решает вызывающий (оболочка для сайта / отказ для Instant Forms).

Чистая функция без сети: те же ответы считает `cbPreflight` в report.html (правило
`page_choice`), а тест паритета гоняет одни фикстуры через обе реализации.
"""
from __future__ import annotations

SOURCE_MAP = "map"
SOURCE_ACCOUNT_DEFAULT = "account default"


class PageChoiceError(ValueError):
    """Страниц несколько и человек не выбрал ни одну."""


def several_pages_message(count: int) -> str:
    return (f"This ad account has {count} Facebook Pages. "
            "Choose the Page for this campaign in the builder.")


def automatic_page(page_ids: list[str], map_page_id: str = "") -> tuple[str, str]:
    """(id, источник) для пустого выбора; ("", "") — страниц нет. Несколько — `PageChoiceError`."""
    ids = [str(p).strip() for p in page_ids if str(p).strip()]
    mapped = str(map_page_id or "").strip()
    if mapped and mapped in ids:
        return mapped, SOURCE_MAP
    if len(ids) == 1:
        return ids[0], SOURCE_ACCOUNT_DEFAULT
    if len(ids) > 1:
        raise PageChoiceError(several_pages_message(len(ids)))
    return "", ""
