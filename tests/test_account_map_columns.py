"""Колонки карты аккаунтов: состав, порядок и число долей.

Раньше здесь уже была ошибка того же рода: колонок стало одиннадцать, а долей
в colgroup осталось десять — ширины сложились в 100% без последней, и колонка с
кнопками уехала за правый край. Кнопка, которой не видно, всё равно что
отсутствует, поэтому счёт проверяется, а не держится в голове.
"""
from __future__ import annotations

import re
from pathlib import Path

TPL = (
    Path(__file__).resolve().parent.parent
    / "src" / "web" / "templates" / "_account_map_country.html"
)

#: порядок, о котором просил пользователь: спенд из книги, спенд с кабинета,
#: затем лиды, депозиты и производные
WANT = ["Account", "Spend", "On account", "Leads", "FTD", "CPL", "CPA", "CR"]


def _head() -> list[str]:
    t = TPL.read_text(encoding="utf-8")
    head = t[t.index("<thead>"): t.index("</thead>")]
    return [re.sub(r"<[^>]+>", "", h).strip() for h in re.findall(r"<th[^>]*>(.*?)</th>", head, re.S)]


def test_columns_are_in_the_asked_order():
    assert _head()[: len(WANT)] == WANT


def test_ad_asset_and_also_in_are_gone():
    t = TPL.read_text(encoding="utf-8")
    assert "Ad Asset" not in t, "колонка Ad Asset вернулась"
    assert "Also in" not in t, "колонка Also in вернулась"


def test_colgroup_matches_the_number_of_columns():
    t = TPL.read_text(encoding="utf-8")
    cols = t[t.index("<colgroup>"): t.index("</colgroup>")].count("<col ")
    assert cols == len(_head()), (
        f"долей {cols}, колонок {len(_head())} — последняя уедет за край"
    )


def test_widths_add_up_to_a_hundred():
    t = TPL.read_text(encoding="utf-8")
    body = t[t.index("<colgroup>"): t.index("</colgroup>")]
    total = sum(int(n) for n in re.findall(r"width:(\d+)%", body))
    assert total == 100, f"ширины дают {total}%, а не 100"


def test_the_drill_spans_every_column():
    """`ncols` — это colspan строки раскрытия: разойдётся, и она поедет."""
    t = TPL.read_text(encoding="utf-8")
    m = re.search(r"ncols=(\d+)", t)
    assert m, "исчез ncols — строка раскрытия не будет знать своей ширины"
    assert int(m.group(1)) == len(_head())


def test_the_two_spends_say_where_they_came_from():
    """Рядом стоят два спенда — из книги и с кабинета. Порознь их путают."""
    t = TPL.read_text(encoding="utf-8")
    head = t[t.index("<thead>"): t.index("</thead>")]
    assert "workbook" in head, "не сказано, что первый спенд — из книги"
    assert "ad account itself" in head, "не сказано, что второй прочитан с кабинета"
