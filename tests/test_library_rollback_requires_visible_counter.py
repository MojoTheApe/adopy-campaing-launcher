"""A hidden/unmounted library counter is not proof of a clean selection."""
import os

import pytest
from playwright.sync_api import sync_playwright
from src.fb_ads_rpa import meta_dom as md


@pytest.fixture(scope='module')
def browser():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get('ACL_BROWSER_EXECUTABLE') or None)
        yield browser
        browser.close()


@pytest.fixture
def page(browser, monkeypatch):
    page = browser.new_page()
    monkeypatch.setattr(md, '_sleep_h', lambda *a: None)
    page.set_content('''<div role="dialog"><div id="count">1 selected</div>
      <button onclick="window.selected=false; document.querySelector('#count').textContent='0 selected'">video.mp4</button>
      </div><script>window.selected=true</script>''')
    monkeypatch.setattr(md, '_close_creative_setup_modal', lambda p: p.evaluate("document.querySelector('[role=dialog]').remove()"))
    yield page
    page.close()


def test_rollback_deselects_before_closing_the_grid(page):
    assert md._retreat(page, page.get_by_role('button'), [0], 0) == md.REUSE_ABSENT
    assert page.evaluate('window.selected') is False


def test_missing_counter_is_unknown_and_never_allows_fallback(page):
    page.locator('#count').evaluate('el => el.remove()')
    assert md._library_selected_count(page) is None
    assert md._retreat(page, page.get_by_role('button'), [0], 0) == md.REUSE_DIRTY
    assert page.evaluate('window.selected') is True


def test_an_exclusive_replacement_is_not_proven_undone_by_the_same_count(page):
    assert md._retreat(page, page.get_by_role('button'), [0], 1) == md.REUSE_DIRTY


def test_unknown_initial_selection_blocks_fallback(page):
    # ACL-1669: без счётчика, но и с отмеченной плиткой — по-прежнему отказ.
    page.locator('#count').evaluate('el => el.remove()')
    page.evaluate("document.querySelector('button').setAttribute('data-test-decoration', 'selected')")
    assert md._try_reuse_library_media(page, 'missing.mp4') == md.REUSE_DIRTY


def test_no_counter_and_nothing_marked_steps_back_for_a_fresh_upload(page):
    # ACL-1669 (B97, 07.10): счётчика нет, плиток с отметкой нет — ничего не
    # кликаем, файл грузится заново.
    page.locator('#count').evaluate('el => el.remove()')
    assert md._try_reuse_library_media(page, 'missing.mp4') == md.REUSE_ABSENT
    assert page.evaluate('window.selected') is True  # ничего не тронуто
