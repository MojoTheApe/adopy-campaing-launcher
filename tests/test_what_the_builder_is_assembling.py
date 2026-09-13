"""То, что собирается, переживает F5 — и не копится вечно.

Собранная наполовину кампания есть работа: имя, бюджет, таргетинг, тексты.
Потерять её на перезагрузке — потерять час чужого вечера, а перезагрузка
здесь случается часто: дешборд просят обновить, страница уходит на Refresh.

Обратная сторона — хранилище само ничего не забывает. Без срока годности
через месяц в ящике лежат позапрошлые черновики, и список, который должен
отвечать «что я собираю», отвечает «что я когда-либо начинал».

Поведение проверяется исполнением кода страницы
(`builder_draft_store_harness.cjs`) с часами в руках: срок годности иначе
проверить нечем.
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


def _store_block() -> str:
    src = TPL.read_text(encoding="utf-8")
    block = src[src.index("window.cbDraft = (function () {"):]
    return block[: block.index("\n})();") + 6]


def test_the_page_code_behaves():
    """Память, срок годности, группировка и счётчик — на живом коде."""
    node = shutil.which("node")
    if not node:
        pytest.skip("нет node — исполнить код страницы нечем")
    r = subprocess.run(
        [node, str(ROOT / "tests" / "builder_draft_store_harness.cjs"), str(TPL)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert r.returncode == 0, r.stderr


def test_the_store_is_versioned():
    """Ключ с версией: следующая раскладка данных не должна разбираться
    как эта и рисовать чепуху из полей, которых больше нет."""
    block = _store_block()
    assert "acl.builder.v1" in block
    assert "parsed.v !== VERSION" in block, "чужая версия читается как своя"


def test_a_broken_store_never_takes_the_page_down():
    """В хранилище может лежать что угодно: чужая вкладка, недописанная
    запись, приватное окно. Дешборд от этого не должен переставать
    открываться — а он открывается ровно один раз в день."""
    block = _store_block()
    assert block.count("catch (e)") >= 4, "чтение или запись не обёрнуты"
    assert "JSON.parse" in block and "try { parsed = JSON.parse" in block


def test_nothing_here_talks_to_the_server():
    """Собранное живёт в браузере. Не «пока живёт» — по замыслу: у десяти
    байеров десять разных наборов, и общего места для них нет."""
    block = _store_block()
    for forbidden in ("fetch(", "XMLHttpRequest", "htmx", "hx-post"):
        assert forbidden not in block, f"хранилище полезло наружу: {forbidden}"
