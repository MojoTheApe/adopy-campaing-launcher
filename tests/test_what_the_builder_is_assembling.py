"""Набранное переживает перезагрузку — и не копится вечно.

Форму билдера рисует сервер, и рисует пустой: карточек кампаний в разметке
больше нет, автозаполнение из рекомендаций убрано. Значит, после F5 в
билдере чисто — а собранная наполовину кампания есть работа: имя, бюджет,
таргетинг, тексты, выбранные креативы. Перезагрузка здесь случается часто,
дешборд сам просит обновиться, и терять на ней час чужого вечера нельзя.

Поэтому значения полей складываются в браузер при вводе и раскладываются
обратно при загрузке. Обратная сторона — хранилище само ничего не
забывает: без срока годности через месяц там лежат позапрошлые черновики.

Поведение проверяется исполнением кода страницы (`builder_keep_harness.cjs`)
с часами в руках: срок годности иначе проверить нечем.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TPL = ROOT / "src" / "web" / "templates" / "report.html"


def _block() -> str:
    src = TPL.read_text(encoding="utf-8")
    block = src[src.index("window.cbKeep = (function () {"):]
    return block[: block.index("\n})();") + 6]


def test_the_page_code_behaves():
    """Запоминание, восстановление, срок годности и отложенная запись."""
    node = shutil.which("node")
    if not node:
        pytest.skip("нет node — исполнить код страницы нечем")
    r = subprocess.run(
        [node, str(ROOT / "tests" / "builder_keep_harness.cjs"), str(TPL)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert r.returncode == 0, r.stderr


def test_the_store_is_versioned():
    """Ключ с версией: следующая раскладка данных не должна разбираться как
    эта и раскладывать по форме поля, которых в ней больше нет."""
    block = _block()
    assert "acl.builder.v2" in block
    assert "parsed.v !== VERSION" in block, "чужая версия читается как своя"


def test_a_broken_store_never_takes_the_page_down():
    """В хранилище может лежать что угодно: чужая вкладка, недописанная
    запись, приватное окно. Дешборд от этого не должен переставать
    открываться — а открывают его раз в день."""
    block = _block()
    assert block.count("catch (e)") >= 3, "чтение или запись не обёрнуты"
    assert "try { parsed = JSON.parse" in block


def test_nothing_here_talks_to_the_server():
    """Собранное живёт в браузере. Не «пока живёт» — по замыслу: у десяти
    байеров десять разных наборов, и общего места для них нет."""
    block = _block()
    for forbidden in ("fetch(", "XMLHttpRequest", "hx-post", "/publish"):
        assert forbidden not in block, f"хранилище полезло наружу: {forbidden}"


def test_the_visible_search_text_is_kept_beside_the_value():
    """У поиска два значения: скрытое (список интересов) и видимое (код,
    который набрал человек). Сохранив только первое, после перезагрузки
    получаешь заполненную кампанию с пустым на вид полем."""
    block = _block()
    assert "shown" in block and ".cb-sel-text" in block


def test_the_key_is_taken_while_the_form_is_still_in_its_block():
    """Гостя в ящике, форма вынута из блока страны, и спросить у неё
    страну будет не у кого. Ключ ставится заранее."""
    block = _block()
    assert "form.dataset.cbdKey" in block
    assert "forms().forEach(stamp)" in block, "ключи не проставляются при загрузке"
