"""Трекинговая ссылка: предупредить, но не запретить.

Кампания без ссылки собирается и уходит в черновик — Meta её примет, и в Ads
Manager всё выглядит готовым. Не будет только учёта: клики некуда считать, и
обнаружится это не при сборке, а через день, когда за лиды спросят.

Решение пользователя: не запрет, а предупреждение. Бывает, что ссылку ставят
позже руками, и это его выбор — задача в том, чтобы он выбирал, зная.
"""
from __future__ import annotations

import re
from pathlib import Path

TPL = Path(__file__).resolve().parent.parent / "src" / "web" / "templates" / "report.html"


def _js() -> str:
    t = TPL.read_text(encoding="utf-8")
    return t[t.index("window.cbTrackingWarning"): t.index("function _doCbPublish")]


def test_the_warning_does_not_block_the_build():
    """Главное свойство. Сборка остаётся возможной при любом предупреждении."""
    js = _js()
    pub = js[js.index("window.cbPublish"):]
    assert "return;" not in pub.split("_safeConfirm")[0].split("if (warn)")[-1], (
        "предупреждение стало запретом"
    )
    assert "_safeConfirm" in pub, "диалог подтверждения исчез — собрать станет нельзя"


def test_the_three_cases_are_told_apart():
    """Их путать нельзя: делать в каждом надо разное.

    Нет ссылки — вписать. Адрес Jira — заменить на трекер. Набрана и не
    сохранена — текущая сборка использует её, Save сохраняет для следующих.
    """
    js = _js()
    assert "No tracking link" in js
    assert "not saved" in js
    assert "Jira" in js


def test_an_unsaved_link_is_the_most_deceptive_case():
    """Форма определяет текущую сборку; Save нужен для следующих."""
    js = _js()
    block = js[js.index("typed !== saved"): js.index("link_status === 'jira'")]
    assert "This build will use" in block
    assert "+ typed" in block


def test_a_saved_tracking_link_says_nothing():
    """Предупреждение на исправном поле приучает не читать предупреждения."""
    js = _js()
    assert "return null;" in js


def test_the_field_is_highlighted_where_it_can_be_seen():
    """Подсветка внутри свёрнутой панели — это подсветка, которой не видно."""
    js = _js()
    hl = js[js.index("window.cbHighlightField"):]
    assert "cb-preflight-collapsed" in hl, "свёрнутая панель не раскрывается"
    assert "scrollIntoView" in hl, "поле может остаться за краем экрана"
    assert "cb-field-warn" in hl


def test_the_highlight_clears_when_the_person_starts_typing():
    """Метка, которая не уходит, перестаёт что-либо значить."""
    hl = _js()
    hl = hl[hl.index("window.cbHighlightField"):]
    assert "addEventListener('input'" in hl
    assert "removeEventListener" in hl, "обработчик остаётся висеть после первой правки"


def test_the_warning_is_read_before_the_details():
    """Его читают, пока смотрят, что собирается, — а не после нажатия."""
    js = _js()
    pub = js[js.index("window.cbPublish"):]
    assert "bodyHtml = '<div class=\"cb-warn-block\">" in pub, (
        "предупреждение больше не идёт первым блоком"
    )


def test_the_confirm_button_says_what_will_happen():
    """«Publish → draft» на кампании без ссылки — не вся правда."""
    js = _js()
    assert "Build anyway" in js


def test_the_warning_style_is_not_an_error_style():
    """Красным помечается сломанное. Здесь ничего не сломано — здесь выбор."""
    css = TPL.read_text(encoding="utf-8")
    block = css[css.index(".cb-warn-block {"): css.index(".cb-actions {")]
    assert "--warn" in block
    assert "--bad" not in block and "--danger" not in block
