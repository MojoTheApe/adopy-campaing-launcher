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


def _page(browser, *, alive=False, token_saved=True, team_admin=True, status_extra=None,
          origin='http://acl.test'):
    report = (TPL / 'report.html').read_text(encoding='utf-8')
    styles = '\n'.join(re.findall(r'<style[^>]*>(.*?)</style>', report, re.S))
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
    state = {'alive': {'A':alive,'B':False}, 'calls':[], 'token_saved':token_saved,
             'team_admin': team_admin, 'status_extra': status_extra or {}}
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
            # ACL-1311: роль приходит в каждом ответе о состоянии.
            body = {'ok':True,'alive':state['alive'][inst],'has_token':state['alive'][inst],
                    'team_admin':state['team_admin'], **state['status_extra']}
        elif url.path in ('/multilogin-signin','/multilogin-save-token'):
            saved = state['token_saved'] or url.path.endswith('save-token')
            state['alive'][data['instance']] = saved
            body = {'ok':True,'token_saved':saved,'token_error':'Workspace owner must issue the automation token'}
        elif url.path == '/multilogin-team-token':
            body = ({'ok':True,'instance':data['instance'],'token':'team-token-' + data['instance']}
                    if state['team_admin'] else {'ok':False,'error':'Only the team lead'})
        elif url.path == '/multilogin-search':
            body = {'ok':True, 'profiles':[]}
        else:
            r.abort(); return
        r.fulfill(body=json.dumps(body), content_type='application/json')
    ctx.route('**/*', route)
    page.goto(origin + '/')
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


# ── Team access (ACL-1311) ─────────────────────────────────────────────────
#
# Логин Multilogin у команды общий; вход по паролю выпускает новый токен и
# выбивает остальных. Тимлид входит и копирует токен, байер вставляет готовый.

def test_a_buyer_is_offered_only_the_team_token(browser):
    ctx, p, s, errors = _page(browser, alive=False, team_admin=False)
    try:
        opener = p.locator('.amap-ml-open').first
        assert opener.inner_text() == 'Paste team token'
        opener.click()
        form = p.locator('.amap-ml-signin')
        assert form.locator('.amap-ml-email, .amap-ml-pass, .amap-ml-connect').count() == 0, (
            'у байера остались поля входа по паролю'
        )
        assert form.locator('.amap-ml-token').get_attribute('placeholder') == 'team token from your team lead'
        form.locator('.amap-ml-token').fill('team-token-A')
        form.get_by_role('button', name='Save token').click()
        p.wait_for_function("document.querySelectorAll('.amap-ml-open:not([hidden])').length === 0")
        assert ('/multilogin-save-token', {'token': 'team-token-A', 'instance': 'A'}) in s['calls']
        assert not any(path == '/multilogin-signin' for path, _ in s['calls'])
        assert errors == []
    finally:
        ctx.close()


def test_the_team_lead_signs_in_and_a_buyer_cannot_copy(browser):
    ctx, p, s, errors = _page(browser, alive=False, team_admin=True)
    try:
        assert p.locator('.amap-ml-open').first.inner_text() == 'Connect Multilogin'
        p.locator('.amap-ml-open').first.click()
        assert p.locator('.amap-ml-signin .amap-ml-email').count() == 1
    finally:
        ctx.close()
    ctx, p, s, errors = _page(browser, alive=True, team_admin=False)
    try:
        assert p.locator('.amap-ml-copy:visible').count() == 0, 'байеру предложено раздавать токен'
    finally:
        ctx.close()


def test_the_team_lead_copies_the_token_and_it_does_not_stay_on_the_page(browser):
    ctx, p, s, errors = _page(browser, alive=True, team_admin=True,
                              status_extra={'expires_at': '2026-10-09T09:30:00+00:00'})
    try:
        # Страница открыта не по https и не на localhost, поэтому Clipboard API
        # нет и работает запасной путь через execCommand — его и ловим.
        p.evaluate("""() => {
          window.__copied = null;
          document.execCommand = function (cmd) {
            if (cmd === 'copy') window.__copied = document.activeElement && document.activeElement.value;
            return true;
          };
        }""")
        copy = p.locator('.amap-ml-copy').first
        assert copy.is_visible()
        copy.click()
        p.wait_for_function("window.__copied !== null")
        assert p.evaluate("window.__copied") == 'team-token-A'
        assert ('/multilogin-team-token', {'instance': 'A'}) in s['calls']
        # Поле для копирования получает токен свойством `value`, а не атрибутом,
        # поэтому в тексте страницы его не видно. Ищем по живым полям.
        assert not p.evaluate(
            "[...document.querySelectorAll('textarea, input')].some(e => e.value === 'team-token-A')"
        ), 'поле с токеном осталось на странице'
        assert 'team-token-A' not in p.content(), 'токен попал в разметку страницы'
        msg = p.locator('.amap-msg').first.inner_text()
        assert 'copied' in msg.lower() and '09.10.2026' in msg
        assert errors == []
    finally:
        ctx.close()


