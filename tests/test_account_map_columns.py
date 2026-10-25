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

#: Source spend remains; newer ACL-2063 replaces the Meta amount with list status.
#: Leads, FTDs, ratios and account status keep their established relative order.
WANT = ["Account", "Spend", "Campaign list", "Leads", "FTDs", "CPL", "CPA", "CR", "Status"]


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


def test_the_drill_no_longer_needs_a_hand_written_width():
    """`ncols` был colspan'ом книжной строки раскрытия: разойдётся с числом
    колонок — и строка поедет. Стеречь его было правильно.

    12.09 книжная детализация ушла со стрелки (решение владельца): одна
    стрелка открывала два списка сразу, и читались они как одна поломанная
    таблица. Осталось живое дерево, а его colspan берётся из самой строки
    (`td.colSpan = row.children.length`) — разойтись ему не с чем.

    Сторож на это стоит в test_the_account_tree_on_screen.py:
    `test_the_tree_lands_under_its_own_account`.
    """
    t = TPL.read_text(encoding="utf-8")
    assert "ncols=" not in t, "вернулась ширина, написанная руками"


def test_workbook_spend_is_kept_and_meta_spend_is_replaced_by_structure_status():
    """Keep the actual source caption without restoring the retired Meta column."""
    t = TPL.read_text(encoding="utf-8")
    head = t[t.index("<thead>"): t.index("</thead>")]
    from src.web.app import templates
    # ACL-103 source captions and newer ACL-2063 structure-only columns both apply.
    for description in ("the Stats workbook", "the BI data", "the selected source data"):
        shown = templates.env.from_string(head).render(amap={"source_info": {"description": description}})
        assert f"Spend from {description} for the chosen period" in shown
        assert "Campaign list" in shown and "Account spend" not in shown
    assert "amap-live" not in t and "amapReadSpend" not in t
