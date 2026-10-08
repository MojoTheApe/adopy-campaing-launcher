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
    # ACL-1727: узкое окно после восстановления ещё раз увеличивается и измеряется
    readings = iter([{'inner': before}, {'inner': after}, {'inner': after}])
    monkeypatch.setattr(br, '_enlarge_small_window', MagicMock(return_value=None))
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


def test_a_minimized_window_is_restored_even_if_it_reports_a_wide_page(monkeypatch):
    """19.09: свёрнутое окно сообщало 1707 px, и проверка его не разворачивала."""
    readings = iter([{'inner': [1707, 887], 'minimized': True}, {'inner': [1707, 887], 'minimized': False}])
    monkeypatch.setattr(rt, 'window_state', lambda _: next(readings))
    recovery = MagicMock()
    monkeypatch.setattr(br, '_ensure_wide_window', recovery)
    monkeypatch.setattr(rt, 'record', lambda *a, **kw: None)
    assert br.ensure_editor_window(MagicMock(), 'adset') is True
    assert recovery.call_count == 1


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
    record.assert_called_once()
    args, kwargs = record.call_args
    assert args == ('window_bounds', None)
    assert kwargs['diag']['section'] == 'attach'
    assert kwargs['diag']['after'] == {'inner': [1280, 700]}
    # ACL-1402: видимость вкладки и окна профиля — в той же строке.
    assert 'hidden' in kwargs['diag']['visibility']
    # Avoid leaving the synthetic object as the current thread's Playwright.
    br._pw_tls.playwright = None


def test_a_minimized_window_is_moved_offscreen_and_put_back(monkeypatch):
    """19.09: окна держат свёрнутыми — робот выводит окно за край экрана, а в
    конце кампании возвращает на место и сворачивает."""
    calls = []

    class Session:
        def __init__(self): self.state = "minimized"
        def send(self, method, params=None):
            calls.append((method, params))
            if method == "Browser.getWindowForTarget":
                return {"windowId": 7, "bounds": {"windowState": self.state, "left": 100, "top": 50,
                                                  "width": 1400, "height": 900}}
            if method == "Browser.setWindowBounds" and "windowState" in params["bounds"]:
                self.state = params["bounds"]["windowState"]
            return {}

    session = Session()
    page = MagicMock()
    page.context.new_cdp_session.return_value = session
    monkeypatch.setattr(br.time, "sleep", lambda *_: None)
    readings = iter([{'inner': [1707, 887], 'minimized': True}, {'inner': [1707, 887], 'minimized': False}])
    monkeypatch.setattr(rt, 'window_state', lambda _: next(readings))
    monkeypatch.setattr(rt, 'record', lambda *a, **kw: None)
    wide = MagicMock()
    monkeypatch.setattr(br, '_ensure_wide_window', wide)
    assert br.ensure_editor_window(page, 'campaign') is True
    wide.assert_not_called()
    moved = [p["bounds"] for m, p in calls if m == "Browser.setWindowBounds" and "left" in p["bounds"]]
    assert moved and moved[0]["left"] < 0, calls
    br.put_window_back(page)
    tail = [p["bounds"] for m, p in calls if m == "Browser.setWindowBounds"][-2:]
    assert tail == [{"left": 100, "top": 50}, {"windowState": "minimized"}]


def test_the_window_can_be_parked_on_a_chosen_screen(monkeypatch):
    """20.09: свёрнутое окно больше не тормозит прогон, поэтому место окна —
    вопрос удобства. FB_RPA_WINDOW_LEFT/TOP ставят его, например, на второй
    монитор, и в конце кампании оно там и остаётся."""
    from src.fb_ads_rpa import browser

    monkeypatch.setenv("FB_RPA_WINDOW_LEFT", "1920")
    monkeypatch.setenv("FB_RPA_WINDOW_TOP", "0")
    assert browser.window_home() == (1920, 0)

    monkeypatch.delenv("FB_RPA_WINDOW_LEFT", raising=False)
    assert browser.window_home() == (None, None)

    monkeypatch.setenv("FB_RPA_WINDOW_LEFT", "не число")
    assert browser.window_home() == (None, None)


class _FakeCdp:
    """Окно в CDP: maximise / новая ширина дают ширину страницы `after_inner`."""

    def __init__(self, state='normal', after_inner=1700, left=100, top=50, width=854):
        self.state, self.after_inner, self.calls = state, after_inner, []
        self.left, self.top, self.width = left, top, width
        self.inner = 1117
        self.detached = False

    def send(self, method, params=None):
        if method == 'Browser.getWindowForTarget':
            return {'windowId': 7, 'bounds': {'windowState': self.state, 'width': self.width,
                                              'height': 502, 'left': self.left, 'top': self.top}}
        self.calls.append((method, params))
        b = (params or {}).get('bounds') or {}
        if b.get('windowState'):
            self.state = b['windowState']
        for k in ('left', 'top', 'width'):
            if k in b:
                setattr(self, k, b[k])
        if b.get('windowState') == 'maximized' or b.get('width', 0) > 900:
            self.inner = self.after_inner
        return {}

    def detach(self):
        self.detached = True


