"""Точка сборки у имени кампании в ящике билдера (ACL-1315) — на настоящем шаблоне.

Страница рендерится целиком, сеть подменена: `/publish-jobs/{id}/progress`
отдаёт то, что задаёт тест. Проверяются четыре состояния точки (серая с
пульсом, оранжевая с пульсом, оранжевая ровная, зелёная), встающая
кампания, заблокированный аккаунт, задание, потерянное после перезапуска
дешборда (404), и отказ от пульса при `prefers-reduced-motion`. ACL-1321:
задание в очереди профилей — неподвижное кольцо с точкой и местом в
очереди, а список «Publish all» внизу ящика пишет «queued».
"""
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest
from playwright.sync_api import expect, sync_playwright
from test_builder_per_account import _render
from test_drawer_review_browser import fields

JOB = 'offline-job-1'


@pytest.fixture(scope='module')
def browser():
    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=os.environ.get('ACL_BROWSER_EXECUTABLE') or None)
        yield b
        b.close()


@pytest.fixture(scope='module')
def html():
    return _render({'Demo': [], 'Other': []})


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
            state['posts'] += 1
            data = {'job_id': JOB}
        elif path.endswith('/progress'):
            state['progress_calls'] += 1
            own = (state.get('by_job') or {}).get(path.split('/')[-2])
            if own is not None:
                r.fulfill(json=own)
                return
            if state['progress'] is None:
                r.fulfill(status=404, json={'error': 'Job not found.'})
                return
            data = state['progress']
        elif path.startswith('/publish-jobs/'):
            if state['progress'] is None:
                r.fulfill(status=404, json={'error': 'Job not found.'})
                return
            data = {'status': state['progress']['status'], 'results': [], 'errors': [],
                    'queue': state['progress'].get('queue')}
        r.fulfill(json=data)
    return route


@pytest.fixture
def ui(browser, html):
    context = browser.new_context()
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    state = {'posts': 0, 'progress_calls': 0, 'progress': {'status': 'queued', 'campaigns': []}}
    page.route('**/*', _route_for(html, state))
    page.goto('http://acl.test/')
    page.evaluate('cbDrawer.open()')
    yield page, state
    assert not errors, errors
    context.close()


def add(page, account=0, count=1):
    page.evaluate('''([account,count]) => {
      const f=document.querySelectorAll('form.open-account-form')[account];
      for(let i=0;i<count;i++) {
        cbAddSlot(f); const s=f.querySelector('.cb-slot:last-child');
        s.querySelector('[name$="_asset"]').value=['Gold','Silver','Crude','Wheat'][account*2+i];
        s.querySelector('[name$="_ad_0_creative_file"]').value='offline-'+i+'.png';
      }
      f.dispatchEvent(new Event('input',{bubbles:true}));
    }''', [account, count])
    page.wait_for_function("document.querySelectorAll('.cbd-camp').length > 0")


def names(page, account=0):
    return page.evaluate('''a => Array.from(document.querySelectorAll('form.open-account-form')[a]
        .querySelectorAll('.cb-slot .cb-name-hidden')).map(x => x.value)''', account)


def publish_demo(page, count=2):
    """Две кампании аккаунта Demo уходят в одно задание через «Publish all»."""
    add(page, count=count)
    page.wait_for_function("Array.from(document.querySelectorAll('.cb-slot')).every(s => s._drawerResult && s._drawerResult.state === 'ok')")
    page.locator('.cbd-publish-all').click()
    page.locator('#sfc-ok').click()
    page.wait_for_function('Object.keys(cbBuild.jobs()).length === 1')
    got = names(page)
    assert len(got) == count and len(set(got)) == count and all(got)
    return got


def camp(name, **kw):
    c = {'name': name, 'result': None, 'step': '', 'ok': None, 'note': '', 'ts': '', 'idle_s': None, 'blocker': ''}
    c.update(kw)
    return c


