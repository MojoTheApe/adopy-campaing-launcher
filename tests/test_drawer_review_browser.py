"""Full rendered drawer, real DOM/events and offline HTTP boundaries."""
import json
import os
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from email.parser import BytesParser
from urllib.parse import urlparse

import pytest
from playwright.sync_api import sync_playwright, expect
from test_builder_per_account import _render


def fields(request):
    header = 'Content-Type: ' + request.headers['content-type'] + '\r\n\r\n'
    msg = BytesParser().parsebytes(header.encode() + request.post_data_buffer)
    return {part.get_param('name', header='content-disposition'): part.get_payload(decode=True).decode()
            for part in msg.get_payload()}


# ACL-1668: the Builder marks a chosen creative whose file is not in the loaded
# /api/creatives list as missing and refuses to publish it, so offline fixtures
# that write names straight into the hidden field must list those files too.
OFFLINE_NAMES = (['offline.png', 'good.png', 'fixed.png']
                 + ['offline-%d.png' % i for i in range(40)]
                 + ['offline-ad-%d.png' % i for i in range(40)])
OFFLINE_FILES = [{'filename': n, 'rel_path': n, 'folder': '', 'type': 'Image', 'size_bytes': 1}
                 for n in OFFLINE_NAMES]


@pytest.fixture(scope='module')
def browser():
    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=os.environ.get('ACL_BROWSER_EXECUTABLE') or None)
        yield b
        b.close()


@pytest.fixture
def ui(browser):
    context = browser.new_context()
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    state = {'posts': [], 'previews': [], 'held': [], 'hold': False, 'status': 'done',
             # ACL-1628: сколько заданий должно уйти, прежде чем ответит опрос
             # состояния; 0 — отвечает сразу. Ответы опроса, дождавшиеся этого.
             'poll_after_posts': 0, 'polls_held': []}

    def answer_poll(r):
        if state['status'] == 'missing':
            r.fulfill(status=404, json={'error': 'Not found'})
            return
        r.fulfill(json={'status': state['status'], 'publishable': True,
                        'results': [{'success': True, 'publishable': True}], 'errors': []})
    html = _render({'Demo': [], 'Other': []})

    def route(r):
        path = urlparse(r.request.url).path
        if r.request.resource_type == 'document':
            r.fulfill(content_type='text/html', body=html)
            return
        data = {'files': OFFLINE_FILES, 'texts': [], 'profiles': [], 'waiting': False}
        if path == '/preview-publish':
            f = fields(r.request)
            state['previews'].append(f)
            plan = []
            for i in range(int(f['camp_count'])):
                name = f[f'camp_{i}_name']
                errs = [] if f.get(f'camp_{i}_ad_0_creative_file') else ['Creative file is required']
                plan.append({'Campaign Name': name, 'errors': errs, 'warnings': [], '_ads_raw': []})
            data = {'plan': plan}
            if state['hold']:
                state['held'].append((r, data))
                return
        elif path == '/publish-jobs' and r.request.method == 'POST':
            state['posts'].append(fields(r.request))
            data = {'job_id': 'offline-job-' + str(len(state['posts']))}
            r.fulfill(json=data)
            # Все нужные задания ушли — отпускаем придержанные ответы опроса.
            if len(state['posts']) >= state['poll_after_posts']:
                held, state['polls_held'] = state['polls_held'], []
                for h in held:
                    answer_poll(h)
            return
        elif path.startswith('/publish-jobs/'):
            if len(state['posts']) < state['poll_after_posts']:
                state['polls_held'].append(r)
                return
            answer_poll(r)
            return
        r.fulfill(json=data)

    page.route('**/*', route)
    page.goto('http://acl.test/')
    page.evaluate('cbDrawer.open()')
    yield page, state
    assert not errors, errors
    context.close()


def add(page, account=0, count=1, valid=True):
    page.evaluate('''([account,count,valid]) => {
      const f=document.querySelectorAll('form.open-account-form')[account];
      for(let i=0;i<count;i++) {
        cbAddSlot(f); const s=f.querySelector('.cb-slot:last-child');
        s.querySelector('[name$="_asset"]').value='Offer '+i;
        if(valid) s.querySelector('[name$="_ad_0_creative_file"]').value='offline-'+i+'.png';
      }
      f.dispatchEvent(new Event('input',{bubbles:true}));
    }''', [account, count, valid])
    page.wait_for_function("document.querySelectorAll('.cbd-camp').length > 0")