def test_a_replaced_token_says_so_and_what_to_do(browser):
    ctx, p, s, errors = _page(browser, alive=False, team_admin=False, status_extra={
        'has_token': True, 'reason': 'revoked',
        'next_step': 'Ask your team lead for the current team token and paste it here.'})
    try:
        line = p.locator('.amap-ml-line').first.inner_text()
        assert 'team token was replaced' in line
        assert 'Ask your team lead' in line
        assert 'expired' not in line
    finally:
        ctx.close()


def test_the_expiry_is_a_date_not_a_timestamp(browser):
    ctx, p, s, errors = _page(browser, alive=True, team_admin=True,
                              status_extra={'expires_at': '2026-10-09T09:30:00+00:00'})
    try:
        line = p.locator('.amap-ml-line').first.inner_text()
        assert 'until 09.10.2026' in line
        assert 'T09:30' not in line
    finally:
        ctx.close()


def test_the_team_lead_copies_the_token_of_the_selected_account(browser):
    """Ревью 24.09: подмена слота на «A» проходила. В жизни — тимлид выбрал KLD
    и разослал команде токен Inhouse."""
    ctx, p, s, errors = _page(browser, alive=True, team_admin=True)
    try:
        s['alive']['B'] = True
        p.evaluate("""() => {
          window.__copied = null;
          document.execCommand = function (cmd) {
            if (cmd === 'copy') window.__copied = document.activeElement && document.activeElement.value;
            return true;
          };
        }""")
        p.locator('.amap-inst').first.select_option('B')
        p.wait_for_function("AMAP_ML_STATE['B'] && AMAP_ML_STATE['B'].alive")
        p.locator('.amap-ml-copy').first.click()
        p.wait_for_function("window.__copied !== null")
        assert p.evaluate("window.__copied") == 'team-token-B'
        assert ('/multilogin-team-token', {'instance': 'B'}) in s['calls']
    finally:
        ctx.close()


def test_the_team_lead_is_not_offered_to_copy_a_dead_token(browser):
    ctx, p, s, errors = _page(browser, alive=False, team_admin=True, status_extra={
        'has_token': True, 'reason': 'revoked', 'next_step': 'Sign in again.'})
    try:
        assert p.locator('.amap-ml-copy:visible').count() == 0, 'предложено разослать мёртвый токен'
    finally:
        ctx.close()


def test_a_refused_clipboard_is_not_reported_as_copied(browser):
    ctx, p, s, errors = _page(browser, alive=True, team_admin=True)
    try:
        p.evaluate("() => { document.execCommand = function () { return false; }; }")
        p.locator('.amap-ml-copy').first.click()
        p.wait_for_function("document.querySelector('.amap-msg').textContent.length > 0")
        msg = p.locator('.amap-msg').first.inner_text()
        assert 'copied' not in msg.lower(), 'отказ буфера показан как успех'
        assert 'clipboard' in msg.lower()
    finally:
        ctx.close()


def test_an_expired_token_tells_a_buyer_what_to_do(browser):
    ctx, p, s, errors = _page(browser, alive=False, team_admin=False, status_extra={
        'has_token': True, 'reason': 'expired',
        'next_step': 'Ask your team lead for the current team token and paste it here.'})
    try:
        line = p.locator('.amap-ml-line').first.inner_text()
        assert 'team token expired' in line and 'Ask your team lead' in line
    finally:
        ctx.close()


def test_a_refused_clipboard_api_falls_back_on_the_real_dashboard_address(browser):
    """На настоящем адресе дешборда (http://127.0.0.1) контекст безопасный, и
    копирует Clipboard API. Браузер может в нём отказать — тогда обязан сработать
    запасной путь, а не сырое «Write permission denied» и не ложное «copied»
    (ревью 24.09: заглушка вместо запасного пути проходила незамеченной, потому
    что прочие тесты открыты на http://acl.test и эту ветку не выполняют)."""
    ctx, p, s, errors = _page(browser, alive=True, team_admin=True, origin='http://127.0.0.1:9')
    try:
        assert p.evaluate("window.isSecureContext") is True
        p.evaluate("""() => {
          window.__copied = null;
          navigator.clipboard.writeText = function () { return Promise.reject(new Error('Write permission denied.')); };
          document.execCommand = function (cmd) {
            if (cmd === 'copy') window.__copied = document.activeElement && document.activeElement.value;
            return true;
          };
        }""")
        p.locator('.amap-ml-copy').first.click()
        p.wait_for_function("window.__copied !== null")
        assert p.evaluate("window.__copied") == 'team-token-A'
        msg = p.locator('.amap-msg').first.inner_text()
        assert 'copied' in msg.lower() and 'permission' not in msg.lower()
        assert not p.evaluate("[...document.querySelectorAll('textarea, input')].some(e => e.value === 'team-token-A')")
    finally:
        ctx.close()


def test_a_token_that_was_not_accepted_says_so(browser):
    ctx, p, s, errors = _page(browser, alive=False, team_admin=False, status_extra={
        'has_token': True, 'reason': 'invalid',
        'next_step': 'Ask your team lead for the current team token and paste it here.'})
    try:
        line = p.locator('.amap-ml-line').first.inner_text()
        assert 'team token was not accepted' in line
        assert 'expired' not in line and 'could not be verified' not in line
    finally:
        ctx.close()

