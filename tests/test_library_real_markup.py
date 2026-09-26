# -*- coding: utf-8 -*-
"""Медиатека и объявление на живой разметке (ревью ACL-1171 / ACL-1192).

Снимки 18.09, сохранённые целиком через page_capture.capture(blocks=...):

* library_one_picked.html — CO_bnc Mix2, счётчик «1 selected», три плитки;
* library_multi_select_picked.html — CR_ait Mix, режим «до 10 файлов»,
  счётчики «1 of 10 selected» и «0 selected» (Image from AI);
* ad_media_attached.html — блок Media объявления CO_bnc Mix2 после
  прикрепления: миниатюры у строк размещения.
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

FIX = Path(__file__).parent / "fixtures" / "meta"
PICKED_ID = "813948786_38351125907864671_2841079256059848182_n"


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=os.environ.get("ACL_BROWSER_EXECUTABLE") or None)
        try:
            yield b
        finally:
            b.close()


@pytest.fixture
def page(browser):
    ctx = browser.new_context(viewport={"width": 1707, "height": 1400})
    p = ctx.new_page()
    p.route("**/*", lambda route: route.abort())
    try:
        yield p
    finally:
        ctx.close()


def _load(page, name):
    page.set_content("<div role='dialog'>" + (FIX / name).read_text(encoding="utf-8") + "</div>")


def test_the_counter_reads_the_single_select_library(page):
    _load(page, "library_one_picked.html")
    assert md._library_selected_count(page) == 1


def test_the_counter_reads_the_multi_select_library(page):
    """«1 of 10 selected» + «0 selected»: читали только «N selected» и видели 0."""
    _load(page, "library_multi_select_picked.html")
    assert md._library_selected_count(page) == 1


def test_a_picked_tile_is_seen_as_marked_in_the_real_grid(page):
    _load(page, "library_one_picked.html")
    assert md._library_any_tile_marked(page) is True


def test_an_unmarked_real_grid_is_clean(page):
    _load(page, "library_one_picked.html")
    page.evaluate("""() => document.querySelectorAll('[data-test-decoration="selected"],[aria-selected="true"]')
      .forEach(e => { e.removeAttribute('data-test-decoration'); e.setAttribute('aria-selected', 'false'); })""")
    assert md._library_any_tile_marked(page) is False


def _unpicked_real_grid(page):
    """Живая сетка 18.09 без выбора плитки; вкладка Account остаётся выбранной."""
    _load(page, "library_one_picked.html")
    page.locator('[data-test-decoration="selected"]').evaluate(
        "el => el.removeAttribute('data-test-decoration')")
    page.get_by_text('1 selected', exact=True).evaluate("el => el.textContent = '0 selected'")
    assert page.get_by_role('tab', name='Account', exact=True).get_attribute('aria-selected') == 'true'


def test_account_tab_selection_is_not_a_selected_video(page, monkeypatch):
    """Порт ACL-1284: вкладка Account с `aria-selected=true` — не отмеченная плитка."""
    _unpicked_real_grid(page)
    monkeypatch.setattr(md, '_sleep_h', lambda *_: None)
    monkeypatch.setattr(md, '_library_search', lambda *_: True)
    monkeypatch.setattr(md._telemetry, 'record', lambda *a, **k: None)
    assert md._library_any_tile_marked(page) is False
    assert md._try_reuse_library_media(page, 'not-in-the-library.mp4') == md.REUSE_ABSENT


def test_a_pick_that_did_not_take_leaves_the_real_grid_clean(page, monkeypatch):
    """Клик не взялся, счётчик «0 selected» — откат чистый, загрузка разрешена.

    Пока вкладка Account считалась отмеченной плиткой, этот откат всегда
    отвечал «грязно» — вероятная причина `library_pick_left_selection`
    живых прогонов 19–25.09.
    """
    _unpicked_real_grid(page)
    monkeypatch.setattr(md, '_sleep_h', lambda *_: None)
    assert md._library_undo_selection(page, None, [0], 0) is True


def test_a_tile_still_marked_in_the_real_grid_is_not_clean(page, monkeypatch):
    _load(page, "library_one_picked.html")
    page.get_by_text('1 selected', exact=True).evaluate("el => el.textContent = '0 selected'")
    monkeypatch.setattr(md, '_sleep_h', lambda *_: None)
    assert md._library_any_tile_marked(page) is True
    assert md._library_undo_selection(page, None, [0], 0) is False


def test_the_picked_video_is_identified_on_the_ad_by_its_thumbnail(page):
    """ACL-1171: у объявления миниатюра того же файла, что у выбранной плитки."""
    page.set_content((FIX / "ad_media_attached.html").read_text(encoding="utf-8"))
    assert PICKED_ID in md._ad_media_thumb_ids(page)


def test_the_picked_tile_carries_the_same_thumbnail_id(page):
    _load(page, "library_one_picked.html")
    tile = page.locator('[data-test-decoration="selected"]').first
    assert PICKED_ID in md._tile_thumb_ids(tile)


def test_a_missing_grid_is_unknown_even_when_an_unrelated_counter_is_zero(page, monkeypatch):
    """Сетки на экране нет — «неизвестно», и откат после клика не чистый.

    Порт ACL-1284: живой код отвечал здесь False («ничего не отмечено»), и
    откат после клика по плитке без видимой сетки разрешал загрузку поверх.
    Без клика решает счётчик, как в живом коде: мы ничего не трогали.
    """
    page.set_content('<div>0 selected</div>')
    monkeypatch.setattr(md, "_sleep_h", lambda *_: None)
    monkeypatch.setattr(md, "_close_creative_setup_modal", lambda *_: None)
    assert md._library_any_tile_marked(page) is None
    assert md._library_undo_selection(page, None, [0], 0) is False
    assert md._retreat(page, None, [0], 0) == md.REUSE_DIRTY
    assert md._retreat(page, None, [], 0) == md.REUSE_ABSENT
    page.set_content('<div>1 selected</div>')
    assert md._retreat(page, None, [], 0) == md.REUSE_DIRTY
