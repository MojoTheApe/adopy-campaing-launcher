"""Свёрнутая строка Languages раскрывается карандашом или кликом по значению (ACL-1226).

17.09 23:04 CR_ait Day: блок аудитории показан сводкой «Languages: All languages»,
поля поиска нет, клик по центру строки его не открывал. 18.09 08:23: у второго
вида свёрнутого блока нет ни одной кнопки — только текст значения.
"""
from __future__ import annotations

import pytest
from playwright.sync_api import sync_playwright

from src.fb_ads_rpa import meta_dom as md


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


@pytest.fixture
def page(browser, monkeypatch):
    pg = browser.new_page()
    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    monkeypatch.setattr(md, "_cdp_mouse_click", lambda p, x, y: p.evaluate(
        "([x, y]) => { const e = document.elementFromPoint(x, y); window.clicked = e && e.id; if (e) e.click(); }",
        [x, y]))
    yield pg
    pg.close()


def test_the_pencil_of_the_languages_row_is_clicked(page):
    page.set_content(
        "<div style='position:absolute;left:0;top:0'><div><span>Languages</span>"
        "<span>All languages</span>"
        "<div role='button' aria-label='Edit' id='pen' style='width:24px;height:24px'></div></div></div>")
    assert md._click_languages_edit(page) is True
    assert page.evaluate("window.clicked") == "pen"


def test_a_languages_row_without_buttons_is_opened_by_its_value(page):
    page.set_content(
        "<div style='position:absolute;left:0;top:0'><div><div>Languages</div>"
        "<div id='val'>All languages</div></div></div>")
    assert md._click_languages_edit(page) is True
    assert page.evaluate("window.clicked") == "val"


def test_nothing_to_open_is_reported_honestly(page):
    page.set_content("<div>Audience</div>")
    assert md._click_languages_edit(page) is False
