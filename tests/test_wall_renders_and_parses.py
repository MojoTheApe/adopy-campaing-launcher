"""Витрина доски: свёрнутые области и живой JavaScript.

Сторож на разбор появился здесь после случая на дешборде: одна строковая
константа с настоящим переводом строки внутри кавычек уронила весь блок
`<script>` — и вместе с ним перестал открываться календарь, хотя правка была
про другое. Все проверки шаблона тогда искали текст, а текст был на месте.
Здесь страница отдаётся тому же разборщику, что стоит в браузере.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import acl_board as b  # noqa: E402

_SCRIPT = re.compile(r"<script[^>]*>(.*?)</script>", re.S)

_SAMPLE = """---
board: ACL
---

## 1 · First area

> What this area is for.

### Some capability

- [x] `acl-001` A finished thing #shipped @dashboard
- [ ] `acl-002` An unfinished thing #next @rpa

## 2 · Second area

### Another capability

- [ ] `acl-003` Waiting on someone #blocked @ops
"""


def _html() -> str:
    return b.render(b.parse(_SAMPLE))


def test_areas_are_collapsible_and_closed_by_default():
    html = _html()
    assert "<details class=\"area\"" in html, "область перестала быть сворачиваемой"
    assert "<summary class=\"area-head\"" in html, "шапка не является summary"
    assert "<details class=\"area\" id=\"area-1\" data-area=\"1\" open" not in html, (
        "область раскрыта разметкой — свёрнутость должна быть состоянием по умолчанию"
    )


def test_the_heading_and_its_blurb_stay_visible_when_collapsed():
    """Заголовок и подзаголовок — в шапке, а не внутри раскрываемой части."""
    html = _html()
    head = html[html.index("<summary class=\"area-head\""):]
    head = head[: head.index("</summary>")]
    assert "First area" in head
    assert "What this area is for." in head


def test_the_caret_is_in_the_header():
    assert "area-caret" in _html(), "исчезла стрелка, которой область открывают"


@pytest.mark.skipif(shutil.which("node") is None, reason="нет node — разобрать нечем")
def test_the_page_script_parses():
    blocks = _SCRIPT.findall(_html())
    assert blocks, "на витрине не осталось ни одного скрипта"
    for i, body in enumerate(blocks):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / f"block{i}.js"
            f.write_text(body, encoding="utf-8")
            r = subprocess.run(
                [shutil.which("node"), "--check", str(f)],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
            assert r.returncode == 0, (
                "витрина отдаёт браузеру нерабочий JavaScript:\n" + (r.stderr or "")
            )


def test_a_filter_opens_what_it_finds():
    """Свёрнутая область под фильтром читается как «ничего не найдено»."""
    html = _html()
    script = _SCRIPT.findall(html)[0]
    assert "a.open = !!any" in script, (
        "фильтр снова не раскрывает найденное — нажатие есть, ответа нет"
    )


def test_the_table_of_contents_opens_its_target():
    script = _SCRIPT.findall(_html())[0]
    assert "nav.areas a" in script, "ссылка оглавления снова ведёт к закрытой шапке"


def test_the_intro_counts_the_areas_it_shows():
    """В подзаголовке стояло «Five areas», когда их было шесть.

    Мелочь, которая учит не верить остальному тексту на странице.
    """
    html = _html()
    assert "2 areas of the system" in html, "число областей снова написано на глаз"
