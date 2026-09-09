"""Раздел Ad titles на странице: кнопка, панель и связь между ними.

Проверка написана после случая, который уже стоил ревью: правка удалила
панель, а кнопки и обращения к ней по имени остались — на экране это выглядело
как «раздел просто не открывается». Поэтому здесь сверяются не тексты, а
именно связи: у каждой боковой кнопки есть панель с таким id, а код, который
её наполняет, обращается к существующим полям.
"""
from __future__ import annotations

import re
from pathlib import Path

TPL = Path(__file__).resolve().parent.parent / "src" / "web" / "templates" / "report.html"


def _t() -> str:
    return TPL.read_text(encoding="utf-8")


def test_every_sidebar_button_has_a_panel_to_open():
    """Кнопка без панели молчит: `showView` не находит её и просто выходит."""
    t = _t()
    views = set(re.findall(r'class="sidebar-link[^"]*" data-view="([a-z0-9-]+)"', t))
    assert "ad-texts" in views, "боковой кнопки Ad titles нет"
    for view in views:
        assert f'id="view-{view}"' in t, f"кнопка «{view}» ведёт в никуда: панели нет"


def test_the_section_stands_next_to_targeting():
    """Пользователь так и просил: «там же, где у нас есть раздел таргетинг»."""
    t = _t()
    targeting = t.index('data-view="targets"')
    ad_texts = t.index('data-view="ad-texts"')
    accounts = t.index('data-view="accounts"')
    assert targeting < ad_texts < accounts, "порядок в боковой колонке разошёлся с решением"


def test_the_form_carries_every_column_of_the_sheet():
    """Иначе перенос теряет колонку, и понять это можно только по пропаже."""
    t = _t()
    panel = t[t.index('id="view-ad-texts"'): t.index('id="view-home"')]
    for field in ("at-name", "at-headline", "at-body", "at-description", "at-asset", "at-language"):
        assert f'id="{field}"' in panel, f"в форме нет поля {field}"


def test_the_body_is_a_textarea_not_a_single_line_input():
    """Тело объявления многострочное. В `input` абзацы набрать нечем."""
    t = _t()
    panel = t[t.index('id="view-ad-texts"'): t.index('id="view-home"')]
    assert '<textarea id="at-body"' in panel


def test_the_body_keeps_its_line_breaks_on_screen():
    """Без `pre-wrap` абзацы схлопнутся, и текст на экране разойдётся с текстом
    в Facebook — а именно по нему принимают решение."""
    t = _t()
    style = t[t.index(".at-body {"): t.index(".at-body {") + 400]
    assert "pre-wrap" in style


def test_the_script_reads_the_fields_it_declares():
    """Список полей в JS и поля в форме — одно и то же множество.

    Разойдясь, они дают самую тихую поломку: форма отправляет пустое значение
    вместо набранного, и текст сохраняется без описания.
    """
    t = _t()
    block = t[t.index("var texts = [], editing = ''"):]
    block = block[: block.index("})();")]
    declared = re.search(r"var FIELDS = \[([^\]]+)\]", block)
    assert declared, "список полей в скрипте не найден"
    names = set(re.findall(r"'([a-z]+)'", declared.group(1)))
    assert names == {"name", "headline", "body", "description", "asset", "language"}
    panel = t[t.index('id="view-ad-texts"'): t.index('id="view-home"')]
    for field in names:
        assert f'id="at-{field}"' in panel, f"скрипт читает at-{field}, а поля нет"


def test_the_list_is_searchable_and_grouped():
    """Три сотни текстов не читают сверху вниз."""
    t = _t()
    panel = t[t.index('id="view-ad-texts"'): t.index('id="view-home"')]
    assert 'id="at-search"' in panel, "поиска нет"
    assert 'id="at-lang-filter"' in panel, "отбора по языку нет"


def test_the_panel_does_not_offer_writing_back_to_the_sheet():
    """Вкладка — источник наполнения, дальше библиотека живёт здесь.

    Кнопка «выгрузить обратно» вернула бы работу в таблицу, от которой уходим,
    и разошлась бы с тем, что реестр умеет.
    """
    t = _t()
    panel = t[t.index('id="view-ad-texts"'): t.index('id="view-home"')]
    assert "Replace shared sheet" not in panel
    assert "at-export" not in panel


def test_the_text_is_escaped_before_it_reaches_the_page():
    """Тексты объявлений полны кавычек, стрелок и эмодзи.

    Без экранирования «Registre-se agora>>>» рвёт разметку карточки.
    """
    t = _t()
    block = t[t.index("var texts = [], editing = ''"):]
    block = block[: block.index("})();")]
    for field in ("t.name", "t.headline", "t.body", "t.description"):
        assert f"esc({field})" in block, f"{field} попадает в разметку неэкранированным"


def test_the_section_behaves_when_the_code_is_actually_run():
    """Разметка карточек и отбор собираются строкой во время работы.

    Чтение шаблона глазами этого не покроет: экранирование, группировку и
    отчёт о переносе видно только на исполнении.
    """
    import shutil
    import subprocess

    import pytest

    node = shutil.which("node")
    if not node:
        pytest.skip("нет node — исполнить код страницы нечем")
    root = Path(__file__).resolve().parents[1]
    r = subprocess.run(
        [node, str(root / "tests" / "ad_texts_ui_harness.cjs"), str(TPL)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert r.returncode == 0, r.stderr