def progress(status, campaigns, *, current='', idle_s=None, errors=()):
    return {'job_id': JOB, 'account_label': 'Demo', 'status': status,
            'active': status in ('pending', 'queued', 'running', 'cancelling'),
            'errors': list(errors), 'stall_after_s': 180, 'idle_s': idle_s,
            'current': current, 'campaigns': campaigns}


def show(page, state, payload):
    state['progress'] = payload
    page.evaluate('cbBuild.refresh()')


def dot(page, name):
    """Индикатор строки кампании с этим именем."""
    return page.locator('.cbd-camp').filter(
        has=page.locator('.cbd-camp-name', has_text=name)).locator('.cbd-ind')


def animation(page, name):
    # Дерево ящика перерисовывается само (repaintSoon): узел, найденный за миг
    # до перерисовки, уже вне документа, и стиль у него пустой. Спросить снова.
    for _ in range(20):
        got = dot(page, name).locator('span').evaluate(
            'e => e.isConnected ? getComputedStyle(e).animationName : ""')
        if got:
            return got
        page.wait_for_timeout(100)
    return got


OK = {'success': True, 'publishable': True, 'error': 'Draft ready', 'blocking_failures': []}
BLOCKED = {'success': True, 'publishable': False, 'error': 'Draft exists',
           'blocking_failures': ['ad2_creative_upload']}


def test_building_then_finished_clean_and_with_errors(ui):
    p, state = ui
    a, b = publish_demo(p)
    # Не отправленная ни в одно задание кампания сохраняет прежний значок.
    add(p, account=1)
    other = names(p, 1)[0]

    show(p, state, progress('running', [camp(a, step='ad_copy', idle_s=12), camp(b)], current=a, idle_s=12))
    expect(dot(p, a)).to_have_class('cbd-ind cbd-ind-build-run')
    expect(dot(p, a)).to_have_attribute('title', 'Building: ad copy')
    expect(dot(p, b)).to_have_class('cbd-ind cbd-ind-build-run')
    expect(dot(p, b)).to_have_attribute('title', 'Waiting for its turn')
    assert animation(p, a) == 'cbd-build-pulse'
    expect(dot(p, other)).not_to_have_class('cbd-ind cbd-ind-build-run')
    assert 'build' not in dot(p, other).get_attribute('class')

    show(p, state, progress('done', [camp(a, result=OK), camp(b, result=BLOCKED)]))
    expect(dot(p, a)).to_have_class('cbd-ind cbd-ind-build-ok')
    expect(dot(p, a)).to_have_attribute('title', 'Draft ready in Ads Manager — Publish was not clicked')
    expect(dot(p, b)).to_have_class('cbd-ind cbd-ind-build-bad')
    expect(dot(p, b)).to_have_attribute('title', 'Do not publish: ad2_creative_upload')
    assert animation(p, a) == 'none' and animation(p, b) == 'none'
    # Законченное задание больше не опрашивается.
    calls = state['progress_calls']
    p.wait_for_timeout(3500)
    assert state['progress_calls'] == calls


def test_a_stalled_campaign_pulses_orange_with_its_last_step(ui):
    p, state = ui
    a, b = publish_demo(p)
    show(p, state, progress('running', [camp(a, step='ad2_creative_upload', idle_s=179), camp(b)],
                            current=a, idle_s=179))
    expect(dot(p, a)).to_have_class('cbd-ind cbd-ind-build-run')

    show(p, state, progress('running', [camp(a, step='ad2_creative_upload', idle_s=200), camp(b)],
                            current=a, idle_s=200))
    expect(dot(p, a)).to_have_class('cbd-ind cbd-ind-build-attn')
    expect(dot(p, a)).to_have_attribute(
        'title', 'No new step for 3 min (last: ad2 creative upload) — check the Multilogin window')
    assert animation(p, a) == 'cbd-build-pulse'
    # Ждущая своей очереди кампания не «встала» — она просто ждёт.
    expect(dot(p, b)).to_have_class('cbd-ind cbd-ind-build-run')


