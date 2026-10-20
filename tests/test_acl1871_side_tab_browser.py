"""ACL-1871: размер билдера с бокового язычка — три кнопки и перетаскивание (реальный DOM)."""
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from urllib.parse import urlparse

import pytest
from playwright.sync_api import sync_playwright
from test_builder_per_account import _render


@pytest.fixture(scope='module')
def browser():
    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=os.environ.get('ACL_BROWSER_EXECUTABLE') or None)
        yield b
        b.close()


def _open(browser, width=1400, storage=None):
    context = browser.new_context(viewport={'width': width, 'height': 900})
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    html = _render({'Demo': []})

    def route(r):
        if r.request.resource_type == 'document':
            r.fulfill(content_type='text/html', body=html)
        else:
            r.fulfill(json={'files': [], 'texts': [], 'profiles': [], 'waiting': False})
    page.route('**/*', route)
    if storage:
        page.add_init_script('(' + str(storage) + ')')
    page.goto('http://acl.test/')
    return context, page, errors


def _panel_w(page):
    return page.evaluate("document.getElementById('cbd-panel').getBoundingClientRect().width")


def test_three_buttons_and_label_click(browser):
    context, page, errors = _open(browser)
    try:
        assert page.locator('#cbd-preset-closed').get_attribute('aria-pressed') == 'true'
        page.click('#cbd-preset-wide')
        page.wait_for_timeout(300)
        assert _panel_w(page) > 1200
        assert page.evaluate('cbDrawer.isOpen()') is True
        page.click('#cbd-preset-normal')
        assert abs(_panel_w(page) - 480) < 2
        page.click('#cbd-preset-closed')
        assert page.evaluate('cbDrawer.isOpen()') is False
        page.click('#cbd-preset-wide')
        page.click('#cbd-handle')             # закрыть
        assert page.evaluate('cbDrawer.isOpen()') is False
        page.click('#cbd-handle')             # открыть на прежней ширине
        page.wait_for_timeout(300)
        assert _panel_w(page) > 1200
    finally:
        assert not errors, errors
        context.close()


def test_dragging_resizes_without_toggling_and_survives_reload(browser):
    context, page, errors = _open(browser)
    try:
        page.evaluate('cbDrawer.open()')
        page.wait_for_timeout(300)
        box = page.locator('#cbd-handle').bounding_box()
        x, y = box['x'] + box['width'] / 2, box['y'] + box['height'] / 2
        page.mouse.move(x, y)
        page.mouse.down()
        page.mouse.move(x - 150, y, steps=6)
        page.mouse.up()
        assert page.evaluate('cbDrawer.isOpen()') is True, 'протяжка закрыла ящик'
        assert abs(_panel_w(page) - 630) < 3
        page.reload()
        page.wait_for_timeout(300)
        assert abs(_panel_w(page) - 630) < 3, 'ширина не вернулась после перезагрузки'
    finally:
        assert not errors, errors
        context.close()


def test_stored_width_is_bounded_on_a_narrow_screen_and_garbage_is_ignored(browser):
    context, page, errors = _open(browser, width=420,
                                  storage="() => { localStorage.setItem('acl.builder.width', '1500'); localStorage.setItem('acl.builder.open', '1'); }")
    try:
        assert _panel_w(page) <= 420 - 40, 'панель закрыла язычок'
        assert page.locator('#cbd-handle').is_visible()
    finally:
        assert not errors, errors
        context.close()
    context, page, errors = _open(browser, storage="() => localStorage.setItem('acl.builder.width', 'zzz')")
    try:
        page.evaluate('cbDrawer.open()')
        assert abs(_panel_w(page) - 480) < 2
    finally:
        assert not errors, errors
        context.close()
