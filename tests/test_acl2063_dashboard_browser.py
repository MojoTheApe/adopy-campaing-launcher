"""Production component + coordinator JS + actual HTTP routes, offline readers.

No profile, MLX or Meta is opened. Dates, state, retries and BI joins use the
real app paths; synthetic profile results are explicit fixture boundaries.
"""
from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace
import threading
from urllib.parse import urlparse, parse_qs

import pytest
from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright
from src.analysis_agent import account_refresh as ar, campaign_inventory as ci
from src.analysis_agent.account_refresh_state import RefreshStore
from tests._retry_once import goto, launch_chromium
from tests.test_acl1798_campaign_inventory import snapshot

ROOT = Path(__file__).resolve().parents[1]
REPORT = (ROOT / 'src/web/templates/report.html').read_text()
MOUNT_JS = REPORT[REPORT.index('function amapCampaignSlug('):REPORT.index('function amapPollSpend(')]
AMAP_CSS = REPORT[REPORT.index('    .amap table { width: 100%'):REPORT.index('    .amap-caret-off {')]

def open_acc(page, account):
    page.click(f'tr:has(td[data-account="{account}"]) td.amap-acc', position={'x': 8, 'y': 8})
    page.wait_for_selector(f'tr:has(td[data-account="{account}"]) + tr.hc-acct-child:not([hidden])')

APPLY_JS = REPORT[REPORT.index('function amapApply('):REPORT.index('/* ── Спенд прямо с аккаунта')]


