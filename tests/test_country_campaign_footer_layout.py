"""Real Chromium geometry on sanitized pending campaign-component markup.

The fixture records its preview provenance; main's actual report CSS is applied
before component CSS, matching the preview cascade. No live services are used.
"""
import os
import re
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        instance = pw.chromium.launch(executable_path=os.environ.get("ACL_BROWSER_EXECUTABLE") or None)
        yield instance
        instance.close()


def document(theme, baseline=False):
    css = re.search(r"<style>(.*?)</style>", (ROOT / "src/web/templates/report.html").read_text(encoding="utf-8"), re.S)[1]
    if baseline:
        css = re.sub(r"\.amap-country > \.hc(?:\.hc-after|-after)[^{]*\{[^}]*\}", "", css)
    fixture = (ROOT / "tests/fixtures/country_campaign_footer.html").read_text(encoding="utf-8")
    return f'<html data-theme="{theme}"><head><style>{css}</style></head><body>{fixture}</body></html>'


def boxes(page):
    return page.evaluate("""() => Object.fromEntries(
      ['.amap-country', 'table', '.hc-after', '.hc-foot', '.hc-hist > summary', '.hc-hist-body', '.amap-empty', '.amap-add']
        .map(s => { const r=document.querySelector(s).getBoundingClientRect();
          return [s, {x:r.x, right:r.right, y:r.y, bottom:r.bottom}]; }))""")


@pytest.mark.parametrize("width", [1280, 1024, 320])
@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("inventory", ["empty", "populated", "filtered"])
def test_footer_inset_wrap_spacing_and_keyboard(browser, width, theme, inventory):
    page = browser.new_page(viewport={"width": width, "height": 900})
    try:
        page.set_content(document(theme))
        page.evaluate("""inventory => {
          document.querySelector('.hc-foot span').textContent = inventory === 'empty'
            ? 'Showing 0 campaigns. Reach and frequency are read per level from Meta and never summed up the tree.'
            : inventory === 'filtered' ? 'Showing 2 of 16 campaigns for the selected filter.' : 'Showing 16 campaigns.';
          document.querySelector('.hc-hist-body').innerHTML = '<div class="hc-hist-line">Synthetic account: ' + 'long-history-entry'.repeat(40) + '</div>';
        }""", inventory)
        summary = page.locator('.hc-hist > summary')
        summary.focus()
        page.keyboard.press('Enter')
        assert page.locator('.hc-hist').get_attribute('open') is not None
        positions = boxes(page)
        anchor = positions['.amap-add']['x'] + 13.6  # real .85rem connection-form padding
        for selector in ['.hc-foot', '.hc-hist > summary', '.hc-hist-body']:
            assert positions[selector]['x'] == pytest.approx(anchor, abs=0.5)
            assert positions[selector]['right'] <= positions['.amap-country']['right'] - 8
        assert positions['.hc-foot']['y'] - positions['table']['bottom'] >= 8
        assert positions['.amap-empty']['y'] - positions['.hc-hist-body']['bottom'] >= 8
        assert page.evaluate("document.querySelector('.hc-after').scrollWidth <= document.querySelector('.hc-after').clientWidth")
        outline = summary.evaluate("e => ({width:parseFloat(getComputedStyle(e).outlineWidth), offset:parseFloat(getComputedStyle(e).outlineOffset)})")
        assert outline['width'] == 2
        assert positions['.hc-hist > summary']['x'] - outline['width'] - outline['offset'] > positions['.amap-country']['x']
        page.keyboard.press('Space')
        assert page.locator('.hc-hist').get_attribute('open') is None
    finally:
        page.close()


def test_original_preview_reproduces_missing_inset(browser):
    page = browser.new_page()
    try:
        page.set_content(document('light', baseline=True))
        positions = boxes(page)
        assert positions['.hc-foot']['x'] < positions['.amap-add']['x'] + 2
    finally:
        page.close()
