"""Дерево в ящике: страна → аккаунт → кампания → объявление.

Подписями, а не лесенкой. Два уровня отступов в панели шириной в 360
пикселей не оставляют имени кампании места, где жить, — а имя тут длинное,
и в нём вся суть: страна, стратегия, возраст, устройство.

Страны и аккаунты дерево берёт с самой страницы, а не отдельным списком с
сервера. Список, живущий рядом с настоящим, рано или поздно начинает ему
врать: в этом файле уже было двадцать копий одной панели входа и две формы
подключения, и обе разошлись с тем, что показывала карта аккаунтов.

Индикатор готовности пока заглушка — и нарочно не зелёная. Зелёный,
поставленный до проверки, — обещание, которое ещё не сдержано; настоящие
проверки приедут вместе с формой.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TPL = ROOT / "src" / "web" / "templates" / "report.html"


def _tree_block() -> str:
    src = TPL.read_text(encoding="utf-8")
    block = src[src.index("window.cbTree = (function () {"):]
    return block[: block.index("\n})();") + 6]


def test_the_page_code_behaves():
    """Готовность, выбор места, раскрытие, удаление и потолок — на живом коде."""
    node = shutil.which("node")
    if not node:
        pytest.skip("нет node — исполнить код страницы нечем")
    r = subprocess.run(
        [node, str(ROOT / "tests" / "builder_tree_harness.cjs"), str(TPL)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert r.returncode == 0, r.stderr


def test_accounts_are_read_from_the_page_not_from_a_second_list():
    """Второй список аккаунтов — это второй источник правды, а он расходится.

    Проверяется отсутствие нового серверного блока: дерево обязано ходить в
    ту же разметку, из которой растёт билдер.
    """
    block = _tree_block()
    assert ".cb-acc-row[data-acc-label]" in block, "дерево перестало спрашивать страницу"
    for forbidden in ("tojson", "fetch(", "/account-map", "hx-get"):
        assert forbidden not in block, f"дерево завело свой источник: {forbidden}"


def test_the_readiness_is_one_place():
    """Когда приедут настоящие проверки, менять надо будет одну функцию, а не
    девять мест, где решается цвет кружка."""
    block = _tree_block()
    assert block.count("function readiness(") == 1
    for literal in ("cbd-ind-ok", "cbd-ind-bad", "cbd-ind-warn", "cbd-ind-wait"):
        assert literal not in block, (
            f"состояние {literal} проставлено разметкой мимо readiness — "
            "теперь их два, и они разойдутся"
        )


def test_the_indicator_is_never_colour_alone():
    """Красный и зелёный — самая частая пара, которую не различают. Поэтому
    к цвету добавлены форма и значок, и ни одно состояние не обходится
    только классом цвета."""
    css = TPL.read_text(encoding="utf-8")
    css = css[css.index(".cbd-ind {"):css.index(".cbd-add {")]
    assert "border-radius: 50%" in css and "rotate(45deg)" in css and "border-style: dashed" in css, (
        "состояния различаются только цветом"
    )
    block = _tree_block()
    assert re.search(r"sign: '[^']+'", block), "у состояния нет значка"


def test_a_removed_campaign_is_asked_about_first():
    """Собранная наполовину кампания — работа; вопрос должен называть, какую
    именно убирают, иначе соглашаешься вслепую."""
    block = _tree_block()
    assert "window.confirm(" in block
    assert "camp.name" in block[block.index("act === 'drop'"):][:400], (
        "в вопросе не названо, что удаляют"
    )


def test_the_whole_form_travels_and_never_a_single_card():
    """В ящик переносится форма аккаунта целиком.

    Браузер собирает данные формы только из того, что лежит **внутри** её
    тега: `new FormData(form)` не видит ничего снаружи. Карточка, вынутая в
    ящик по отдельности, ушла бы в публикацию без единого своего поля —
    молча, без ошибки. В коде, поднимающем черновики в кабинете, такой отказ
    недопустим, поэтому переносится форма, а лишние карточки в ней просто
    скрыты.
    """
    block = _tree_block()
    assert "host.appendChild(form)" in block, "в ящик переносится не форма"
    assert "host.appendChild(slot" not in block and "appendChild(found.camp.slot" not in block, (
        "карточку вынимают из формы — её поля не уйдут в публикацию"
    )
    only = block[block.index("function showOnly("):]
    only = only[: only.index("\n  }") + 4]
    assert "slot.hidden" in only and ".remove()" not in only, (
        "лишние карточки не скрыты, а выброшены — это кампании того же аккаунта"
    )


def test_the_drawer_follows_the_page_without_being_told():
    """Кампанию заводят и на странице страны. Ящик должен показать это сам —
    иначе половина работы ему не видна, и добавлять приходится только через
    него."""
    block = _tree_block()
    assert "MutationObserver" in block, "за формами никто не следит"
    assert "form.open-account-form" in block


def test_the_tree_shows_what_is_being_assembled_not_every_account():
    """Аккаунтов два десятка. Показанные все, они делают стену пустых строк,
    в которой не видно двух кампаний, ради которых ящик и открыли."""
    block = _tree_block()
    assert "a.campaigns.length" in block, "пустые аккаунты не отсеиваются"
