"""ACL-1355: аккаунт за аккаунтом из одной вкладки билдера — на настоящем шаблоне.

До этого вкладка держала одну пачку публикаций: пока собирался аккаунт A,
Publish у аккаунта B показывал «A run is already in progress in this tab …
open the dashboard in a second browser tab». Сервер при этом давно ставит
задания в очередь сам (память машины, один профиль — по очереди) и разные
профили ведёт параллельно — второй замок был на странице.

Страница рендерится целиком, сеть подменена поддельным `/publish-jobs`:
у каждого задания свой статус, отмена и `/progress`. Проверяется:
второй аккаунт стартует поверх идущего, у каждого свой ход, точки и итог;
тот же аккаунт второй раз не заводит дубль и говорит об этом; «Publish
all» поверх одиночного запуска ставит остальных и пропускает идущий;
Stop останавливает только своё задание; F5 возвращает оба задания; после
потери статуса остаётся прежняя подсказка «перезагрузите страницу».
"""
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest
from playwright.sync_api import expect, sync_playwright
from test_builder_per_account import _render
from test_drawer_review_browser import fields

ACTIVE = ('pending', 'queued', 'running', 'cancelling')
# Форма ищется по аккаунту, а не по номеру: раскрытая в ящике форма
# переезжает в него, и порядок форм в документе меняется.
LABELS = ['Demo', 'Other']
FORM_JS = "a => Array.from(document.querySelectorAll('form.open-account-form')).find(f => f.querySelector('[name=\"account_label\"]').value === a)"


@pytest.fixture(scope='module')
def browser():
    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=os.environ.get('ACL_BROWSER_EXECUTABLE') or None)
        yield b
        b.close()


@pytest.fixture(scope='module')
def html():
    return _render({'Demo': [], 'Other': []})


def _job_view(job):
    done = job['status'] == 'done'
    result = [{'success': True, 'publishable': True, 'campaign_name': n, 'error': 'Draft ready'} for n in job['names']]
    return {'status': job['status'], 'publishable': done, 'results': result if done else [],
            'errors': [], 'done_count': len(job['names']) if done else 0, 'total': len(job['names'])}


def _progress_view(jid, job):
    done = job['status'] == 'done'
    ok = {'success': True, 'publishable': True, 'error': 'Draft ready', 'blocking_failures': []}
    return {'job_id': jid, 'account_label': job['account'], 'status': job['status'],
            'active': job['status'] in ACTIVE, 'errors': [], 'stall_after_s': 180, 'idle_s': 1,
            'current': job['names'][0] if job['status'] == 'running' else '',
            'campaigns': [{'name': n, 'result': ok if done else None, 'step': 'create', 'ok': None,
                           'note': '', 'ts': '', 'idle_s': 1, 'blocker': ''} for n in job['names']]}


def _route_for(html, state):
    def route(r):
        path = urlparse(r.request.url).path
        if r.request.resource_type == 'document':
            r.fulfill(content_type='text/html', body=html)
            return
        data = {'files': [], 'texts': [], 'profiles': [], 'waiting': False}
        if path == '/preview-publish':
            f = fields(r.request)
            data = {'plan': [{'Campaign Name': f[f'camp_{i}_name'], 'errors': [], 'warnings': [], '_ads_raw': []}
                             for i in range(int(f['camp_count']))]}
        elif path == '/publish-jobs' and r.request.method == 'POST':
            f = fields(r.request)
            state['posts'].append(f)
            jid = 'job-%s-%d' % (f['account_label'], len(state['posts']))
            state['jobs'][jid] = {'status': 'running', 'account': f['account_label'],
                                  'names': [f[f'camp_{i}_name'] for i in range(int(f['camp_count']))]}
            data = {'job_id': jid}
        elif path.startswith('/publish-jobs/'):
            parts = path.split('/')
            job = state['jobs'].get(parts[2])
            if job is None:
                r.fulfill(status=404, json={'error': 'Job not found.'})
                return
            if parts[-1] == 'cancel':
                state['cancels'].append(parts[2])
                job['status'] = 'cancelled'
                data = {'ok': True}
            elif parts[-1] == 'progress':
                data = _progress_view(parts[2], job)
            else:
                data = _job_view(job)
        r.fulfill(json=data)
    return route


@pytest.fixture
def ui(browser, html):
    context = browser.new_context()
    page = context.new_page()
    errors, dialogs = [], []
    page.on('pageerror', lambda e: errors.append(str(e)))

    def on_dialog(d):
        dialogs.append(d.message)
        d.accept()
    page.on('dialog', on_dialog)
    state = {'posts': [], 'jobs': {}, 'cancels': [], 'dialogs': dialogs}
    page.route('**/*', _route_for(html, state))
    page.goto('http://acl.test/')
    page.evaluate('cbDrawer.open()')
    yield page, state
    assert not errors, errors
    context.close()