def test_a_blocked_account_pulses_orange_while_the_job_runs_then_turns_solid(ui):
    p, state = ui
    a, b = publish_demo(p)
    why = 'Account restricted by Meta — complete the verification in Ads Manager'
    failed = {'success': False, 'publishable': False, 'error': 'Recorded flow failed at: account_blocked',
              'blocking_failures': ['account_blocked']}
    show(p, state, progress('running', [camp(a, result=failed, blocker=why), camp(b, step='create', idle_s=5)],
                            current=b, idle_s=5))
    expect(dot(p, a)).to_have_class('cbd-ind cbd-ind-build-attn')
    expect(dot(p, a)).to_have_attribute('title', why)
    assert animation(p, a) == 'cbd-build-pulse'

    show(p, state, progress('done', [camp(a, result=failed, blocker=why), camp(b, result=OK)]))
    expect(dot(p, a)).to_have_class('cbd-ind cbd-ind-build-bad')
    expect(dot(p, a)).to_have_attribute('title', 'Do not publish: account_blocked')
    expect(dot(p, b)).to_have_class('cbd-ind cbd-ind-build-ok')


def test_a_job_lost_after_a_restart_stops_pulsing_even_after_reload(ui):
    p, state = ui
    a, b = publish_demo(p)
    show(p, state, progress('running', [camp(a, step='create', idle_s=3), camp(b)], current=a, idle_s=3))
    expect(dot(p, a)).to_have_class('cbd-ind cbd-ind-build-run')

    show(p, state, None)   # дешборд перезапущен — задания больше нет
    for name in (a, b):
        expect(dot(p, name)).to_have_class('cbd-ind cbd-ind-build-lost')
        assert animation(p, name) == 'none'
    assert 'unknown' in dot(p, a).get_attribute('title')

    # После F5 точка помнится, но не оживает: сервер по-прежнему не знает задания.
    p.reload()
    p.evaluate('cbDrawer.open()')
    expect(dot(p, a)).to_have_class('cbd-ind cbd-ind-build-lost')
    assert animation(p, a) == 'none'


def test_a_known_job_comes_back_after_reload(ui):
    p, state = ui
    a, b = publish_demo(p)
    show(p, state, progress('running', [camp(a, step='create'), camp(b)], current=a, idle_s=1))
    expect(dot(p, a)).to_have_class('cbd-ind cbd-ind-build-run')
    state['progress'] = progress('done', [camp(a, result=OK), camp(b, result=OK)])
    p.reload()
    p.evaluate('cbDrawer.open()')
    expect(dot(p, a)).to_have_class('cbd-ind cbd-ind-build-ok')
    expect(dot(p, b)).to_have_class('cbd-ind cbd-ind-build-ok')


def test_reduced_motion_shows_a_ring_instead_of_a_pulse(browser, html):
    context = browser.new_context(reduced_motion='reduce')
    page = context.new_page()
    state = {'posts': 0, 'progress_calls': 0,
             'progress': {'status': 'queued', 'campaigns': []}}
    page.route('**/*', _route_for(html, state))
    page.goto('http://acl.test/')
    page.evaluate('cbDrawer.open()')
    try:
        a, b = publish_demo(page)
        show(page, state, progress('running', [camp(a, step='create'), camp(b)], current=a, idle_s=1))
        expect(dot(page, a)).to_have_class('cbd-ind cbd-ind-build-run')
        span = dot(page, a).locator('span')
        # Ждущие проверки: узел мог уйти при перерисовке ящика (см. animation()).
        expect(span).to_have_css('animation-name', 'none')
        expect(span).not_to_have_css('box-shadow', 'none')
    finally:
        context.close()


