# -*- coding: utf-8 -*-
"""Обход мастера креатива: одна кнопка «вперёд» — один вызов страницы (ACL-1313).

Прогоны 25.09: от выбора плитки в медиатеке до «медиа на объявлении» —
35–55 с на КАЖДОЕ объявление (library_pick_selected → ad_media_ready в
rpa_pages). Большую часть этого съедает обход мастера: на каждом круге
подписи «Done», «Save», «Next», … проверялись по одной, и каждая — XPath
`normalize-space(.)` по всему документу редактора. На шаге Trim («Skip and
continue») это четыре пустых прохода по DOM до нужного.

Счёт вызовов страницы — на синтетическом мастере из
test_the_wizard_after_upload_is_walked_out.py, в офлайн-Chrome.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import meta_dom  # noqa: E402
from tests.test_the_wizard_after_upload_is_walked_out import (  # noqa: E402,F401
    NEW_SHAPE, OLD_SHAPE, _open, browser, page)


class _Counting:
    """Страница, которая считает вызовы `evaluate` по их назначению."""

    def __init__(self, page):
        self._page = page
        self.xpath_scans = 0
        self.forward_lookups = 0

    def evaluate(self, js, *args):
        if "document.evaluate(" in js:
            self.xpath_scans += 1
        if js is meta_dom._WIZARD_FORWARD_JS:
            self.forward_lookups += 1
        return self._page.evaluate(js, *args)

    def __getattr__(self, name):
        return getattr(self._page, name)


def test_each_step_costs_one_forward_lookup(page):
    _open(page, NEW_SHAPE)
    counting = _Counting(page)
    assert meta_dom._walk_creative_wizard(counting) is True
    clicks = page.evaluate("window.clicks")
    assert clicks[-1] == "close"
    presses = len(clicks) - 1           # «close» — не нажатие, а итог последнего
    assert counting.xpath_scans == 0, "подписи снова ищутся XPath по одной"
    assert counting.forward_lookups == presses


def test_label_priority_is_kept(page):
    """Две кнопки сразу — берётся та, что раньше в списке (Done раньше Next)."""
    page.set_content(
        "<div><span>Creative setup</span><span>Media</span><span>Text</span>"
        "<button id='n' onclick=\"window.hit='Next'\">Next</button>"
        "<button id='d' onclick=\"window.hit='Done'\">Done</button>"
        "<button aria-disabled='true' onclick=\"window.hit='Save'\">Save</button></div>")
    assert meta_dom._press_wizard_forward(page) == "Done"
    assert page.evaluate("window.hit") == "Done"


def test_a_disabled_or_hidden_button_is_not_pressed(page):
    page.set_content(
        "<button aria-disabled='true'>Done</button>"
        "<div style='display:none'><button>Next</button></div>")
    assert meta_dom._press_wizard_forward(page) is None


def test_the_walk_ends_as_soon_as_the_wizard_closes(page, monkeypatch):
    """Последнее нажатие закрыло мастер — выходим, не досиживая 1,4 с."""
    _open(page, OLD_SHAPE, start=7)     # один шаг: «Done» закрывает мастер
    clock = {"now": 0.0}

    def sleep(s):
        clock["now"] += s

    monkeypatch.setattr(meta_dom, "time", SimpleNamespace(time=lambda: clock["now"], sleep=sleep))
    monkeypatch.setattr(meta_dom, "_sleep_h", lambda base, spread=0.3: sleep(base))
    t0 = time.time()
    assert meta_dom._walk_creative_wizard(page) is True
    # Часы видят и паузы самого клика (путь курсора ~0,1 с); прежде сверху
    # всегда шли ещё 1,4 с.
    assert clock["now"] < 1.0, clock["now"]
    assert time.time() - t0 < 10
