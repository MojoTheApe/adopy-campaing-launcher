"""Ящик билдера: один на страницу, открыт на любой вкладке, не гаснет от клика мимо.

Собирать кампанию приходилось там, где она живёт: уйти со страницы, которую
читаешь, найти страну, домотать до билдера и набрать по памяти то, что было
на экране минуту назад. Ящик снимает этот переход — он открыт поверх любой
вкладки, и страница за ним остаётся живой.

Живой — не оборот речи. Прежняя правая панель закрывалась от клика мимо себя,
и выделить текст в таблице слева, чтобы вставить его в поле справа, было
нельзя: панель захлопывалась вместе с набранным. Ради этого ящик сделан
немодальным, и здесь это проверяется прямо.

Поведение проверяется исполнением кода страницы (`builder_drawer_harness.cjs`),
разметка — отрисовкой шаблона. По исходнику видно только то, что условие есть,
а не что оно делает.
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
sys.path.insert(0, str(Path(__file__).resolve().parent))

TPL = ROOT / "src" / "web" / "templates" / "report.html"


def _no_comments(text: str) -> str:
    """Без комментариев Jinja, JS и CSS.

    Сторож, читающий объяснение вместо кода, ловит собственный текст: за эту
    неделю так случалось трижды.
    """
    text = re.sub(r"\{#.*?#\}", "", text, flags=re.S)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"^\s*//.*$", "", text, flags=re.M)


# ── как он себя ведёт ─────────────────────────────────────────────────────

def test_the_page_code_behaves():
    """Открытие, закрытие, память и счётчик — на живом коде шаблона."""
    node = shutil.which("node")
    if not node:
        pytest.skip("нет node — исполнить код страницы нечем")
    r = subprocess.run(
        [node, str(ROOT / "tests" / "builder_drawer_harness.cjs"), str(TPL)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert r.returncode == 0, r.stderr


def test_nothing_outside_the_drawer_can_close_it():
    """Тот самый изъян прежней панели, из-за которого всё и затевалось.

    Проверяется дважды: здесь — что в коде ящика нет ни подложки, ни
    обработчиков на документе; в harness — что их нет и на исполнении.
    """
    src = _no_comments(TPL.read_text(encoding="utf-8"))
    block = src[src.index("window.cbDrawer = (function () {"):]
    block = block[: block.index("\n})();") + 6]
    assert "document.addEventListener" not in block, "ящик слушает документ"
    assert "Escape" not in block, (
        "Escape прилетает из полей ввода — закрыть ящик на полуслове он не должен"
    )
    assert "cbd-back" not in src and "aria-modal" not in block, "у ящика появилась подложка"


# ── где он стоит ──────────────────────────────────────────────────────────

def _render(countries):
    from test_web_drill import _MOCK_REPORT, _render_html
    return _render_html({**_MOCK_REPORT, "countries": countries})


def test_there_is_exactly_one_drawer_on_the_page():
    """Панелей «по копии на страну» на этой странице уже хватило: их было
    двадцать, с двадцатью полями пароля, и это чинили отдельной задачей."""
    html = _render(_mock_countries())
    assert html.count('id="cbd-root"') == 1, "ящиков на странице не один"
    assert html.count('id="cbd-panel"') == 1


def test_the_drawer_is_there_even_without_a_workbook():
    """Весь дешборд завёрнут в `{% if report.countries %}`. Ящик стоит
    снаружи: собирать кампании можно и когда книги нет, а вкладка Accounts
    живёт без стран."""
    html = _render([])
    assert 'id="cbd-root"' in html, "без книги ящик исчез вместе с дешбордом"


def test_the_drawer_does_not_live_inside_a_tab():
    """Вкладки переключаются без перезагрузки. Ящик, стоящий в одной из них,
    исчезал бы при переходе — а он должен быть открыт на любой."""
    html = _render(_mock_countries())
    before = html[: html.index('id="cbd-root"')]
    assert before.count("<main") == before.count("</main>"), "ящик оказался внутри main"
    assert before.count('class="view-panel') == html.count('class="view-panel'), (
        "после ящика идут вкладки — значит, он стоит среди них"
    )


def test_the_handle_says_how_many_are_being_assembled():
    """Закрытый ящик — это язычок и больше ничего. Пусть он что-то говорит."""
    html = _render(_mock_countries())
    assert 'id="cbd-count"' in html
    assert 'class="cbd-handle"' in html


def test_the_width_is_one_number():
    """Ширина ящика и отступ страницы, который его освобождает, — одно и то
    же число. Разъехавшись, они оставят полосу пустоты или щель.

    Язычку ширина задана явно и входит в отступ: он торчит слева от панели,
    и без этого открытый ящик кладёт его поверх таблицы."""
    css = TPL.read_text(encoding="utf-8")
    # ACL-1769: две колонки — 480, а не 360; растянутая панель ширину не
    # переопределяет (у неё своё правило на `.cbd-panel`), так что число одно.
    assert "--cbd-w: 480px;" in css and "--cbd-handle-w: 40px;" in css
    assert css.count("--cbd-w: ") == 1, "ширина задана в двух местах"
    assert css.count("--cbd-handle-w: ") == 1
    assert "width: var(--cbd-handle-w); box-sizing: border-box;" in css, (
        "ширина язычка отдана содержимому — отступ страницы станет догадкой"
    )
    assert "padding-right: calc(var(--cbd-w) + var(--cbd-handle-w));" in css


def _mock_countries():
    from test_web_drill import _MOCK_REPORT
    return [dict(c) for c in _MOCK_REPORT["countries"]]
