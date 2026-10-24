"""Real report/UI comparison with synthetic transport; no account service calls."""
from __future__ import annotations

import mimetypes
import os
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import expect, sync_playwright

from src.web.period_compare import PeriodCompare
from src.web import app as web
from tests.test_acl110_missing_rows import INFO, fixtures, metrics, render, workbench_fixture

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope='module')
def browser():
    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=os.environ.get('ACL_BROWSER_EXECUTABLE') or None)
        yield b
        b.close()


@pytest.mark.parametrize('theme', ['light', 'dark'])
@pytest.mark.parametrize('width', [390, 1280])
def test_comparison_everywhere_is_readonly_and_both_modes_keep_primary_dates(browser, theme, width):
    report, amap, base_compare = fixtures()
    wb_report, _, wb_compare, _ = workbench_fixture()
    report['my_cm'] = wb_report['my_cm']
    report['countries'][0]['workbench'] = wb_report['countries'][0]['workbench']
    prior = base_compare._report
    prior['countries'][0]['workbench'] = wb_compare._report['countries'][0]['workbench']
    pack = report['countries'][0]['workbench']['slices']['Alpha']['creatives'][0]
    new_raw = pack['pack'] + ' 99 - new comparison version'
    pack['versions'].append({**pack['versions'][0], 'key': pack['pack'] + '|99', 'version': '99', 'raw': new_raw})
    prior_pack = prior['countries'][0]['workbench']['slices']['Alpha']['creatives'][0]
    prior_pack['versions'].append({**prior_pack['versions'][0], 'key': pack['pack'] + '|77', 'version': '77',
                                  'raw': 'Retired exact comparison version', 'you': {'spend': 10, 'leads': 1, 'ftds': 1}})
    old_variant = metrics(10, variant='1', label='Retained static version')
    new_variant = metrics(10, variant='2', label='New static version')
    creative = metrics(20, creative_id='42', ad_title='42', text_name='Text', has_variants=True,
                       variants=[old_variant, new_variant])
    report['countries'][0]['creatives'] = [creative]
    prior['countries'][0]['creatives'] = [{**creative, 'variants': [old_variant]}]
    amap['cm_section'].append({'country': 'New country', **metrics(0), 'rows': [metrics(0, cm='Zero manager')]})
    zero_own = {'target': 'Zero own FTDs', 'spend': 20, 'leads': 2, 'ftds': 1,
                'you': {'spend': 10, 'leads': 1, 'ftds': 0}}
    prior['countries'][0]['workbench']['slices']['Alpha']['targets'].append(zero_own)
    report['countries'][0]['workbench']['slices']['Alpha']['targets'].append(
        {**zero_own, 'you': {'spend': 0, 'leads': 0, 'ftds': 0}})
    # A whole earlier-only target is evidence, never a current Use index.
    old_target = dict(prior['countries'][0]['workbench']['slices']['Alpha']['targets'][0], target='Retired <target>')
    prior['countries'][0]['workbench']['slices']['Alpha']['targets'].append(old_target)
    ctx = browser.new_context(viewport={'width': width, 'height': 960}, color_scheme=theme)
    ctx.set_default_timeout(10000)
    ctx.set_default_navigation_timeout(15000)
    p = ctx.new_page()
    errors, requests, map_queries = [], [], []
    p.on('pageerror', lambda err: errors.append(str(err)))

    def route(r):
        req = r.request
        u = urlparse(req.url)
        requests.append((req.method, u.hostname, u.path))
        # Only ordinary page opening admits the local coordinator. This fixture
        # never starts a profile worker; comparison/date navigation stays view-only.
        if u.hostname == 'acl.test' and u.path == '/account-refresh/page-open':
            assert req.method == 'POST'
            return r.fulfill(json={'ok': True, 'sweep': 'disabled', 'queued': 0})
        if u.hostname == 'acl.test' and u.path == '/account-refresh/state':
            assert req.method == 'GET'
            return r.fulfill(json={'ok': True, 'rows': {}, 'accounts': {}, 'queued': []})
        assert req.method == 'GET', (req.method, u.path)
        if req.resource_type == 'document' or u.path == '/account-map':
            query = parse_qs(u.query)
            mode = query.get('compare', [''])[0]
            info = {**INFO, 'mode': mode}
            if mode == 'custom':
                info.update(date_from=query['cmp_from'][0], date_to=query.get('cmp_to', query['cmp_from'])[0])
            compare = PeriodCompare(info, prior, {'countries': [
                {'country': 'Costa Rica', 'book': {'spend': 10, 'leads': 2, 'deposits': 1}, 'rows': []},
                {'country': 'Mexico', 'book': {'spend': 50, 'leads': 2, 'deposits': 1}, 'rows': []}],
                'cm_section': [{'country': 'Costa Rica', 'spend': 20, 'leads': 4, 'deposits': 2, 'rows': prior['countries'][0]['cm_comparison']}]}) if mode else None
            if u.path == '/account-map':
                map_queries.append(query)
                body = web.templates.env.get_template('_account_map.html').render(amap=amap, compare=compare, mlx={})
                return r.fulfill(content_type='text/html', body=body)
            return r.fulfill(content_type='text/html', body=render(report, amap, compare))
        if u.path.startswith('/static/'):
            file = (ROOT / 'src/web' / u.path.lstrip('/')).resolve()
            if file.is_relative_to(ROOT / 'src/web/static') and file.is_file():
                return r.fulfill(body=file.read_bytes(), content_type=mimetypes.guess_type(file.name)[0] or 'application/octet-stream')
        if req.resource_type == 'script':
            return r.fulfill(content_type='text/javascript', body='')
        return r.fulfill(json={'files': [], 'texts': [], 'rows': [], 'profiles': [], 'waiting': False})

    p.route('**/*', route)
    try:
        p.goto('http://acl.test/?date_from=2026-10-08&date_to=2026-10-14&compare=prev#home')
        expect(p.locator('#home-kpis .cmp-prev').first).to_be_visible()
        assert p.evaluate("""() => { const period = document.querySelector('.period-pick').getBoundingClientRect(); const chip = document.querySelector('.cmp-chip').getBoundingClientRect(); const button = document.querySelector('.period-btn').getBoundingClientRect(); return chip.left >= period.left - 1 && chip.right <= period.right + 1 && chip.top >= button.bottom - 1; }""")
        ghost = p.locator('#view-home .cmp-only-country').filter(has_text='Mexico').first
        ghost.locator('summary').click()
        expect(ghost.locator('.cmp-flag.gone').first).to_be_visible()
        expect(ghost.locator('button, input, select, form, [onclick], [hx-get]')).to_have_count(0)
        # Compare annotations must compose with the responsive account-map main.
        # Wide data stays inside its scroller rather than panning the whole pane.
        bounds = p.evaluate("""() => ({page: document.documentElement.scrollWidth,
          viewport: innerWidth, mainPan: document.querySelector('.main-content').scrollLeft})""")
        assert bounds['page'] <= bounds['viewport'] + 1, bounds
        assert abs(bounds['mainPan']) <= 1, bounds
        head = p.locator('#view-home .amap-head')
        assert head.evaluate('e => e.scrollWidth <= e.clientWidth + 1')
        outer = head.bounding_box()
        for control in head.locator('button,input,select').all():
            if control.is_visible():
                rect = control.bounding_box()
                assert outer['x'] - 1 <= rect['x'] and rect['x'] + rect['width'] <= outer['x'] + outer['width'] + 1
        expect(p.locator('#amap-cm').get_by_text('Bob', exact=False)).to_have_count(1)
        p.locator('#amap-cm > summary').click()
        expect(p.locator('#amap-cm [data-country="__cm__:New country"] > summary .cmp-flag.new')).to_be_visible()
        p.evaluate('() => amapRefreshCountryLines(amapPeriod())')
        assert map_queries[-1]['compare'] == ['prev']
        assert map_queries[-1]['date_from'] == ['2026-10-08']
        shots = os.environ.get('ACL_COMPARE_SCREENSHOTS')
        if shots:
            Path(shots).mkdir(parents=True, exist_ok=True)
            p.screenshot(path=str(Path(shots)/f'home-{theme}-{width}.png'))
        p.goto('http://acl.test/?date_from=2026-10-08&date_to=2026-10-14&compare=prev#country-costa-rica-workbench')
        panel = p.locator('#view-country-costa-rica-workbench')
        expect(panel).to_be_visible()
        expect(panel.locator('[data-cw="kpis"] .cmp-prev').first).to_be_visible()
        panel.locator('[data-cw-asset="Alpha"]').click()
        asset_counts = panel.locator('[data-cw-asset="Alpha"] [aria-label="Earlier FTDs and change"]')
        expect(asset_counts).to_contain_text('You 4')
        expect(asset_counts).to_contain_text('All 19')
        if width == 1280:
            sidebar_counts = p.locator('.sb-asset[data-asset="Alpha"] [aria-label="Earlier FTDs and change"]')
            expect(sidebar_counts).to_contain_text('You 4')
            expect(sidebar_counts).to_contain_text('All 19')
            assert sidebar_counts.evaluate('e => e.getBoundingClientRect().right <= document.querySelector(".sidebar").getBoundingClientRect().right + 1')
            assert p.locator('.sb-asset[data-asset="Alpha"] .sb-asset-name').bounding_box()['width'] > 60
        volume = panel.locator('[data-kpi="ftds"]')
        earlier_money = volume.locator('.cmp-prev').all_text_contents()
        assert earlier_money.count('$600') == 2 and '$999' in earlier_money, earlier_money
        for tab in ('campaigns', 'setups', 'landings', 'targets', 'creatives', 'buyers'):
            panel.locator('[data-cw-tab="'+tab+'"]').click()
            expect(panel.locator('.cw-table:not(.cmp-readonly) .cmp-prev').first).to_be_visible()
            if tab != 'buyers':
                expect(panel.locator('.cw-table:not(.cmp-readonly) .cw-you .cmp-prev').first).to_be_visible()
        panel.locator('[data-cw-tab="creatives"]').click()
        panel.locator('.cw-pack').filter(has_text='Pack ' + pack['pack']).locator('.cw-toggle').click()
        expect(panel.locator('.cw-sub').filter(has_text=new_raw).locator('.cmp-flag.new')).to_be_visible()
        gone_version = panel.locator('.cmp-gone-row').filter(has_text='Retired exact comparison version')
        expect(gone_version.locator('.cw-you .cmp-prev')).to_have_text('1')
        expect(gone_version.locator('button,[data-cw-use]')).to_have_count(0)
        panel.locator('[data-cw-tab="targets"]').click()
        expect(panel.locator('.cw-table:not(.cmp-readonly) tr').filter(has_text='Zero own FTDs').locator('.cw-you .cmp-prev')).to_have_text('0')
        retired = panel.locator('.cmp-readonly').filter(has_text='Retired <target>')
        expect(retired).to_be_visible()
        expect(retired.locator('button, [data-cw-use], [onclick], form')).to_have_count(0)
        expect(panel.locator('[data-cw="chips"]').get_by_text('Beta', exact=False)).to_have_count(1)
        assert panel.locator('.cmp-readonly').evaluate_all('(els)=>els.every(e=>!e.querySelector("[data-cw-use]"))')
        shots = os.environ.get('ACL_COMPARE_SCREENSHOTS')
        if shots:
            Path(shots).mkdir(parents=True, exist_ok=True)
            p.screenshot(path=str(Path(shots)/f'compare-{theme}-{width}.png'))
        p.goto('http://acl.test/?date_from=2026-10-08&date_to=2026-10-14&compare=prev#country-costa-rica-creatives')
        static = p.locator('#view-country-costa-rica-creatives')
        static.locator('.creative-group').click()
        expect(static.locator('.creative-variant').filter(has_text='New static version').locator('.cmp-flag.new')).to_be_visible()
        # Use the real date picker. Navigation is caught by the local-only route.
        p.locator('#period-btn').click()
        p.locator('[data-cmp-mode]').select_option('custom')
        p.locator('[data-cmp-from]').fill('2026-09-01')
        p.locator('[data-cmp-to]').fill('2026-09-07')
        p.locator('[data-apply]').click()
        p.wait_for_url(lambda url: parse_qs(urlparse(url).query).get('compare') == ['custom'])
        query = parse_qs(urlparse(p.url).query)
        assert query['date_from'] == ['2026-10-08'] and query['date_to'] == ['2026-10-14']
        assert query['cmp_from'] == ['2026-09-01'] and query['cmp_to'] == ['2026-09-07']
        p.evaluate('() => amapRefreshCountryLines(amapPeriod())')
        assert map_queries[-1]['compare'] == ['custom'] and map_queries[-1]['cmp_from'] == ['2026-09-01']
        p.locator('#period-btn').click()
        p.locator('[data-cmp-mode]').select_option('prev')
        p.locator('[data-apply]').click()
        p.wait_for_url(lambda url: parse_qs(urlparse(url).query).get('compare') == ['prev'])
        assert 'cmp_from' not in parse_qs(urlparse(p.url).query)
        p.locator('#period-btn').click()
        p.locator('[data-cmp-mode]').select_option('')
        p.locator('[data-apply]').click()
        p.wait_for_url(lambda url: 'compare=' not in url)
        expect(p.locator('small.cmp, .cmp-flag, .cmp-only-country')).to_have_count(0)
        assert p.locator('html').get_attribute('data-theme') == theme
        assert not errors, errors
        assert all(host in ('acl.test', 'unpkg.com') for _, host, _ in requests)
        assert all(method == 'GET' or (method, host, path) ==
                   ('POST', 'acl.test', '/account-refresh/page-open')
                   for method, host, path in requests)
    finally:
        ctx.close()