def test_same_named_campaigns_of_two_accounts_keep_their_own_dots(ui):
    """D6: ключ точки — аккаунт и имя. Задание другого аккаунта с тем же
    именем кампании (и свежее) не красит строку аккаунта Demo."""
    p, state = ui
    a, b = publish_demo(p)
    state['by_job'] = {
        JOB: progress('running', [camp(a, step='ad_copy', idle_s=5), camp(b)], current=a, idle_s=5),
        'other-job': dict(progress('done', [camp(a, result=BLOCKED)]), job_id='other-job', account_label='Other'),
    }
    p.evaluate('([n]) => cbBuild.watch("other-job", "Other", [n])', [a])
    p.evaluate('cbBuild.refresh()')
    p.wait_for_timeout(500)
    p.evaluate('cbBuild.refresh()')
    expect(dot(p, a)).to_have_class('cbd-ind cbd-ind-build-run')
    expect(dot(p, a)).to_have_attribute('title', 'Building: ad copy')


# ── ACL-1321: задание ждёт места в очереди профилей ──────────────────────

def queued(campaigns, *, reason='slot', ahead=0):
    p = progress('queued', campaigns)
    p['queue'] = {'reason': reason, 'ahead': ahead, 'limit': 2, 'running': 2, 'available_mb': 1500}
    return p


def test_a_queued_job_shows_a_still_ring_with_its_place_in_line(ui):
    p, state = ui
    a, b = publish_demo(p)
    show(p, state, queued([camp(a), camp(b)], ahead=2))
    for name in (a, b):
        expect(dot(p, name)).to_have_class('cbd-ind cbd-ind-build-wait')
        expect(dot(p, name)).to_have_attribute('title', 'Waiting for a free slot (2 ahead)')
        assert animation(p, name) == 'none', 'ждущее задание дышит, будто робот уже работает'
    span = dot(p, a).locator('span')
    # Не «потеряно»: у потерянного пустое кольцо, у ждущего — кольцо с точкой.
    # Ящик перерисовывается сам (repaintSoon): узел, найденный за миг до
    # перерисовки, уже вне документа и отдаёт пустой стиль (CI 3.13, PR #328) —
    # поэтому ждущие проверки expect, они перечитывают узел.
    expect(span).to_have_css('background-image', re.compile('radial-gradient'))
    expect(span).to_have_css('border-top-style', 'solid')

    show(p, state, queued([camp(a), camp(b)], reason='profile'))
    expect(dot(p, a)).to_have_attribute('title', 'Waiting for the Multilogin profile — another run is using it')

    # Место дали — точка задышала.
    show(p, state, progress('running', [camp(a, step='create', idle_s=2), camp(b)], current=a, idle_s=2))
    expect(dot(p, a)).to_have_class('cbd-ind cbd-ind-build-run')
    assert animation(p, a) == 'cbd-build-pulse'


def test_a_job_held_by_low_memory_says_how_much_is_free_and_what_a_profile_uses(ui):
    """ACL-1424: причина ожидания — свободная память и вес профиля."""
    p, state = ui
    a, b = publish_demo(p)
    held = queued([camp(a), camp(b)])
    held['queue'].update(hold='memory_low', available_mb=int(5.4 * 1024), profile_mb=int(3.5 * 1024),
                         reserve_mb=3 * 1024, need_mb=int(6.5 * 1024))
    show(p, state, held)
    expect(dot(p, a)).to_have_class('cbd-ind cbd-ind-build-wait')
    expect(dot(p, a)).to_have_attribute('title', 'Waiting for memory: 5.4 GB free, needs 6.5 GB (profile ~3.5 GB + 3 GB reserve)')


def test_the_publish_all_list_says_queued_and_then_building(ui):
    p, state = ui
    publish_demo(p)
    show(p, state, queued([], ahead=1))
    box = p.locator('.cbd-queue')
    expect(box).to_contain_text('Demo — queued — waiting for a free slot (1 ahead)', timeout=6000)
    expect(box.locator('.cbd-q-queued')).to_have_count(1)
    show(p, state, progress('running', [], idle_s=1))
    expect(box).to_contain_text('Demo — building the draft', timeout=6000)
    expect(box.locator('.cbd-q-queued')).to_have_count(0)