def _until(page, condition, timeout_s=10.0):
    """Ждать, пока Python-сторона увидит перехваченный запрос (ACL-1397).

    Проверка превью уходит с задержкой (debounce) и приходит в `route` на
    стороне теста; прежде тест ждал ровно 450 мс и на занятой машине
    (параллельный прогон) иногда проверял раньше, чем запрос дошёл.
    Условие проверяется между короткими ожиданиями страницы — в них же
    Playwright доставляет события `route`."""
    deadline = time.monotonic() + timeout_s
    while not condition() and time.monotonic() < deadline:
        page.wait_for_timeout(50)


def ready(page):
    page.wait_for_function("Array.from(document.querySelectorAll('.cb-slot')).every(s => s._drawerResult && s._drawerResult.state === 'ok')")


def all_confirm(page):
    page.locator('.cbd-publish-all').click()
    page.locator('#sfc-ok').click()


def test_readiness_uses_server_checks_and_recovers_after_edit(ui):
    p, state = ui
    add(p, valid=False)
    p.locator('.cbd-camp-row').first.click()
    expect(p.locator('.cbd-ind-bad')).to_have_count(1)
    expect(p.locator('.cb-creative-search').first).to_have_attribute('aria-invalid', 'true')
    state['hold'] = True
    p.evaluate('''() => {const x=document.querySelector('[name="camp_0_ad_0_creative_file"]'); x.value='good.png'; x.dispatchEvent(new Event('input',{bubbles:true}));}''')
    expect(p.locator('.cbd-ind-wait')).to_have_count(1)
    expect(p.locator('.cb-field-error')).to_have_count(0)
    _until(p, lambda: state['held'])
    assert state['held']
    state['hold'] = False
    for r, data in state['held']:
        r.fulfill(json=data)
    ready(p)
    expect(p.locator('.cb-in-drawer .cb-publish-btn')).to_be_enabled()
    expect(p.locator('.cb-in-drawer .cb-preview-panel')).not_to_be_visible()


def test_single_campaign_never_submits_hidden_siblings(ui):
    p, state = ui
    add(p, count=2)
    ready(p)
    p.locator('.cbd-camp-row').first.click()
    p.locator('.cb-in-drawer .cb-publish-btn').click()
    p.locator('#sfc-ok').click()
    p.wait_for_function('window.CB_QUEUE && !CB_QUEUE.active')
    assert len(state['posts']) == 1
    sent = state['posts'][0]
    assert sent['camp_count'] == '1'
    assert sent['camp_0_asset'] == 'Offer 0'
    assert not any(k.startswith('camp_1_') for k in sent)
    assert sent['activate'] == '0' and sent['hand_off'] == '1'


def test_cancel_confirmation_and_active_queue_survive_repaint(ui):
    p, state = ui
    add(p)
    add(p, account=1)
    ready(p)
    p.locator('.cbd-publish-all').click()
    p.locator('#sfc-cancel').click()
    expect(p.locator('.cbd-publish-all')).to_be_enabled()
    state['status'] = 'running'
    all_confirm(p)
    # Оба аккаунта уходят сразу (acl-1210); повторный «Publish all» поверх
    # идущей очереди не добавляет ни одной работы. Zero unlocked campaigns
    # disables the action; the programmatic duplicate guard still explains why.
    # Other accounts remain enqueueable as soon as one is added.
    p.wait_for_function("CB_QUEUE.progress.stage === 'publishing'")
    _until(p, lambda: len(state['posts']) >= 2)
    assert len(state['posts']) == 2
    native = []
    p.once('dialog', lambda d: (native.append(d.message), d.accept()))
    p.evaluate('cbTree.paint(); cbPublishAll()')
    expect(p.locator('.cbd-publish-all')).to_be_disabled()
    expect(p.locator('.cbd-publish-all')).to_have_text('Publish all → drafts (0)')
    expect(p.locator('#sfc-ok')).to_have_count(0)
    # ACL-141: отказ — окно страницы, а не alert браузера.
    said = p.locator('dialog.acl-dlg')
    expect(said).to_contain_text('Demo, Other are already being built')
    assert not native, native
    said.locator('.acl-dlg-primary').click()
    expect(said).to_have_count(0)
    assert len(state['posts']) == 2
    state['status'] = 'done'
    p.wait_for_function('!CB_QUEUE.active', timeout=12000)
    assert [f['account_label'] for f in state['posts']] == ['Demo', 'Other']