def _enlarge_case(monkeypatch, cdp, offscreen=False, dpr_after=0.75):
    page = MagicMock()
    page.context.new_cdp_session.return_value = cdp
    dprs = iter([0.75, dpr_after, dpr_after, dpr_after])
    page.evaluate.side_effect = lambda *_a: [next(dprs), cdp.inner]
    monkeypatch.setattr(br.time, 'sleep', lambda *_: None)
    monkeypatch.setattr(br, '_ensure_wide_window', MagicMock())
    monkeypatch.setattr(br, '_restore_offscreen', MagicMock(return_value=True))
    monkeypatch.setattr(br, '_OFFSCREEN', {id(page.context): {}} if offscreen else {})
    monkeypatch.setattr(br, '_SCALE_REVERTED', set())
    monkeypatch.setattr(br, '_PARK_SPOTS', set())
    monkeypatch.setattr(rt, 'window_state', lambda _: {'inner': [cdp.inner, 542], 'minimized': False})
    record = MagicMock()
    monkeypatch.setattr(rt, 'record', record)
    return page, record


def test_a_restored_small_window_is_enlarged_and_the_run_continues(monkeypatch):
    cdp = _FakeCdp(after_inner=1700)
    page, record = _enlarge_case(monkeypatch, cdp)
    assert br.ensure_editor_window(page, 'ad') is True
    assert ('Browser.setWindowBounds', {'windowId': 7, 'bounds': {'windowState': 'maximized'}}) in cdp.calls
    event = next(c for c in record.call_args_list if c.args[0] == 'window_bounds')
    assert event.args[1] is True
    assert event.kwargs['diag']['enlarged'] is True
    assert event.kwargs['diag']['enlarged_to']['state'] == 'maximized'
    assert not any(c.args[0] == 'window_too_small' for c in record.call_args_list)
    assert cdp.detached


def test_window_still_small_after_enlarging_stops_with_window_too_small(monkeypatch):
    cdp = _FakeCdp(after_inner=1000)
    page, record = _enlarge_case(monkeypatch, cdp)
    assert br.ensure_editor_window(page, 'ad') is False
    assert any(c.args[0] == 'window_too_small' for c in record.call_args_list)


def test_a_window_in_offscreen_registry_but_on_screen_is_maximised(monkeypatch):
    """4db2039f760d: контекст в `_OFFSCREEN`, а окно вернулось на экран."""
    cdp = _FakeCdp(after_inner=1700, left=100, top=50)
    page, _ = _enlarge_case(monkeypatch, cdp, offscreen=True)
    assert br.ensure_editor_window(page, 'ad') is True
    assert [p['bounds'] for _m, p in cdp.calls] == [{'windowState': 'maximized'}]


def test_a_parked_window_grows_proportionally_and_stays_off_screen(monkeypatch):
    cdp = _FakeCdp(after_inner=1700, left=-(854 + 400), top=0)
    page, _ = _enlarge_case(monkeypatch, cdp, offscreen=True)
    assert br.ensure_editor_window(page, 'ad') is True
    bounds = [p['bounds'] for _m, p in cdp.calls]
    assert all(b.get('windowState') != 'maximized' for b in bounds)
    grown = bounds[-1]
    assert grown['width'] == int(854 * br.MIN_EDITOR_WIDTH / 1117) + 40
    assert grown['left'] + grown['width'] <= 0


@pytest.mark.parametrize('state', ['maximized', 'fullscreen'])
def test_an_already_maximised_small_window_is_not_toggled(monkeypatch, state):
    cdp = _FakeCdp(state=state, after_inner=1700)
    page, record = _enlarge_case(monkeypatch, cdp)
    assert br.ensure_editor_window(page, 'ad') is False
    assert cdp.calls == []
    assert any(c.args[0] == 'window_too_small' for c in record.call_args_list)


def test_a_mimic_scale_drop_is_undone_and_not_repeated(monkeypatch):
    cdp = _FakeCdp(after_inner=1700)
    page, record = _enlarge_case(monkeypatch, cdp, dpr_after=0.4)
    assert br.ensure_editor_window(page, 'ad') is False
    states = [p['bounds'].get('windowState') for _m, p in cdp.calls]
    assert states[:2] == ['maximized', 'normal']
    assert id(page.context) in br._SCALE_REVERTED
    cdp.calls.clear()
    assert br._enlarge_small_window(page) is None
    assert cdp.calls == []


def test_a_wide_enough_window_is_not_touched(monkeypatch):
    cdp = _FakeCdp()
    cdp.inner = 1500
    page, _ = _enlarge_case(monkeypatch, cdp)
    assert br.ensure_editor_window(page, 'ad') is True
    assert cdp.calls == []
