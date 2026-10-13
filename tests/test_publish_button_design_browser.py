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


def appearance(button):
    return button.evaluate("""b => {
      const s = getComputedStyle(b), r = b.getBoundingClientRect();
      return {height:r.height, font:parseFloat(s.fontSize), opacity:s.opacity,
        color:s.color, background:s.backgroundColor, cursor:s.cursor,
        outline:s.outlineStyle, outlineWidth:parseFloat(s.outlineWidth),
        fits:b.scrollWidth <= b.clientWidth + 1};
    }""")


def contrast(style):
    def luminance(color):
        channels = [int(c) / 255 for c in re.findall(r'\d+', color)[:3]]
        linear = [c / 12.92 if c <= .04045 else ((c + .055) / 1.055) ** 2.4 for c in channels]
        return sum(c * weight for c, weight in zip(linear, [.2126, .7152, .0722]))
    values = sorted([luminance(style['color']), luminance(style['background'])])
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
    expect(p.locator('.cbd-publish-all')).to_be_enabled()
    add(p, 1)
    ready(p)
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
    expect(b).to_be_enabled()
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