@pytest.mark.parametrize('status', ['missing', 'cancelled'])
def test_lost_or_cancelled_job_closes_the_queue_and_marks_it(ui, status):
    # С acl-1210 оба аккаунта уходят на сервер сразу после своих превью —
    # раньше, чем первый опрос состояния успевает ответить «отменено».
    # Потерянная/отменённая работа закрывает очередь и помечает её; уже
    # отправленные работы не трогает — у каждой свой Stop.
    p, state = ui
    add(p)
    add(p, account=1)
    ready(p)
    state['status'] = status
    # Потерянный статус первого задания по задумке останавливает отправку
    # второго аккаунта, если его превью ещё в пути. Тест проверяет не эту
    # гонку, а то, что уже отправленные работы остаются: опрос отвечает,
    # когда ушли обе.
    state['poll_after_posts'] = 2
    all_confirm(p)
    p.wait_for_function('window.CB_QUEUE && !CB_QUEUE.active')
    assert len(state['posts']) == 2
    assert p.evaluate('CB_QUEUE.uncertain') is (status == 'missing')
    if status == 'missing':
        p.evaluate('cbPublishAll()')
        expect(p.locator('#sfc-ok')).to_have_count(0)
        assert len(state['posts']) == 2


def test_edit_during_preview_completes_without_submitting_stale_values(ui):
    p, state = ui
    add(p)
    ready(p)
    state['hold'] = True
    all_confirm(p)
    _until(p, lambda: state['held'])
    assert state['held']
    p.evaluate('''() => {const x=document.querySelector('[name="camp_0_budget"]');x.value='99';x.dispatchEvent(new Event('input',{bubbles:true}));}''')
    state['hold'] = False
    r, data = state['held'][0]
    r.fulfill(json=data)
    p.wait_for_function('!CB_QUEUE.active')
    assert state['posts'] == []
    assert 'changed' in p.evaluate('CB_QUEUE.progress.report[0].why')


def test_immediate_reload_keeps_tracking_and_campaign_fields(ui):
    p, _ = ui
    add(p)
    p.evaluate('''() => {const f=document.querySelector('form.open-account-form');
      f.querySelector('[name="tracking_link"]').value='https://example.test/track';
      f.querySelector('[name="camp_0_budget"]').value='77';
      f.dispatchEvent(new Event('input',{bubbles:true}));
      location.reload();
    }''')
    p.wait_for_load_state('load')
    expect(p.locator('[name="camp_0_budget"]').first).to_have_value('77')
    expect(p.locator('[name="tracking_link"]').first).to_have_value('https://example.test/track')
    expect(p.locator('[name="camp_0_ad_0_creative_file"]').first).to_have_value('offline-0.png')


def test_repaint_does_not_refresh_an_untouched_accounts_expiry(ui):
    p, _ = ui
    add(p)
    add(p, account=1)
    # Сначала обе карточки проверены сервером (ACL-1397): ответ проверки
    # дописывает поля карточки, и если он приходил после снимка `before`
    # (занятая машина, параллельный прогон), «Other» честно обновлял
    # `touched` — тест ловил свою же гонку, а не перерисовку.
    ready(p)
    p.evaluate('cbKeep.save()')
    before =p.evaluate("JSON.parse(localStorage.getItem('acl.builder.v2')).forms")
    p.wait_for_timeout(20)
    p.evaluate('''() => {document.querySelector('[name="camp_0_budget"]').value='81';cbKeep.save();}''')
    after = p.evaluate("JSON.parse(localStorage.getItem('acl.builder.v2')).forms")
    other = next(k for k in before if k.endswith('|Other'))
    demo = next(k for k in before if k.endswith('|Demo'))
    assert after[other]['touched'] == before[other]['touched']
    assert after[demo]['touched'] > before[demo]['touched']


def test_stale_check_cannot_overwrite_a_newer_valid_result(ui):
    p, state = ui
    state['hold'] = True
    add(p, valid=False)
    _until(p, lambda: state['held'])
    assert state['held']
    old, data = state['held'][0]
    state['hold'] = False
    p.evaluate('''() => {const x=document.querySelector('[name="camp_0_ad_0_creative_file"]');x.value='fixed.png';x.dispatchEvent(new Event('input',{bubbles:true}));}''')
    ready(p)
    old.fulfill(json=data)
    p.wait_for_timeout(100)
    expect(p.locator('.cbd-ind-ok')).to_have_count(1)


