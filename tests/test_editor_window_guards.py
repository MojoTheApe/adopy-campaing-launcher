"""Section-boundary recovery and safe stops, using synthetic page/CDP responses."""
from unittest.mock import MagicMock

import pytest

from src.fb_ads_rpa import browser as br, meta_dom as dom, retry_policy, run_telemetry as rt


@pytest.mark.parametrize('before,after,expected,recovers', [
    ([1168, 700], [1168, 700], True, False),
    ([896, 700], [1280, 700], True, True),
    ([896, 700], [1100, 700], False, True),
    ([1920, 700], [1920, 700], True, False),
    (None, None, False, True),
    ([1280, 0], [1280, 0], False, True),
    (['1280', 700], ['1280', 700], False, True),
])
def test_guard_requires_measured_viewport(monkeypatch, before, after, expected, recovers):
    page = MagicMock()
    readings = iter([{'inner': before}, {'inner': after}])
    monkeypatch.setattr(rt, 'window_state', lambda _: next(readings))
    recovery = MagicMock()
    monkeypatch.setattr(br, '_ensure_wide_window', recovery)
    record = MagicMock()
    monkeypatch.setattr(rt, 'record', record)
    assert br.ensure_editor_window(page, 'adset') is expected
    assert recovery.call_count == int(recovers)
    event = next(c for c in record.call_args_list if c.args[0] == 'window_bounds')
    assert event.kwargs['diag']['after']['inner'] == (after if recovers else before)
    assert any(c.args[0] == 'window_too_small' for c in record.call_args_list) is not expected


def test_recovery_exception_does_not_turn_unknown_geometry_into_success(monkeypatch):
    monkeypatch.setattr(rt, 'window_state', lambda _: {})
    monkeypatch.setattr(rt, 'record', lambda *a, **kw: None)
    monkeypatch.setattr(br, '_ensure_wide_window', MagicMock(side_effect=RuntimeError('closed')))
    assert br.ensure_editor_window(MagicMock(), 'ad') is False


@pytest.mark.parametrize('stop_at,expected', [
    ('campaign', ['campaign']),
    ('adset', ['campaign', 'adset']),
    ('ad', ['campaign', 'adset', 'ad']),
])
def test_real_flow_stops_before_editing_a_narrow_section(monkeypatch, stop_at, expected):
    page = MagicMock()
    loc = MagicMock()
    loc.first = loc
    loc.bounding_box.return_value = None
    loc.input_value.return_value = 'Example'
    for name in ('get_by_role', 'get_by_text', 'get_by_placeholder'):
        getattr(page, name).return_value = loc
        getattr(loc, name).return_value = loc
    calls = []
    def guard(p, section):
        assert p is page
        calls.append(section)
        return section != stop_at
    monkeypatch.setattr(br, 'ensure_editor_window', guard)
    for name in ('_recover_from_browser_error', '_dismiss_floating_banners', '_sleep_h',
                 'wait_editor_saved', '_ensure_campaign_budget', 'scroll_to_section',
                 'click_conversion_location_dropdown', 'pick_website_conversion_option',
                 'is_website_conversion_selected', 'select_conversion_event_lead',
                 'fill_audience_age', 'fill_placements'):
        monkeypatch.setattr(dom, name, MagicMock(return_value=True))
    monkeypatch.setattr(dom, '_roots', lambda p: [p])
    monkeypatch.setattr(dom, '_first_visible', lambda *a, **kw: None)
    monkeypatch.setattr(rt, 'record', lambda *a, **kw: None)
    monkeypatch.setattr(rt, 'window_state', lambda p: {})
    from src.fb_ads_rpa import screenshots, page_capture
    monkeypatch.setattr(screenshots, 'capture', lambda *a, **kw: None)
    monkeypatch.setattr(page_capture, 'capture', lambda *a, **kw: None)
    result = dom.run_recorded_campaign_flow(
        page, campaign_name='Example', budget='10', adset_name='Set',
        creative_path='synthetic.png',
    )
    assert result == 'window_too_small'
    assert calls == expected
    requested = [c.args[0] for c in page.get_by_placeholder.call_args_list]
    forbidden = {'campaign': 'Enter your campaign name here',
                 'adset': 'Enter your ad set name here...', 'ad': 'Enter your ad name here...'}
    assert forbidden[stop_at] not in requested
    assert retry_policy.should_retry_campaign(result, completed_attempts=1) is False


def test_attach_records_measured_bounds_after_resizing(monkeypatch):
    page, context, browser, pw = (MagicMock() for _ in range(4))
    browser.contexts = [context]
    context.pages = [page]
    pw.chromium.connect_over_cdp.return_value = browser
    launcher = MagicMock()
    launcher.start.return_value = pw
    monkeypatch.setattr(br, 'sync_playwright', lambda: launcher)
    for name in ('ensure_playwright_browsers_path', '_stop_thread_playwright',
                 '_reset_asyncio_running_loop', '_disable_background_throttling',
                 '_keep_renderer_active'):
        monkeypatch.setattr(br, name, lambda *a: None)
    resized = []
    monkeypatch.setattr(br, '_ensure_wide_window', lambda c, p: resized.append((c, p)))
    def measured(p):
        assert resized == [(context, page)]
        return {'inner': [1280, 700]}
    monkeypatch.setattr(rt, 'window_state', measured)
    record = MagicMock()
    monkeypatch.setattr(rt, 'record', record)
    connection = br._connect_playwright(cdp_url='synthetic', provider='synthetic',
                                       debug_port=None, ws_endpoint=None)
    assert connection.page is page
    record.assert_called_once_with('window_bounds', None,
                                   diag={'section': 'attach', 'after': {'inner': [1280, 700]}})
    # Avoid leaving the synthetic object as the current thread's Playwright.
    br._pw_tls.playwright = None