class Dashboard:
    def __init__(self, web, monkeypatch, tmp_path):
        self.web = web
        monkeypatch.setattr(web, '_inventory_goal_context', lambda countries: {'goal_cpa': None, 'benchmark_cpa': None})
        self.reads, self.requests, self.errors = [], [], []
        self.accounts = ['Acc', 'Fresh']
        self.command_world = None
        self.warm_available = False
        self.fail = set()
        self.entered, self.release = threading.Event(), threading.Event()
        self.hold = False
        self.now = datetime(2026, 10, 24, 8).astimezone().timestamp()
        monkeypatch.setenv('ACL_AUTO_REFRESH', '1')
        monkeypatch.setenv('ACL_FB_BACKEND', 'api')
        monkeypatch.setattr(ar, 'dashboard_accounts', lambda: list(self.accounts))
        monkeypatch.setattr(ci, '_store_path', lambda: tmp_path / 'inventory.json')
        monkeypatch.setattr(web, '_mlx_batch_refusal', lambda *a: None)
        class Time(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls.fromtimestamp(self.now, tz).astimezone()
        monkeypatch.setattr(ar, 'datetime', Time)
        self.q = ar.RefreshQueue(self.read, clock=lambda: self.now,
                                 store=RefreshStore(tmp_path), accounts=ar.dashboard_accounts)
        monkeypatch.setattr(ar, '_QUEUE', self.q)
        self.client = TestClient(web.app, base_url='http://127.0.0.1:9095')
        for account in self.accounts:
            self.save(account, age=100)
        monkeypatch.setattr(web, '_inventory_bi', lambda a, c, f, t, s: (
            {'campaigns': {'Camp B': {'spend': float(int(f[-2:])) if f else 999,
                                     'leads': 10, 'deposits': 2}}, 'ads': {}}, ''))

    def save(self, account, age=0):
        snap = snapshot()
        snap.account = account
        snap.adsets = [a for a in snap.adsets if a['campaign_id'] != '2']
        snap.ads = [a for a in snap.ads if a['campaign_id'] != '2']
        if self.command_world is not None:
            objects = self.command_world.meta.objs.values()
            for edge, kind in [('campaigns', 'campaign'), ('adsets', 'adset'), ('ads', 'ad')]:
                parsed, _ = ci.parse_inventory([self.command_world.meta._row(o, '') for o in objects if o['_kind'] == kind], kind)
                setattr(snap, edge, parsed)
        stamp = datetime.fromtimestamp(self.now - age).astimezone()
        snap.finished_at, snap.finished_at_utc = stamp.strftime('%Y-%m-%d %H:%M:%S'), stamp.astimezone(timezone.utc).isoformat()
        ci.put_result(snap)
        return snap

    def read(self, account, trigger):
        self.reads.append((account, trigger))
        self.entered.set()
        if self.hold:
            assert self.release.wait(10), 'test must release its synthetic reader'
        if account in self.fail:
            snap = ci.Snapshot(account=account, state='failed', error='Proxy connection failed', error_code='proxy_failed')
            ci.put_result(snap)
            return snap
        return self.save(account)

    def html_for(self, query):
        from src.web.app import templates
        f = parse_qs(query).get('date_from', ['2026-10-08'])[0]
        span = parse_qs(query).get('span', [''])[0]
        rows = [dict(account=a, has_data=True, spend=float(int(f[-2:])) if f else 999, leads=10, deposits=2,
                     status={}, workbook_alias='', elsewhere={}) for a in self.accounts]
        country = dict(country='Kuwait', key='Kuwait', rows=rows, virtual=False)
        amap = dict(span=span, date_from=f, date_to=f, live={}, fallback={}, splits={}, notes={}, countries=[country])
        country_html = templates.env.get_template('_account_map_country.html').render(mc=country, amap=amap, mlx={})
        return ('<!doctype html><html><head><meta charset="utf-8"><style>' + AMAP_CSS +
                'body{font-family:system-ui;margin:16px;color:#1a1d23;background:#f4f5f7}</style></head><body>' +
                templates.env.get_template('_campaign_table.html').render() +
                '<div class="amap"><details class="amap-country" open>' + country_html + '</details></div>' +
                '<script src="/static/warm_sessions.js"></script>' +
                '<script>window.AMAP_CAMPAIGN_TABLE=true;function amapPeriod(){return ' +
                json.dumps({'from': '' if span == 'all' else f, 'to': f, 'span': span}) + ';}\n' + MOUNT_JS + APPLY_JS + '</script></body></html>')

    def handle(self, route):
        req, u = route.request, urlparse(route.request.url)
        self.requests.append((req.method, u.path))
        if u.path == '/':
            return route.fulfill(content_type='text/html', body=self.html_for(u.query))
        if u.path.startswith('/static/'):
            path = ROOT / 'src/web/static' / u.path.rsplit('/', 1)[1]
            return route.fulfill(status=200 if path.exists() else 404, content_type='application/javascript',
                                 body=path.read_text() if path.exists() else '')
        if u.path.startswith('/warm/'):
            return route.fulfill(content_type='application/json', body=json.dumps({'available': self.warm_available, 'actions': True, 'accounts': {a: {'state':'closed','text':'Closed'} for a in self.accounts}}))
        assert u.path.startswith(('/account-refresh/', '/campaign-inventory/', '/campaign-commands/')), u.path
        # TestClient executes the actual ASGI form/CSRF/off-loop/route handling.
        headers = {'Content-Type': req.headers.get('content-type', '')}
        response = self.client.request(req.method, u.path + ('?' + u.query if u.query else ''),
                                       content=req.post_data_buffer, headers=headers)
        return route.fulfill(status=response.status_code, content_type=response.headers.get('content-type', 'application/json'), body=response.content)


@pytest.fixture
def dashboard(tmp_path, monkeypatch):
    from src.web import app as web
    site = Dashboard(web, monkeypatch, tmp_path)
    with sync_playwright() as pw:
        browser = launch_chromium(pw)
        page = browser.new_page(viewport={'width': 1280, 'height': 900})
        page.route('http://acl.test/**', site.handle)
        page.on('pageerror', lambda error: site.errors.append(str(error)))
        try:
            yield site, page
        finally:
            site.release.set()
            site.q.stop()
            browser.close()


def wait_ready(page):
    page.wait_for_function("document.querySelector('.amap-campaigns')._hc && document.querySelector('.amap-campaigns')._hc.state.rows.Acc")


def test_ten_date_changes_rejoin_workbook_keep_structure_and_open_zero_profiles(dashboard):
    site, page = dashboard
    goto(page, 'http://acl.test/?view_only=1')
    wait_ready(page)
    for day in range(1, 10):
        page.evaluate('(d)=>amapApply(d,d,\'\')', f'2026-10-{day:02d}')
        wait_ready(page)
        open_acc(page, 'Acc')
        row = page.locator('tr[data-key="campaign:2"]')
        assert row.count() == 1 and f'${day:.2f}' in row.inner_text()
    page.evaluate("amapApply('','','all')")
    wait_ready(page)
    open_acc(page, 'Acc')
    assert '$999.00' in page.locator('tr[data-key="campaign:2"]').inner_text()
    assert site.reads == []
    assert not [r for r in site.requests if r == ('POST', '/account-refresh/page-open')]
    assert page.locator('button', has_text='Read campaigns').count() == 0
    assert page.locator('[data-col^="m_"]').count() == 0 and site.errors == []


def test_first_window_serial_sweep_skips_fresh_and_reload_does_not_repeat(dashboard):
    site, page = dashboard
    site.save('Acc', age=7200)
    goto(page, 'http://acl.test/')
    wait_ready(page)
    page.wait_for_function("document.querySelector('.amap-campaigns')._hc.state.summaries.Acc.age_s < 3600")
    assert site.reads == [('Acc', 'schedule:2026-10-24/08:00')]
    page.reload(); wait_ready(page)
    assert site.reads == [('Acc', 'schedule:2026-10-24/08:00')]
    assert site.q.state()['sweep']['done'] == 2 and site.errors == []


def test_stale_expand_shows_cache_while_updating_then_new_list_without_reopening(dashboard):
    site, page = dashboard
    site.save('Acc', age=7200)
    site.hold = True
    goto(page, 'http://acl.test/?view_only=1'); wait_ready(page)
    open_acc(page, 'Acc')
    assert site.entered.wait(3)
    assert page.locator('tr[data-key="campaign:1"]').is_visible()
    page.wait_for_function("document.querySelector('.hc-snap').textContent === 'updating'")
    assert len(site.reads) == 1 and site.reads[0] == ('Acc', 'attention')
    site.release.set()
    page.wait_for_function("document.querySelector('.hc-snap').textContent === 'list 08:00'")
    assert page.locator('tr[data-key="campaign:1"]').is_visible() and len(site.reads) == 1
    assert site.errors == []


def test_proxy_failure_has_one_row_retry_and_does_not_stop_other_accounts(dashboard):
    site, page = dashboard
    site.fail.add('Acc')
    site.save('Acc', age=7200); site.save('Fresh', age=7200)
    goto(page, 'http://acl.test/'); wait_ready(page)
    page.wait_for_function("document.querySelector('.amap-campaigns')._hc.state.refreshRows.Fresh && document.querySelector('.amap-campaigns')._hc.state.refreshRows.Acc.state === 'failed'")
    assert [a for a, _ in site.reads] == ['Acc', 'Fresh']
    retry = page.locator('tr:has(td[data-account="Acc"]) button.hc-rc')
    assert retry.is_visible() and retry.inner_text() == 'Retry'
    assert page.locator('button.hc-rc:visible').count() == 1
    site.fail.clear(); retry.click()
    page.wait_for_function("document.querySelector('.amap-campaigns')._hc.state.refreshRows.Acc.state === 'ok'")
    assert site.reads[-1] == ('Acc', 'retry') and len(site.reads) == 3
    assert not retry.is_visible() and site.errors == []


def test_hand_edit_pause_refuses_write_shows_message_and_refreshes_account(dashboard, monkeypatch, tmp_path):
    from types import SimpleNamespace
    from src.web import campaign_commands as cc
    from src.fb_api import commands as cmd
    from tests.test_acl2063_commands_refresh import SessionWorld, InventoryMeta
    from tests.test_acl1800_campaign_commands import ACT, CAMP_A
    site, page = dashboard
    world = SessionWorld(tmp_path, InventoryMeta())
    cmd._STORES.clear()
    world.store = cmd.get_store()
    site.command_world = world
    monkeypatch.setenv('ACL_BIND_HOST', '127.0.0.1')
    monkeypatch.setattr(cc, '_sessions', lambda: world.open_session)
    monkeypatch.setattr(ci._as, '_entry_for', lambda a: SimpleNamespace(
        ad_account_id=ACT, profile_id='11111111-1111-1111-1111-111111111111'))
    # A running campaign is paused in the UI, but the human already paused it
    # after preview. Execute must preserve the exact no-write stale guard.
    world.meta.objs[CAMP_A]['status'] = 'ACTIVE'
    site.save('Acc')
    goto(page, 'http://acl.test/?view_only=1'); wait_ready(page); open_acc(page, 'Acc')
    switch = page.locator(f'.hc-switch[data-object-id="{CAMP_A}"]')
    page.wait_for_function("document.querySelector('.amap-campaigns')._hc.state.avail !== null")
    switch.click()
    page.wait_for_selector('dialog.acm-confirm[open]')
    world.meta.objs[CAMP_A]['status'] = 'PAUSED'
    page.click('dialog.acm-confirm [data-acm="confirm"]')
    page.wait_for_function("document.querySelector('.amap-campaigns')._hc.state.summaries.Acc.counts.campaigns > 0")
    page.wait_for_function("document.querySelector('[data-hc=batch-note]').textContent.toLowerCase().includes('changed since the preview')")
    page.wait_for_function(f"document.querySelector('.hc-switch[data-object-id=\"{CAMP_A}\"]').getAttribute('aria-checked') === 'false'")
    assert world.meta.posts == [] and site.reads == [('Acc', 'action')]
    assert site.errors == []
    cmd._STORES.clear()


def test_existing_warm_profile_controls_bind_to_structure_rows_without_reading(dashboard):
    site, page = dashboard
    site.warm_available = True
    goto(page, 'http://acl.test/?view_only=1'); wait_ready(page)
    page.wait_for_selector('.warm-chip[data-account="Acc"]')
    assert page.locator('.warm-chip').count() == 2
    assert page.locator('.warm-open-all').count() == 1
    open_acc(page, 'Acc')
    assert site.reads == [] and not [r for r in site.requests if r[0] == 'POST' and r[1].startswith('/warm/')]
    assert site.errors == []


def test_backend_rpa_keeps_saved_structure_and_bi_but_refuses_real_reads(dashboard, monkeypatch):
    site, page = dashboard
    monkeypatch.setenv('ACL_FB_BACKEND','rpa')
    goto(page,'http://acl.test/?view_only=1'); wait_ready(page); open_acc(page,'Acc')
    assert page.locator('tr[data-key="campaign:1"]').is_visible()
    assert '$8.00' in page.locator('tr[data-key="campaign:2"]').inner_text()
    page.wait_for_function("document.getElementById('account-refresh-status').textContent.includes('API backend')")
    assert site.reads==[] and site.q.state()['queued']==[]
    assert site.client.post('/account-refresh/request',data={'account':'Acc','trigger':'retry'}).status_code==409
    assert site.errors==[]