def test_publish_all_skips_red_campaigns(ui):
    p, state = ui
    add(p, count=2)
    ready(p)   # обе проверены сервером, и только потом одна краснеет (ACL-1397)
    p.evaluate('''() => {const x=document.querySelector('[name="camp_1_ad_0_creative_file"]'); x.value='';x.dispatchEvent(new Event('input',{bubbles:true}));}''')
    expect(p.locator('.cbd-ind-bad')).to_have_count(1)
    all_confirm(p)
    p.wait_for_function('window.CB_QUEUE && !CB_QUEUE.active')
    assert len(state['posts']) == 1
    assert state['posts'][0]['camp_count'] == '1'
    assert state['posts'][0]['camp_0_asset'] == 'Offer 0'


def test_malformed_saved_form_is_ignored_without_breaking_dashboard(ui):
    p, _ = ui
    key = p.evaluate("document.querySelector('form.open-account-form').dataset.cbdKey")
    p.evaluate('''key => {localStorage.setItem('acl.builder.v2',JSON.stringify({v:2,forms:{[key]:{touched:Date.now(),campaigns:[{fields:null,ads:2}]}}}));}''', key)
    # Navigate to a second document in the same origin; the first page's pagehide
    # legitimately saves its own current empty state, so seed before scripts there.
    p.context.add_init_script(f"localStorage.setItem('acl.builder.v2',JSON.stringify({{v:2,forms:{{[{json.dumps(key)}]:{{touched:Date.now(),campaigns:[{{fields:null,ads:2}}]}}}}}}));")
    p.reload()
    expect(p.locator('.cb-slot')).to_have_count(0)
    p.evaluate('cbDrawer.open()')
    expect(p.locator('[data-act="add-country"]')).to_be_visible()


def test_connection_borrows_the_existing_country_form(ui):
    from jinja2 import Environment, FileSystemLoader
    p, _ = ui
    env = Environment(loader=FileSystemLoader(str(Path(__file__).resolve().parents[1] / 'src/web/templates')), autoescape=True)
    env.filters['fmt_usd'] = str
    country = {'country':'Saudi Arabia', 'key':'SA', 'rows':[], 'virtual':False, 'spend':0, 'deposits':0, 'leads':0}
    env.filters['fmt_currency'] = lambda v,c: str(v)
    env.filters['fmt_summary_amount'] = lambda v,c: str(v)
    markup = env.get_template('_account_map.html').render(mc=country,
        amap={'countries':[country],'available':[],'all_accounts':[],'live':{},'dates':[],'date_from':'','date_to':'','span':''}, mlx={'A':'Inhouse','B':'Agency'})
    p.evaluate("html => {const host=document.createElement('div');host.id='offline-map';host.innerHTML=html;document.body.appendChild(host);window.originalConnect=host.querySelector('details.amap-country .amap-add');}", markup)
    p.locator('[data-act="connect-picked"]').click()
    expect(p.locator('.cbd-connect-host .amap-add')).to_have_count(1)
    assert p.evaluate("document.querySelector('.cbd-connect-host .amap-add') === originalConnect")
    # Одна на эту страну, а не две: форма ПЕРЕЕХАЛА в ящик, а не скопировалась.
    # Счёт сузился до конкретной страны, потому что во вкладке Accounts теперь
    # стоит такая же форма — но она ни к какой стране не привязана (значение
    # пустое) и второй копией этой не является. Считать все подряд значило бы
    # ловить не то: проверка здесь про переезд, а не про число форм на странице.
    expect(p.locator('.amap-add[data-amap-country="SA"]')).to_have_count(1)
    p.locator('[data-act="connect-picked"]').click()
    assert p.evaluate("document.querySelector('#offline-map details.amap-country .amap-add') === originalConnect")


def test_delayed_tree_repaint_keeps_typing_focus_and_selection(ui):
    p, _ = ui
    p.evaluate("""() => {
      document.querySelector('[data-act=\"add-picked\"]').click();
      cbTree.setLevel('adset');   // ACL-1769: таргетинг лежит в разделе Ad set
      const x=document.querySelector('.cb-in-drawer .cb-targeting-search');
      x.focus();x.value='typed targeting';x.setSelectionRange(2,7);
      x.dispatchEvent(new Event('input',{bubbles:true}));
    }""")
    p.wait_for_timeout(250)
    expect(p.locator('.cb-in-drawer .cb-targeting-search')).to_be_focused()
    assert p.evaluate('[document.activeElement.selectionStart,document.activeElement.selectionEnd]') == [2,7]
