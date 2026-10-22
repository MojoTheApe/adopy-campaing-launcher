"""Publication controls on the real rendered builder, with offline publishing."""
import pytest
import sys
import re
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from playwright.sync_api import expect
from test_builder_multi_publish_browser import (  # noqa: F401
    browser, html, ui, add, ready, open_card, publish_one, finish, job_of,
)
from test_builder_publish_selected_browser import box, wait_posts
from test_builder_multi_publish_browser import FORM_JS


def appearance(button):
    return button.evaluate("""b => {
      const s = getComputedStyle(b), r = b.getBoundingClientRect();
      let surface = s.backgroundColor, parent = b;
      while ((surface === 'rgba(0, 0, 0, 0)' || surface === 'transparent') && parent.parentElement) {
        parent = parent.parentElement; surface = getComputedStyle(parent).backgroundColor;
      }
      return {height:r.height, font:parseFloat(s.fontSize), opacity:s.opacity,
        color:s.color, background:s.backgroundColor, cursor:s.cursor,
        surface:surface,
        outline:s.outlineStyle, outlineWidth:parseFloat(s.outlineWidth),
        fits:b.scrollWidth <= b.clientWidth + 1};
    }""")


def contrast(style):
    def luminance(color):
        channels = [float(c) for c in re.findall(r'[0-9]*\.?[0-9]+', color)[:3]]
        if not color.startswith('color('):
            channels = [c / 255 for c in channels]
        linear = [c / 12.92 if c <= .04045 else ((c + .055) / 1.055) ** 2.4 for c in channels]
        return sum(c * weight for c, weight in zip(linear, [.2126, .7152, .0722]))
    values = sorted([luminance(style['color']), luminance(style.get('surface', style['background']))])
    return (values[1] + .05) / (values[0] + .05)


@pytest.mark.parametrize('theme', ['light', 'dark'])
@pytest.mark.parametrize('width', [360, 480, 720])
def test_publication_controls_are_legible_and_focusable(ui, theme, width):
    p, state = ui
    p.evaluate("([t,w]) => {document.documentElement.dataset.theme=t; document.documentElement.style.setProperty('--cbd-w',w+'px');}", [theme, width])
    add(p)
    ready(p)
    open_card(p, 0)
    for selector in ('.cb-in-drawer .cb-publish-btn', '.cbd-publish-all'):
        b = p.locator(selector)
        expect(b).to_be_enabled()
        style = appearance(b)
        assert style['height'] >= 38
        assert style['font'] >= 14
        assert style['fits']
        assert contrast(style) >= 4.5
        if 'cb-in-drawer' in selector:
            assert style['background'] == 'rgba(0, 0, 0, 0)'
        b.focus()
        focus = appearance(b)
        assert focus['outline'] == 'solid' and focus['outlineWidth'] >= 2
    p.locator('.cb-in-drawer .cb-publish-btn').click()
    expect(p.locator('#sfc-ok')).to_be_visible()
    assert appearance(p.locator('#sfc-ok'))['height'] >= 38
    p.locator('#sfc-cancel').click()
    assert not state['posts']


def test_unavailable_and_running_controls_keep_readable_states(ui):
    p, state = ui
    add(p)
    ready(p)
    open_card(p, 0)
    # Real preview invalidation disables the individual publishing control.
    p.evaluate("cbInvalidatePreview(document.querySelector('.cb-in-drawer'))")
    b = p.locator('.cb-in-drawer .cb-publish-btn')
    expect(b).to_be_disabled()
    assert appearance(b)['opacity'] == '1'
    assert contrast(appearance(b)) >= 4.5
    assert appearance(b)['cursor'] == 'not-allowed'
    p.evaluate("cbDrawerScheduleCheck(document.querySelector('.cb-in-drawer'))")
    ready(p)
    expect(b).to_be_enabled()
    publish_one(p, state, 0)
    expect(b).to_have_attribute('aria-busy', 'true')
    # Existing guards explain duplicate clicks; bulk can still add other accounts.
    expect(b).to_be_enabled()
    expect(p.locator('.cbd-publish-all')).to_be_disabled()
    add(p, 1)
    ready(p)
    expect(p.locator('.cbd-publish-all')).to_be_enabled()
    expect(p.locator('.cbd-publish-all')).to_have_attribute('aria-busy', 'true')
    assert state['posts'][0]['activate'] == '0'
    assert state['posts'][0]['hand_off'] == '1'
    finish(p, state, 'Demo')
    expect(b).to_have_attribute('aria-busy', 'false', timeout=10000)
    expect(p.locator('.cbd-publish-all')).to_have_attribute('aria-busy', 'false')


