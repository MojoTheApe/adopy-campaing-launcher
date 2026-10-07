"""Шапка дешборда: что в ней стоит и чего в ней больше нет.

Раздел «Дизайн», задача «Поправляем Header». Проверки держат решения, которые
легко потерять при следующей правке шаблона.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TPL = ROOT / "src" / "web" / "templates" / "report.html"


def _header() -> str:
    t = TPL.read_text(encoding="utf-8")
    return t[t.index('<header class="page-header">'): t.index("</header>")]


def test_the_two_values_are_labelled():
    """«Updated … · acl-release-…» читалось как одно смазанное сообщение.

    Обе величины нужны в один момент — когда обновление сломало работу и надо
    понять, что стоит и когда встало, чтобы откатиться.
    """
    h = _header()
    assert "Last updated:" in h, "исчезла подпись у времени обновления"
    assert "Version:" in h, "исчезла подпись у версии"


def test_the_manager_is_not_part_of_the_product_name():
    """Под логотипом стояло «CM Evgeni Sedin» — положение зрителя, а не свойство
    продукта. Осталось отдельным элементом сбоку."""
    h = _header()
    brand = h[h.index('class="brand-text"'): h.index("</div>", h.index('class="brand-meta"'))]
    assert "my_cm" not in brand, "имя менеджера вернулось под логотип"
    assert "Logged as" in h, "исчезла подпись у выбора менеджера"
    assert ">Buyer<" not in h, "вернулась подпись Buyer"


def test_the_counters_are_gone():
    """`Trend 7d · 58 combos` — числа, за которыми никто не приходит в шапку."""
    h = _header()
    assert "trend_days" not in h, "счётчик тренда вернулся в шапку"
    assert "total_combos" not in h, "счётчик комбинаций вернулся в шапку"


def test_room_is_kept_for_the_theme_switch():
    """Место было занято заранее, и переключатель встал в него, ничего не сдвинув."""
    assert "theme-switch" in _header(), "исчез переключатель темы из шапки"


def test_the_routine_csv_line_is_not_a_warning():
    """Полоса «Added 13 account(s)…» висела всегда и приучала не читать это
    место. Настоящее предупреждение, встав туда же, тоже осталось бы
    непрочитанным."""
    src = (ROOT / "src" / "analysis_agent" / "morning_service.py").read_text(encoding="utf-8")
    body = src[src.index("if new_from_csv:"):]
    body = body[: body.index("assert enriched_df is not None")]
    assert "warnings.append" not in body, "обычная работа снова объявлена предупреждением"


def test_the_logo_is_the_blue_adopy_mark():
    """ACL-1705: знак — плоская синяя метка Adopy «A» (#2563eb), без плитки и текста.

    Прежний знак с `<text>`-буквами «ACL» зависел от шрифта Segoe UI; новый —
    чистая геометрия, поэтому проверяем, что текста в нём нет.
    """
    svg = (ROOT / "src" / "web" / "static" / "logo.svg").read_text(encoding="utf-8")
    assert "#2563eb" in svg and "<text" not in svg and "<rect" not in svg
    assert 'aria-label="Adopy Campaign Manager"' in svg


def test_the_header_caption_is_the_product_name():
    """ACL-1705: подпись под знаком — «Adopy Campaign Manager», слова Launcher в шапке нет."""
    head = _header()
    assert re.search(r'<h1 class="brand-name">Adopy Campaign Manager</h1>', head)
    assert "Launcher" not in head