def add(page, account=0, count=1):
    page.evaluate('''([account,count]) => {
      const f=(''' + FORM_JS + ''')(['Demo','Other'][account]);
      for(let i=0;i<count;i++) {
        cbAddSlot(f); const s=f.querySelector('.cb-slot:last-child');
        s.querySelector('[name$="_asset"]').value=['Gold','Silver','Crude','Wheat'][account*2+i];
        s.querySelector('[name$="_ad_0_creative_file"]').value='offline-'+i+'.png';
      }
      f.dispatchEvent(new Event('input',{bubbles:true}));
    }''', [account, count])
    page.wait_for_function("document.querySelectorAll('.cbd-camp').length > 0")


def ready(page):
    page.wait_for_function("Array.from(document.querySelectorAll('.cb-slot')).every(s => s._drawerResult && s._drawerResult.state === 'ok')")


def open_card(page, account, slot=0):
    """Раскрыть карточку кампании аккаунта в ящике — её Publish шлёт только её."""
    page.evaluate('''([a,i]) => { const f=(''' + FORM_JS + ''')(['Demo','Other'][a]);
      cbTree.open(f.querySelectorAll('.cb-slot')[i].dataset.cbdId); }''', [account, slot])
    page.wait_for_function('document.querySelector(".cb-in-drawer .cb-publish-btn")')


def press_publish(page, account, slot=0):
    open_card(page, account, slot)
    btn = page.locator('.cb-in-drawer .cb-publish-btn')
    expect(btn).to_be_enabled()
    btn.click()


def publish_one(page, state, account, slot=0):
    before = len(state['posts'])
    press_publish(page, account, slot)
    page.locator('#sfc-ok').click()
    for _ in range(50):
        if len(state['posts']) > before:
            break
        page.wait_for_timeout(100)
    assert len(state['posts']) == before + 1, 'the account was not submitted'


def job_of(state, account):
    return next(j for j, v in state['jobs'].items() if v['account'] == account)


def finish(page, state, account, status='done'):
    state['jobs'][job_of(state, account)]['status'] = status


def panel(page, account):
    return page.locator('form.open-account-form', has=page.locator(
        'input[name="account_label"][value="%s"]' % LABELS[account])).locator('.cb-status-panel')


def dot(page, name):
    return page.locator('.cbd-camp').filter(
        has=page.locator('.cbd-camp-name', has_text=name)).locator('.cbd-ind')


def camp_name(page, account, slot=0):
    return page.evaluate('''([a,i]) => (''' + FORM_JS + ''')(['Demo','Other'][a])
        .querySelectorAll('.cb-slot .cb-name-hidden')[i].value''', [account, slot])


def test_second_account_starts_while_the_first_is_building(ui):
    p, state = ui
    add(p, 0)
    add(p, 1)
    ready(p)
    publish_one(p, state, 0)
    publish_one(p, state, 1)
    assert [f['account_label'] for f in state['posts']] == ['Demo', 'Other']
    assert state['dialogs'] == [], 'the second account was refused with an alert'

    box = p.locator('.cbd-queue')
    expect(box).to_contain_text('Demo — building the draft', timeout=6000)
    expect(box).to_contain_text('Other — building the draft', timeout=6000)
    expect(box).to_contain_text('2 of 2 accounts still going')
    expect(panel(p, 0)).to_contain_text('running')
    expect(panel(p, 1)).to_contain_text('running')

    finish(p, state, 'Demo')
    expect(box).to_contain_text('1 of 2 accounts still going', timeout=6000)
    expect(panel(p, 0)).to_contain_text('draft ready', timeout=6000)
    expect(panel(p, 1)).to_contain_text('running')
    assert p.evaluate('CB_QUEUE.active') is True

    finish(p, state, 'Other')
    expect(box).to_contain_text('Done — 2 of 2 accounts', timeout=6000)
    expect(panel(p, 1)).to_contain_text('draft ready', timeout=6000)
    p.evaluate('cbBuild.refresh()')
    expect(dot(p, camp_name(p, 0))).to_have_class('cbd-ind cbd-ind-build-ok', timeout=6000)
    expect(dot(p, camp_name(p, 1))).to_have_class('cbd-ind cbd-ind-build-ok', timeout=6000)
    assert len(state['posts']) == 2


def test_the_same_account_twice_is_refused_with_a_message(ui):
    p, state = ui
    add(p, 0, count=2)
    ready(p)
    publish_one(p, state, 0, slot=0)
    # Та же кампания и соседняя того же аккаунта: дубля нет, сказано почему.
    for slot in (0, 1):
        press_publish(p, 0, slot)
        p.wait_for_timeout(200)
        expect(p.locator('#sfc-ok')).to_have_count(0)
    assert len(state['posts']) == 1
    assert len(state['dialogs']) == 2
    assert all('Demo is already being built' in m for m in state['dialogs']), state['dialogs']
    # «Publish all», когда кроме идущего аккаунта собирать нечего, — то же.
    p.locator('.cbd-publish-all').click()
    p.wait_for_timeout(200)
    expect(p.locator('#sfc-ok')).to_have_count(0)
    assert len(state['posts']) == 1
    assert 'Demo is already being built' in state['dialogs'][-1]


