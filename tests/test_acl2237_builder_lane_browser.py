"""Docked Builder uses its reserved lane and keeps narrow trees readable."""
import time
import sys
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_builder_duplicate_buttons import browser
from test_web_drill import _MOCK_REPORT, _render_html
from src.web.report_store import StoredPage, decorate


@pytest.fixture(scope='module')
def layout_html():
    country = {**_MOCK_REPORT['countries'][0], 'country': 'Guatemala',
               'open_recs': {'FacebookDemoAccount': []}, 'close_recs': {}}
    html = _render_html({**_MOCK_REPORT, 'countries': [country]})
    return decorate(StoredPage(html, time.time() - 14400, ['code']))


@pytest.fixture
def layout_page(browser, layout_html):
    ctx = browser.new_context(viewport={'width': 1930, 'height': 990})
    p = ctx.new_page()
    p.route('**/*', lambda r: r.fulfill(content_type='text/html', body=layout_html)
            if r.request.resource_type == 'document' else
            r.fulfill(json={'files': [], 'texts': [], 'profiles': [], 'waiting': False}))
    yield p
    ctx.close()


def _open(p, width):
    p.add_init_script(f"if (!localStorage.getItem('acl.builder.width')) localStorage.setItem('acl.builder.width', '{width}')")
    p.goto('http://acl.test/')
    # Reserve the native scrollbar gutter also in overlay-scrollbar browsers.
    p.add_style_tag(content='.cbd-col-tree .cbd-body { scrollbar-gutter: stable; }')
    p.evaluate('''() => {
      cbDrawer.open(); const f = document.querySelector('form.open-account-form');
      for (let i=0;i<9;i++) {
        cbAddSlot(f); const s=f.querySelector('.cb-slot:last-child');
        s.querySelector('.cb-name-override').value='Guatemala_campaign_with_a_long_unbroken_name_for_layout_'+i;
        cbUpdateName(s);
        s.querySelector('[name$="_budget"]').value='120';
        cbAddAd(s.querySelector('.cb-add-btn-sm'));
      }
      cbTree.paint();
    }''')
    p.locator('.cbd-camp input[type=checkbox]').first.check()
    p.wait_for_timeout(250)


def test_docked_panel_ignores_stored_banner(layout_page):
    # ACL-2260: the banner is out of the flow (fixed, bottom-left, under the drawer).
    p=layout_page
    _open(p, 370)
    panel=p.locator('.cbd-panel').bounding_box()
    bar=p.locator('#acl-stored-bar').bounding_box()
    print({'panel': panel, 'banner': bar})
    assert panel['y'] == 0
    assert bar['y'] > panel['y'] + panel['height'] / 2


@pytest.mark.parametrize('width', [320, 370, 480])
def test_narrow_campaign_tree_has_no_horizontal_scroll(layout_page, width):
    p=layout_page
    _open(p, width)
    dimensions=p.locator('.cbd-col-tree .cbd-body').evaluate(
        'e => ({scroll:e.scrollWidth, client:e.clientWidth})')
    print(width, dimensions)
    assert dimensions['scroll'] <= dimensions['client'] + 1
    assert p.locator('.cbd-camp input[type=checkbox]').first.is_checked()
    assert p.locator('.cbd-camp-name').first.inner_text().startswith('Guatemala_campaign_with_a_long_unbroken_name')
    assert '2 ads' in p.locator('.cbd-camp-sub').first.inner_text()
    assert '$120/day' in p.locator('.cbd-camp-meta').first.inner_text()
    metadata=p.locator('.cbd-camp-sub').first.evaluate('''e => {
      const parent=e.closest('.cbd-camp-row').getBoundingClientRect();
      return [...e.children].map(x => {
        const r=x.getBoundingClientRect();return {right:r.right,parentRight:parent.right};
      });
    }''')
    assert all(m['right'] <= m['parentRight'] + 1 for m in metadata), metadata


def test_reload_keeps_drafts_width_and_lane_switches(layout_page):
    p=layout_page
    _open(p, 370)
    p.evaluate('''() => {
      document.querySelector('[name="camp_0_budget"]').value='77';
      cbKeep.save();
    }''')
    p.reload()
    p.evaluate('cbDrawer.open()')
    assert p.locator('.cbd-camp').count() == 9
    assert p.locator('[name="camp_0_budget"]').first.input_value() == '77'
    assert abs(p.locator('.cbd-panel').bounding_box()['width'] - 370) <= 1
    p.set_viewport_size({'width': 390, 'height': 990})
    p.wait_for_timeout(250)
    header=p.locator('header.page-header').bounding_box()
    assert abs(p.locator('.cbd-panel').bounding_box()['y'] - header['y'] - header['height']) <= 1
    p.set_viewport_size({'width': 1930, 'height': 990})
    p.wait_for_timeout(250)
    assert p.locator('.cbd-panel').bounding_box()['y'] == 0
    p.evaluate("document.getElementById('acl-stored-bar').remove(); dispatchEvent(new Event('resize'))")
    p.wait_for_timeout(250)
    assert p.locator('.cbd-panel').bounding_box()['y'] == 0
