"""Чтение спенда: строку итогов сперва надо показать.

Строка итогов в Ads Manager стоит **под** списком кампаний. В невысоком окне
Meta её не отрисовывает вовсе — и тогда ожидание бессмысленно: ждать можно
сколько угодно того, чего не рисуется.

8 сентября прогон на 1–8 сентября сорвался именно так. На снимке, который
читалка сохранила при отказе, окно обрывается сразу под первой строкой
кампании, даты стоят верные, спиннер над колонкой Results ещё крутится, а
строки итогов на странице нет. Двадцать секунд ожидания ничего не меняли.

Проверяется наш порядок действий — прокрутить, потом смотреть, — а не разметка
Meta. Поэтому страница здесь поддельная: она отвечает на вызовы и записывает,
в каком порядке её просили. Разметку Meta проверит стенд из `acl-340`.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.analysis_agent import account_spend as sp  # noqa: E402


class _Page:
    """Страница, которая помнит, о чём её просили и в каком порядке.

    `totals_after` — сколько осмотров пройдёт, прежде чем итоги «появятся»;
    появляются они только если до этого была прокрутка.
    """

    def __init__(self, *, totals_after: int = 0, needs_scroll: bool = True):
        self.calls: list[str] = []
        self.scrolled = 0
        self.scans = 0
        self.totals_after = totals_after
        self.needs_scroll = needs_scroll

    def evaluate(self, script, *a, **kw):
        if "scrollTop" in script:
            self.calls.append("scroll")
            self.scrolled += 1
            return True
        self.calls.append("scan")
        self.scans += 1
        ready = self.scans > self.totals_after
        if self.needs_scroll and self.scrolled == 0:
            ready = False
        return {"spend": "$1,234.00" if ready else None, "label": ready}

    def query_selector(self, sel):
        return None


def _read(page):
    with patch.object(sp, "_wait_for_grid", return_value=True), \
         patch.object(sp, "_sleep"):
        return sp._read_total(page)


def test_the_list_is_scrolled_before_it_is_examined():
    """Сперва показать низ списка, потом искать в нём.

    Осмотр раньше прокрутки — это осмотр пустого места.
    """
    page = _Page()
    _read(page)
    assert page.calls[0] == "scroll", (
        "первым действием должна быть прокрутка, иначе итогов ещё нет на странице"
    )


def test_a_totals_row_below_the_fold_is_still_found():
    """Тот самый случай 8 сентября: строка есть, но её не видно."""
    value, how = _read(_Page(needs_scroll=True))
    assert value == 1234.0
    assert "Total spent" in how


def test_scrolling_happens_before_every_look_not_once():
    """Список догружается по мере прокрутки — низ появляется не сразу."""
    page = _Page(totals_after=3)
    _read(page)
    assert page.scrolled >= 4, f"прокрутили лишь {page.scrolled} раз за {page.scans} осмотров"
    assert page.calls[:4] == ["scroll", "scan", "scroll", "scan"]


def test_a_page_that_never_shows_totals_says_so_and_mentions_scrolling():
    """Отказ должен отличаться от прежнего: прокрутку уже пробовали."""
    class _Empty(_Page):
        def evaluate(self, script, *a, **kw):
            if "scrollTop" in script:
                return True
            return {"spend": None, "label": False}

    value, how = _read(_Empty())
    assert value is None
    assert "прокрутки" in how, "человек не поймёт, что этот путь уже пройден"


def test_a_page_that_refuses_to_scroll_does_not_break_the_read():
    """Прокрутка — попытка помочь, а не условие работы."""
    class _Stubborn(_Page):
        def evaluate(self, script, *a, **kw):
            if "scrollTop" in script:
                raise RuntimeError("нельзя")
            return {"spend": "$7.00", "label": True}

    value, _ = _read(_Stubborn())
    assert value == 7.0


def test_an_already_visible_total_is_read_without_waiting():
    """Обычный случай — итоги на виду. Лишних кругов быть не должно."""
    page = _Page(needs_scroll=False)
    value, _ = _read(page)
    assert value == 1234.0
    assert page.scans == 1, f"сделано {page.scans} осмотров вместо одного"
