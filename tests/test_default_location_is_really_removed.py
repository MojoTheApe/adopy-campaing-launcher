# -*- coding: utf-8 -*-
"""Страна по умолчанию действительно снимается, а не «снята» (Doc21, 16.09).

GT_Ait_All_Video_PF_35_16.09: в локациях остались Guatemala **и** United
States, а шаг записал `locations_remove_default_removedTrue` — старый код
нажал какой-то «Close» (на странице есть ✕ у баннера WhatsApp Status в
площадках) и счёл дело сделанным. Раскладка 2026-09 группирует локации по
странам: заголовок страны с ✕ рядом.

Проверяется в офлайн-Chrome на карточке той же формы: снимается именно
лишняя страна, нужная остаётся, ✕ вне карточки не трогается, безымянные
иконки (сворачивание карточки) не трогаются, а неснятая страна — отказ шага.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import meta_dom as md  # noqa: E402

_CARD = """
<!doctype html><html><body>
<div id="banner">WhatsApp Status included <div role="button" style="display:inline-block;width:20px;height:20px;overflow:hidden" onclick="window.clicked.push('banner')"><span>Close</span></div></div>
<div id="loc">
  <div>Locations <div role="button" style="display:inline-block;width:20px;height:20px;overflow:hidden" id="collapse" onclick="window.clicked.push('collapse')"><svg width="12" height="12"></svg></div></div>
  <div class="grp" data-c="Guatemala"><div>Guatemala <div role="button" style="display:inline-block;width:20px;height:20px;overflow:hidden" onclick="drop('Guatemala')"><span>Close</span></div></div><div>Guatemala</div></div>
  <div class="grp" data-c="United States"><div>United States <div role="button" style="display:inline-block;width:20px;height:20px;overflow:hidden" %(us_click)s><span>Close</span></div></div><div>United States</div></div>
  <div><div>Include</div><div role="combobox"><input placeholder="Search locations"></div><div role="button">Browse</div></div>
</div>
<script>
  window.clicked = [];
  function drop(c) { window.clicked.push(c); const g = document.querySelector('.grp[data-c="' + c + '"]'); if (g) g.remove(); }
</script>
</body></html>
"""


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=os.environ.get("ACL_BROWSER_EXECUTABLE") or None)
        try:
            yield b
        finally:
            b.close()


@pytest.fixture
def page(browser, monkeypatch):
    monkeypatch.setattr(md, "_sleep_h", lambda s: None)
    monkeypatch.setattr(md, "_human_jitter", lambda base, spread: 0.0)
    ctx = browser.new_context(viewport={"width": 1280, "height": 800})
    p = ctx.new_page()
    try:
        yield p
    finally:
        ctx.close()


def test_the_stray_default_country_is_removed_and_the_target_stays(page):
    page.set_content(_CARD % {"us_click": "onclick=\"drop('United States')\""})
    assert md._clear_other_locations(page, "Guatemala") == []
    assert page.evaluate("window.clicked") == ["United States"]
    assert page.evaluate("!!document.querySelector('.grp[data-c=Guatemala]')")


def test_a_close_outside_the_locations_card_is_never_clicked(page):
    page.set_content(_CARD % {"us_click": "onclick=\"drop('United States')\""})
    md._clear_other_locations(page, "Guatemala")
    clicked = page.evaluate("window.clicked")
    assert "banner" not in clicked and "collapse" not in clicked


def test_a_default_that_will_not_go_is_reported_by_name(page):
    page.set_content(_CARD % {"us_click": "onclick=\"window.clicked.push('noop')\""})
    left = md._clear_other_locations(page, "Guatemala")
    assert left and "United States" in left[0]


def test_nothing_to_clear_is_clean(page):
    page.set_content(_CARD % {"us_click": "onclick=\"drop('United States')\""})
    page.evaluate("document.querySelector('.grp[data-c=\"United States\"]').remove()")
    assert md._clear_other_locations(page, "Guatemala") == []
    assert page.evaluate("window.clicked") == []