def test_publish_all_during_a_single_run_queues_the_rest(ui):
    p, state = ui
    add(p, 0)
    add(p, 1)
    ready(p)
    publish_one(p, state, 0)
    btn = p.locator('.cbd-publish-all')
    expect(btn).to_be_enabled()
    btn.click()
    expect(p.locator('#safe-confirm-modal')).to_contain_text('Already being built, skipped')
    expect(p.locator('#safe-confirm-modal')).to_contain_text('Demo')
    p.locator('#sfc-ok').click()
    for _ in range(50):
        if len(state['posts']) == 2:
            break
        p.wait_for_timeout(100)
    assert [f['account_label'] for f in state['posts']] == ['Demo', 'Other']
    box = p.locator('.cbd-queue')
    expect(box).to_contain_text('Other — building the draft', timeout=6000)
    expect(box).to_contain_text('Demo — building the draft')
    finish(p, state, 'Demo')
    finish(p, state, 'Other')
    expect(box).to_contain_text('Done — 2 of 2 accounts', timeout=8000)
    assert state['dialogs'] == []


def test_stop_acts_on_its_own_job_only(ui):
    p, state = ui
    add(p, 0)
    add(p, 1)
    ready(p)
    publish_one(p, state, 0)
    publish_one(p, state, 1)
    stop = panel(p, 0).locator('button[id^="cb-stop-"]')
    expect(stop).to_have_count(1, timeout=6000)
    stop.evaluate('b => b.click()')
    p.wait_for_function('CB_QUEUE.progress.report.length === 1', timeout=8000)
    assert state['cancels'] == [job_of(state, 'Demo')]
    report = p.evaluate('CB_QUEUE.progress.report')
    assert report[0]['account'] == 'Demo' and 'Stopped' in report[0]['why']
    assert p.evaluate('CB_QUEUE.active') is True, 'stopping one account closed the run of the other'
    assert state['jobs'][job_of(state, 'Other')]['status'] == 'running'
    expect(panel(p, 1).locator('button[id^="cb-stop-"]')).to_have_count(1)
    # Остановленный аккаунт можно запустить снова, пока идёт другой.
    publish_one(p, state, 0)
    assert [f['account_label'] for f in state['posts']] == ['Demo', 'Other', 'Demo']


def test_reload_during_two_jobs_restores_both(ui):
    p, state = ui
    add(p, 0)
    add(p, 1)
    ready(p)
    publish_one(p, state, 0)
    publish_one(p, state, 1)
    a, b = camp_name(p, 0), camp_name(p, 1)
    p.reload()
    p.evaluate('cbDrawer.open()')
    box = p.locator('.cbd-queue')
    expect(box).to_contain_text('Demo — building the draft', timeout=8000)
    expect(box).to_contain_text('Other — building the draft', timeout=8000)
    expect(dot(p, a)).to_have_class('cbd-ind cbd-ind-build-run', timeout=6000)
    expect(dot(p, b)).to_have_class('cbd-ind cbd-ind-build-run', timeout=6000)
    expect(panel(p, 0).locator('button[id^="cb-stop-"]')).to_have_count(1, timeout=6000)
    expect(panel(p, 1).locator('button[id^="cb-stop-"]')).to_have_count(1, timeout=6000)

    # После F5 идущий аккаунт тоже не заводит дубль.
    ready(p)
    press_publish(p, 0)
    p.wait_for_timeout(200)
    expect(p.locator('#sfc-ok')).to_have_count(0)
    assert len(state['posts']) == 2
    assert 'Demo is already being built' in state['dialogs'][-1]

    finish(p, state, 'Demo')
    finish(p, state, 'Other')
    expect(box).to_contain_text('Done — 2 of 2 accounts', timeout=8000)


def test_lost_status_keeps_the_reload_hint(ui):
    p, state = ui
    add(p, 0)
    add(p, 1)
    ready(p)
    publish_one(p, state, 0)
    del state['jobs'][job_of(state, 'Demo')]   # дешборд перезапущен — задания нет
    p.wait_for_function('CB_QUEUE.uncertain', timeout=8000)
    open_card(p, 1)
    p.locator('.cb-in-drawer .cb-publish-btn').evaluate('b => cbPublish(b.closest("form"))')
    p.wait_for_timeout(200)
    expect(p.locator('#sfc-ok')).to_have_count(0)
    assert len(state['posts']) == 1
    assert 'Reload the page (F5)' in state['dialogs'][-1]
