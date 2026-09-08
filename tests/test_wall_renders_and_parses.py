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
      Why it was done, and what it cost.
- [ ] `acl-002` An unfinished thing #next @rpa
  - [x] `acl-002a` The first half
  - [ ] `acl-002b` The second half
- [ ] `acl-004` Nothing to expand #later @ops

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


def test_a_card_with_a_note_folds():
    """Карточку тоже сворачиваем: развёрнутая область — снова стена текста."""
    html = _html()
    assert '<details class="pin' in html, "карточка перестала сворачиваться"
    assert 'class="pin-head"' in html, "у карточки нет кликабельной шапки"
    assert "pin-caret" in html, "у карточки нет стрелки"


def _tag_of(html: str, task_id: str) -> str:
    """Каким тегом отрисована карточка задачи."""
    m = re.search(rf'<(\w+) class="pin[^"]*"[^>]*data-id="{task_id}"', html)
    assert m, f"карточки {task_id} нет на витрине"
    return m.group(1)


def test_a_card_with_nothing_inside_stays_flat():
    """Стрелка, за которой пусто, обещает содержимое и не отдаёт его."""
    html = _html()
    assert _tag_of(html, "acl-004") == "article", (
        "карточка без примечания и подзадач стала сворачиваемой"
    )
    assert _tag_of(html, "acl-001") == "details", "карточка с примечанием не сворачивается"
    assert _tag_of(html, "acl-002") == "details", "карточка с подзадачами не сворачивается"


def test_the_collapsed_card_still_shows_what_identifies_it():
    """Свёрнутой видно номер, название, статус и счётчик частей."""
    html = _html()
    head = html[html.index('class="pin-head"'):]
    head = head[: head.index("</summary>")]
    assert "acl-001" in head or "acl-002" in head, "номер задачи спрятан внутрь"
    assert "<h4>" in head, "название спрятано внутрь"
    assert "<footer>" in head, "статус спрятан внутрь"


def test_one_memory_mechanism_for_areas_and_cards():
    """Две разные памяти о том же самом разошлись бы."""
    script = _SCRIPT.findall(_html())[0]
    assert script.count("function watch(") == 1, "память снова написана дважды"
    assert "watch(areas," in script and "watch(pins," in script


def test_the_page_declares_its_encoding():
    """Без этого страница читается только там, где кодировку шлёт сервер.

    Витрину открывают и с диска, и с чужой машины, и показывают начальству.
    До этой правки `<meta charset>` не было вовсе: на claude.ai всё выглядело
    целым, потому что кодировку присылал их сервер, а с простого сервера
    каждая галочка, тире и кириллица рассыпались в «âœ“» и «â€"». Ошибка,
    которую видно только там, где её никто не проверяет.
    """
    html = _html()
    assert '<meta charset="utf-8">' in html.lower(), "страница не объявляет кодировку"
    assert html.lower().index("charset") < html.lower().index("<title"), (
        "объявление кодировки должно идти до заголовка"
    )


def test_a_duplicate_id_in_subtasks_is_caught():
    """Прежняя проверка смотрела только верхний уровень.

    8 сентября номер был выдан дважды именно подзадаче, и сторож промолчал:
    проверка была, а случай не покрывала. Повтор опасен тем, что `by_id`
    возвращает первое совпадение — коммит отметит чужую задачу, а в журнал
    ляжет строка, которая выглядит правдой.
    """
    text = (
        "## 1 · Area\n\n### Cap\n\n"
        "- [ ] `acl-001` One\n"
        "  - [ ] `acl-002a` Same id here\n"
        "- [ ] `acl-003` Two\n"
        "  - [ ] `acl-002a` And here\n"
    )
    assert b.duplicate_ids(b.parse(text)) == ["acl-002a"]


def test_a_clean_board_has_no_duplicates():
    assert b.duplicate_ids(b.parse(_SAMPLE)) == []


def test_merged_is_not_the_same_as_shipped():
    """«Влито» и «роздано» — разные вещи, и между ними бывает много дней.

    Автообновление принимает только вершину master с аннотированным
    `acl-release-*` тегом. 8 сентября три ветки были в master, а последний тег
    стоял на сборке от 3 сентября: код влит, машины его не видят. Пока
    состояний было два, слитое приходилось звать `shipped`, и доска
    утверждала, что работа у людей.
    """
    assert "merged" in b.STATUSES, "состояние «в master» исчезло"
    assert b.STATUS_LABEL["merged"] == "In master"
    assert b.STATUSES.index("merged") < b.STATUSES.index("review"), (
        "порядок состояний должен идти от сделанного к незачатому"
    )


def test_a_merged_task_renders_with_its_own_colour():
    text = _SAMPLE.replace("#shipped @dashboard", "#merged @dashboard")
    html = b.render(b.parse(text))
    assert "s-merged" in html, "у слитой задачи нет своего вида"
    assert "In master" in html