def test_queued_and_unknown_status_have_distinct_states(ui):
    p, state = ui
    add(p)
    ready(p)
    publish_one(p, state, 0)
    state['jobs'][job_of(state, 'Demo')]['status'] = 'queued'
    expect(p.locator('.cbd-queue')).to_contain_text('queued', timeout=10000)
    b = p.locator('.cbd-publish-all')
    expect(b).to_be_disabled()  # no unlocked campaign remains in this account
    expect(b).to_have_attribute('aria-busy', 'true')
    add(p, 1)
    ready(p)
    publish_one(p, state, 1)
    del state['jobs'][job_of(state, 'Demo')]
    p.wait_for_function('CB_QUEUE.uncertain', timeout=10000)
    expect(b).to_be_disabled()
    expect(b).to_have_attribute('aria-busy', 'true')
    assert appearance(b)['opacity'] == '1'
    assert contrast(appearance(b)) >= 4.5
    assert appearance(b)['cursor'] == 'not-allowed'


def test_publication_controls_respect_reduced_motion(ui):
    p, _ = ui
    p.emulate_media(reduced_motion='reduce')
    add(p)
    ready(p)
    open_card(p, 0)
    p.locator('.cb-in-drawer .cb-publish-btn').click()
    expect(p.locator('#sfc-ok')).to_be_visible()
    for selector in ('.cb-in-drawer .cb-publish-btn', '.cbd-publish-all', '#sfc-ok', '#sfc-cancel'):
        duration = p.locator(selector).evaluate('b=>getComputedStyle(b).transitionDuration')
        # The dashboard-wide reduced-motion rule uses a negligible 0.01 ms
        # duration with !important for event compatibility.
        assert all(float(value.strip().removesuffix('s')) <= .00001 for value in duration.split(','))


def test_scope_and_all_count_exclude_running_accounts_and_match_submission(ui):
    p, state = ui
    add(p, 0, count=2)
    add(p, 1, count=1)
    ready(p)
    publish_one(p, state, 0)
    bulk = p.locator('.cbd-publish-all')
    expect(bulk).to_have_text('Publish all → drafts (1)')
    expect(bulk).to_be_enabled()
    expect(p.locator('.cbd-publication-scope')).to_contain_text('All 3 campaigns')
    expect(p.locator('.cbd-publication-scope')).to_contain_text('2 already building')
    bulk.click()
    expect(p.locator('#safe-confirm-modal')).to_contain_text('on 1 account')
    p.locator('#sfc-ok').click()
    wait_posts(state, 2, p)
    assert state['posts'][1]['account_label'] == 'Other'
    assert state['posts'][1]['camp_count'] == '1'
    expect(bulk).to_have_text('Publish all → drafts (0)')
    expect(bulk).to_be_disabled()
    expect(p.locator('.cbd-publication-reason')).to_contain_text('already being built')
    style = appearance(bulk)
    assert style['background'] != p.locator('.cbd-foot').evaluate('e=>getComputedStyle(e).backgroundColor')
    assert contrast(style) >= 4.5


def click_box_now(page, account, slot=0):
    """Click the campaign checkbox without moving focus, on the live DOM node.

    `box(...).evaluate(...)` resolves the node first and clicks it a moment later; the
    drawer's debounced repaint (`repaintSoon`, 120 ms after `ready`) can detach the node
    in between, and a click on a detached checkbox fires no `change` (ACL-2086). Looking
    the node up and clicking it inside one page task cannot be interleaved with a repaint.
    A real Playwright click is not an option here: it would move focus off the select.
    """
    clicked = page.evaluate('''([a, i]) => {
      const f = (''' + FORM_JS + ''')(a);
      const cid = f.querySelectorAll('.cb-slot')[i].dataset.cbdId;
      const b = document.querySelector('.cbd-camp[data-camp="' + cid + '"] input[type="checkbox"]');
      if (!b || !b.isConnected || b.disabled) return false;
      b.click();
      return true;
    }''', [['Demo', 'Other'][account], slot])
    assert clicked, 'campaign checkbox was not available to click'


def test_checkbox_scope_updates_without_replacing_focused_footer_select(ui):
    p, _ = ui
    add(p, 0, count=2)
    ready(p)
    select = p.locator('.cbd-pick-country')
    select.focus()
    p.evaluate('window.publishFocusedSelect = document.activeElement')
    click_box_now(p, 0)
    expect(p.locator('.cbd-publication-scope strong')).to_have_text('1 campaign selected')
    expect(p.locator('.cbd-publish-all')).to_have_text('Publish selected → drafts (1)')
    assert p.evaluate('document.activeElement === window.publishFocusedSelect && document.contains(window.publishFocusedSelect)')
    click_box_now(p, 0)
    expect(p.locator('.cbd-publish-all')).to_have_text('Publish all → drafts (2)')


def test_api_target_label_compatibility_uses_paused_without_a_write(ui):
    p, state = ui
    add(p, 0, count=2)
    ready(p)
    # Main has no API runtime yet: verify the existing newer label contract only.
    p.evaluate("window.ACL_FB_BACKEND = 'api'")
    box(p, 0, 0).click()
    expect(p.locator('.cbd-publish-all')).to_have_text('Publish selected → PAUSED campaigns (1)')
    assert not state['posts']
