"""Run the shipped connect markup, styles and handlers in an offline browser."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re

import pytest
from jinja2 import Environment, FileSystemLoader
from playwright.sync_api import sync_playwright

TPL = Path(__file__).resolve().parents[1] / 'src/web/templates'


@pytest.fixture(scope='module')
def browser():
    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=os.environ.get('ACL_BROWSER_EXECUTABLE') or None)
        yield b
        b.close()


def _page(browser, *, alive=False, token_saved=True, workspaces=None):
    report = (TPL / 'report.html').read_text(encoding='utf-8')
    from _design_tokens import tokens_css  # ACL-1797: палитра — отдельный файл
    styles = tokens_css() + '\n'.join(re.findall(r'<style[^>]*>(.*?)</style>', report, re.S))
    script = report[report.index('var _mlxCache ='):report.index('\nfunction amapHint')]
    shared = report[report.index('<template id="amap-ml-signin-template">'):report.index('</body>')]
    env = Environment(loader=FileSystemLoader(str(TPL)), autoescape=True)
    env.filters['fmt_usd'] = str
    # ACL-1271: ячейка чтения собирает запасное число блоком `set`, и Jinja
    # проверяет его фильтр сразу, а не только когда ветка выполняется.
    from src.analysis_agent.money_format import fmt_currency
    env.filters['fmt_currency'] = fmt_currency
    countries = [{'country': c, 'key': k, 'rows': [], 'virtual': False} for c,k in [('Mexico','MX'),('Spain','ES')]]
    blocks = ''.join(env.get_template('_account_map_country.html').render(
        mc=c, amap={'countries':countries, 'available':[], 'all_accounts':[], 'live':{}, 'dates':[], 'date_from':'','date_to':'','span':''},
        mlx={'A':'Inhouse','B':'Agency'}) for c in countries)
    html = f'<style>{styles}</style>{blocks}{shared}<script>var ACL_MLX_LABELS = {{A:"Inhouse",B:"Agency"}};{script}</script>'
    ctx = browser.new_context()
    page = ctx.new_page()
    state = {'alive': {'A':alive,'B':False}, 'calls':[], 'token_saved':token_saved, 'workspaces':workspaces}
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    def route(r):
        from urllib.parse import urlsplit, parse_qs
        url = urlsplit(r.request.url)
        if url.path == '/':
            r.fulfill(body=html, content_type='text/html'); return
        data = r.request.post_data_json if r.request.method == 'POST' else {}
        state['calls'].append((url.path, data))
        if url.path == '/multilogin-token-status':
            inst = parse_qs(url.query)['instance'][0]
            body = {'ok':True,'alive':state['alive'][inst],'has_token':state['alive'][inst]}
        elif (url.path == '/multilogin-signin' and state['workspaces']
              and not data.get('workspace_id')):
            # ACL-1441: несколько пространств — сервер просит выбрать.
            body = {'ok':True,'choose_workspace':True,'token_saved':False,'token_error':'',
                    'workspaces':state['workspaces']}
        elif url.path in ('/multilogin-signin','/multilogin-save-token'):
            saved = state['token_saved'] or url.path.endswith('save-token')
            state['alive'][data['instance']] = saved
            body = {'ok':True,'token_saved':saved,'token_error':'Workspace owner must issue the automation token'}
        elif url.path == '/multilogin-search':
            body = {'ok':True, 'profiles':[]}
        else:
            r.abort(); return
        r.fulfill(body=json.dumps(body), content_type='application/json')
    ctx.route('**/*', route)
    page.goto('http://acl.test/')
    page.wait_for_function("Object.keys(AMAP_ML_STATE).length === 1")
    return ctx, page, state, errors


def test_live_token_has_no_credentials_in_the_dom(browser):
    ctx,p,s,errors = _page(browser, alive=True)
    try:
        assert p.locator('input[type=password]').count() == 0
        assert p.locator('.amap-ml-open:visible').count() == 0
        assert p.locator('.amap-ml-line').first.inner_text() == 'Multilogin Inhouse: connected'
        assert [x[0] for x in s['calls']].count('/multilogin-token-status') == 1
        assert not errors
    finally: ctx.close()


def test_one_form_moves_to_the_account_being_connected(browser):
    ctx,p,s,errors = _page(browser)
    try:
        p.locator('.amap-ml-open').nth(0).click()
        p.locator('.amap-ml-pass').fill('discard-me')
        p.locator('.amap-ml-open').nth(1).click()
        assert p.locator('.amap-ml-signin').count() == 1
        assert p.locator('.amap-add').nth(1).locator('.amap-ml-pass').is_visible()
        assert p.locator('.amap-ml-pass').input_value() == ''
        p.get_by_role('button', name='RPA help').first.click()
        assert p.get_by_role('heading', name='How to run the RPA').count() == 1
        assert p.locator('#amap-rpa-help').is_visible()
        assert not errors
    finally: ctx.close()


def test_signin_removes_form_everywhere_without_reload(browser):
    ctx,p,s,errors = _page(browser)
    try:
        p.locator('.amap-ml-open').first.click()
        p.locator('.amap-ml-email').fill('tester@example.test')
        p.locator('.amap-ml-pass').fill('synthetic-password')
        p.locator('.amap-ml-signin').get_by_role('button', name='Connect', exact=True).click()
        p.wait_for_function("document.querySelectorAll('.amap-ml-open:not([hidden])').length === 0")
        assert p.locator('.amap-ml-signin').count() == 0
        assert p.locator('input[type=password]').count() == 0
        assert 'Connected' in p.locator('.amap-msg').first.inner_text()
        assert not errors
    finally: ctx.close()


def test_signin_without_saved_token_shows_reason_not_success(browser):
    ctx,p,s,errors = _page(browser, token_saved=False)
    try:
        p.locator('.amap-ml-open').first.click()
        p.locator('.amap-ml-email').fill('tester@example.test')
        p.locator('.amap-ml-pass').fill('synthetic-password')
        p.locator('.amap-ml-signin').get_by_role('button', name='Connect', exact=True).click()
        p.wait_for_function("document.querySelector('.amap-msg').textContent.includes('Workspace owner')")
        assert p.locator('.amap-ml-pass').is_visible()
        assert p.locator('.amap-ml-pass').input_value() == ''
        assert not errors
    finally: ctx.close()


def test_token_paste_uses_selected_instance_and_invalidates_profile_cache(browser):
    ctx,p,s,errors = _page(browser, alive=True)
    try:
        p.locator('.amap-inst').first.select_option('B')
        p.locator('.amap-ml-open').first.click()
        p.evaluate("_mlxCache.B = [{name:'old workspace'}]; _mlxCache.A = [{name:'keep'}]")
        p.locator('.amap-ml-token').fill('synthetic-token')
        p.get_by_role('button', name='Save token', exact=True).click()
        p.wait_for_function("AMAP_ML_STATE.B.alive === true")
        assert p.evaluate('_mlxCache.B === undefined && _mlxCache.A[0].name === "keep"')
        assert next(d for path,d in s['calls'] if path == '/multilogin-save-token') == {'token':'synthetic-token','instance':'B'}
        p.locator('.amap-find').first.fill('X84')
        p.wait_for_function("document.querySelector('.amap-msg').textContent.includes('nothing')")
        assert any(path == '/multilogin-search' and d['instance']=='B' for path,d in s['calls'])
        assert not errors
    finally: ctx.close()


def test_instance_switch_discards_other_workspace_credentials(browser):
    ctx,p,s,errors = _page(browser)
    try:
        p.locator('.amap-ml-open').first.click()
        p.locator('.amap-ml-pass').fill('synthetic-password')
        p.locator('.amap-inst').first.select_option('B')
        assert p.locator('.amap-ml-signin').count() == 0
        assert not errors
    finally: ctx.close()


def test_token_change_invalidates_every_search_query_for_that_instance(browser):
    ctx, p, state, errors = _page(browser, alive=True)
    try:
        p.evaluate("async () => {await amapProfiles('A', false, 'keep'); await amapProfiles('B', false, 'X84'); await amapProfiles('B', false, 'X85');}")
        before = len([x for x in state['calls'] if x[0] == '/multilogin-search'])
        p.locator('.amap-inst').first.select_option('B')
        p.locator('.amap-ml-open').first.click()
        p.locator('.amap-ml-token').fill('replacement-synthetic-token')
        p.get_by_role('button', name='Save token', exact=True).click()
        p.wait_for_function("AMAP_ML_STATE.B.alive === true")
        p.evaluate("async () => {await amapProfiles('A', false, 'keep'); await amapProfiles('B', false, 'X84'); await amapProfiles('B', false, 'X85');}")
        calls = [x for x in state['calls'] if x[0] == '/multilogin-search'][before:]
        assert [x[1]['search_text'] for x in calls] == ['X84', 'X85']
        assert all(x[1]['instance'] == 'B' for x in calls)
        assert not errors
    finally:
        ctx.close()


def test_several_workspaces_show_a_picker_and_continue_with_the_choice(browser):
    """ACL-1441: токен выдаётся там, где профили, — пространство выбирает человек."""
    workspaces = [
        {'workspace_id':'ws-own','name':'own@example.test','role':'owner','automation':False},
        {'workspace_id':'ws-kld','name':'Team KLD','role':'user','automation':True},
        {'workspace_id':'ws-inh','name':'Team Inhouse','role':'manager','automation':True},
    ]
    ctx,p,s,errors = _page(browser, workspaces=workspaces)
    try:
        p.locator('.amap-ml-open').first.click()
        p.locator('.amap-ml-email').fill('tester@example.test')
        p.locator('.amap-ml-pass').fill('synthetic-password')
        p.locator('.amap-ml-signin').get_by_role('button', name='Connect', exact=True).click()
        p.wait_for_selector('.amap-ml-ws select')
        options = p.locator('.amap-ml-ws option').all_inner_texts()
        assert options == ['own@example.test — owner · no automation',
                           'Team KLD — user', 'Team Inhouse — manager']
        assert p.locator('.amap-ml-ws select').input_value() == 'ws-kld', 'not the one without automation'
        assert 'choose the one with your profiles' in p.locator('.amap-msg').first.inner_text()
        assert p.locator('.amap-ml-pass').input_value() == 'synthetic-password', 'kept only until the choice'
        p.locator('.amap-ml-ws select').select_option('ws-inh')
        p.get_by_role('button', name='Continue', exact=True).click()
        p.wait_for_function("document.querySelectorAll('.amap-ml-signin').length === 0")
        signins = [d for path,d in s['calls'] if path == '/multilogin-signin']
        assert 'workspace_id' not in signins[0]
        assert signins[1]['workspace_id'] == 'ws-inh' and signins[1]['instance'] == 'A'
        assert p.locator('input[type=password]').count() == 0
        assert not errors
    finally: ctx.close()
