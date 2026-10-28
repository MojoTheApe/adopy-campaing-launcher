"""ACL-2260: the OUTDATED banner does not push or break the Builder.

The same page is rendered with and without the stored-report bar; every Builder
rectangle and the page header must be identical, at docked, overlay and phone widths.
"""
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_builder_duplicate_buttons import browser  # noqa: F401 - fixture
from test_web_drill import _MOCK_REPORT, _render_html
from src.web.report_store import StoredPage, decorate

SIZES = [(1930, 990), (1100, 800), (390, 800)]
PROBE = '''() => {
  const box = (s) => { const e = document.querySelector(s); if (!e) return null;
    const r = e.getBoundingClientRect(); return [r.x, r.y, r.width, r.height].map(Math.round); };
  return {header: box('header.page-header'), panel: box('.cbd-panel'), tab: box('.cbd-tab'),
          root: box('#cbd-root'), tree: box('.cbd-col-tree'), scroll: document.documentElement.scrollHeight,
          top: getComputedStyle(document.getElementById('cbd-root')).getPropertyValue('--cbd-top') || ''};
}'''


@pytest.fixture(scope='module')
def pages():
    country = {**_MOCK_REPORT['countries'][0], 'country': 'Guatemala',
               'open_recs': {'FacebookDemoAccount': []}, 'close_recs': {}}
    plain = _render_html({**_MOCK_REPORT, 'countries': [country]})
    return {
        'plain': plain,
        'outdated': decorate(StoredPage(plain, time.time() - 14400, ['code', 'source'])),
        'cached': decorate(StoredPage(plain, time.time() - 300)),
        'deferred': decorate(StoredPage(plain, time.time() - 14400, ['code']), deferred=True),
    }


def measure(browser, html, size):
    ctx = browser.new_context(viewport={'width': size[0], 'height': size[1]})
    p = ctx.new_page()
    p.route('**/*', lambda r: r.fulfill(content_type='text/html', body=html)
            if r.request.resource_type == 'document' else
            r.fulfill(json={'files': [], 'texts': [], 'profiles': [], 'waiting': False,
                            'ready': False, 'deferred': True}))
    p.goto('http://acl.test/')
    p.evaluate('cbDrawer.open()')
    p.wait_for_timeout(400)
    data = p.evaluate(PROBE)
    bar = p.evaluate('''() => { const b = document.getElementById('acl-stored-bar');
      if (!b) return null; const r = b.getBoundingClientRect();
      return {position: getComputedStyle(b).position, bottom: Math.round(innerHeight - r.bottom),
              visible: r.width > 0 && r.height > 0}; }''')
    ctx.close()
    return data, bar


@pytest.mark.parametrize('size', SIZES)
@pytest.mark.parametrize('state', ['outdated', 'cached', 'deferred'])
def test_builder_and_header_are_identical_with_the_banner(browser, pages, size, state):
    without, no_bar = measure(browser, pages['plain'], size)
    with_bar, bar = measure(browser, pages[state], size)
    assert no_bar is None
    assert bar['position'] == 'fixed' and bar['visible'], bar
    assert with_bar == without, {'with': with_bar, 'without': without}
    assert with_bar['panel'] is not None and with_bar['panel'][3] > 0


def test_banner_stays_under_the_open_builder(browser, pages):
    ctx = browser.new_context(viewport={'width': 1930, 'height': 990})
    p = ctx.new_page()
    p.route('**/*', lambda r: r.fulfill(content_type='text/html', body=pages['outdated'])
            if r.request.resource_type == 'document' else r.fulfill(json={}))
    p.goto('http://acl.test/')
    z = p.evaluate('''() => [getComputedStyle(document.getElementById('acl-stored-bar')).zIndex,
                              getComputedStyle(document.getElementById('cbd-root')).zIndex].map(Number)''')
    ctx.close()
    assert z[0] < z[1], z
