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
    """Место занято заранее, чтобы шапка не перестроилась на глазах."""
    assert "theme-slot" in _header(), "исчезло место под переключатель темы"


def test_the_routine_csv_line_is_not_a_warning():
    """Полоса «Added 13 account(s)…» висела всегда и приучала не читать это
    место. Настоящее предупреждение, встав туда же, тоже осталось бы
    непрочитанным."""
    src = (ROOT / "src" / "analysis_agent" / "morning_service.py").read_text(encoding="utf-8")
    body = src[src.index("if new_from_csv:"):]
    body = body[: body.index("assert enriched_df is not None")]
    assert "warnings.append" not in body, "обычная работа снова объявлена предупреждением"


def test_the_logo_is_left_alone():
    """Знак оставлен прежним — решение пользователя.

    Я заменил его геометрией, потому что «ACL» нарисовано как `<text>` шрифтом
    Segoe UI: на машине без него буква подставляется другой, и знак едет.
    Пользователь вернул прежний, и это его выбор — но риск не исчез, поэтому
    записан на доску как `acl-701a`, а не забыт.

    Проверка держит только одно: файл не меняется молча.
    """
    svg = (ROOT / "src" / "web" / "static" / "logo.svg").read_text(encoding="utf-8")
    assert "<text" in svg and "ACL" in svg, "знак подменили — он должен остаться прежним"
