"""Manage: четыре бывших экрана на одной странице.

My Goals, CM, Accounts и Campaign Builder отвечали на один вопрос — как идёт
страна и что с этим делать — и стоили четырёх кликов по экранам, которые
никогда не видны рядом.
"""
from __future__ import annotations

import re
from pathlib import Path

TPL = Path(__file__).resolve().parent.parent / "src" / "web" / "templates" / "report.html"


def _t() -> str:
    return TPL.read_text(encoding="utf-8")


def test_the_four_panels_became_one():
    t = _t()
    assert 'id="view-country-{{ cid }}-manage"' in t, "панели Manage нет"
    for gone in ("-goals", "-cm", "-accounts", "-open"):
        assert f'<div id="view-country-{{{{ cid }}}}{gone}" class="view-panel"' not in t, (
            f"панель {gone} снова отдельная"
        )


def test_the_order_is_the_decision():
    """Сверху вниз так, как идёт работа: цель, сравнение, аккаунты, билдер."""
    t = _t()
    # срез до конца самой панели, а не до первого {% endfor %}: внутри неё
    # хватает своих циклов, и срез по первому обрывался раньше билдера
    start = t.index('id="view-country-{{ cid }}-manage"')
    panel = t[start: t.index("view-country-{{ cid }}-assets", start)]
    order = [panel.index(m) for m in ("My Goals", "CM Comparison", "Account Statuses", "Campaign Builder")]
    assert order == sorted(order), "порядок на странице разошёлся с решением"


def test_the_old_addresses_still_answer():
    """`#country-kw-goals` и прочие лежат в закладках и в логике обновления.

    Без этого обновление посреди сборки кампании выбросило бы на пустой вид.
    """
    t = _t()
    assert "MERGED_INTO_MANAGE" in t, "псевдонимы старых адресов исчезли"
    for old in ("goals", "cm", "accounts", "open"):
        assert f"'{old}'" in t[t.index("MERGED_INTO_MANAGE"): t.index("MERGED_INTO_MANAGE") + 200]


def test_the_builder_is_collapsed_by_default():
    """Билдер — страница: со слотами, аккордеоном и публикацией.

    Раскрытым он не дал бы увидеть дна страницы.
    """
    t = _t()
    m = re.search(r'<details class="manage-builder"[^>]*>', t)
    assert m, "билдер перестал быть сворачиваемым"
    assert " open" not in m.group(0), "билдер раскрыт разметкой"


def test_the_numbering_has_no_hole():
    """Слитые разделы не должны тратить номера.

    Иначе список читается «2, 3, 4, 5, 6, 7, 9» — дыра, за которой человек
    будет искать пропавший пункт.
    """
    t = _t()
    for name in ("n_cm", "n_accounts", "n_builder"):
        assert f"{{% set sec.n = sec.n + 1 %}}{{% set {name} = sec.n %}}" not in t, (
            f"{name} снова занимает номер в списке"
        )


def test_both_counters_agree():
    """Счётчиков два — у списка и у панелей. Разойдясь, они дали бы в списке
    одни номера, а в заголовках другие."""
    t = _t()
    blocks = re.findall(r"\{% set sec = namespace\(n=0\) %\}(.*?)(?=<div|\{#)", t, re.S)
    assert len(blocks) == 2, f"счётчиков стало {len(blocks)}, ожидалось два"
    names = [sorted(re.findall(r"set (n_\w+) = sec\.n", b)) for b in blocks]
    assert names[0] == names[1], f"счётчики расходятся: {names[0]} против {names[1]}"
