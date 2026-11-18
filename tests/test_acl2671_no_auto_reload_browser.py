"""ACL-2671: страница из сохранённой копии не перезагружает себя, когда готов свежий отчёт.

Цифры карты аккаунтов обновляются на месте (`window.aclSwapAccountMap`), «Last updated» меняется,
плашка пропадает. Билдер, его черновики, ящик, взятый на время блок подключения и идущая публикация
не затрагиваются. Полная перезагрузка остаётся единственным случаем (решение владельца R1):
страница отрисована другим кодом, чем работает сервер.
Страница — настоящий шаблон, сервер подменён маршрутами Playwright.
"""
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest
from playwright.sync_api import expect

from test_builder_duplicate_buttons import browser  # noqa: F401 - fixture
from test_builder_multi_publish_browser import (
    FORM_JS, _route_for, add, finish, publish_one, ready,
)
from test_builder_per_account import _render
from src.web.report_store import StoredPage, decorate
from src.web import account_links as links
import _home_overview_site as site

ROOT = Path(__file__).resolve().parent.parent
BUILT_BEFORE = '2026-06-08'  # built_at, с которым test_web_drill рисует страницу
ACC, OTHER = 'Facebook_DQ-214', 'Facebook_DQ-217'
BEFORE = {'Colombia': [(ACC, 117.0, None, '', 0), (OTHER, 5.0, None, '', 0)]}
AFTER = {'Colombia': [(ACC, 0, 'restricted', 'Ad account not in BM', 0), (OTHER, 40.0, None, '', 0)]}
MAP_CSV = ('account_name,ad_account_id,page_id,multilogin_instance,folder_id,profile_id,proxy_type,proxy_host,proxy_port,proxy_user,proxy_pass\n'
           f'{ACC},act_1,1,A,f,prof-214,,,,,\n{OTHER},act_2,2,A,f,,,,,,\n')
# Что человек видит на Home: числа блока страны и итог. Меняются после обновления.
HOME_PROBE = """() => ({block: document.querySelector('#aov .aov-block').innerText,
  totals: document.getElementById('amap-totals').innerText})"""
SWAPPED = "document.getElementById('amap-totals').innerText !== window.__before.totals"
UNUSABLE = '<div class="amap-empty">Could not calculate the selected period.</div>'


@pytest.fixture
def home(monkeypatch, tmp_path):
    """Настоящий Home из _account_map.html: страница «до» и фрагмент «после»."""
    from src.web import app as web
    from src.web.app import templates

    csv = tmp_path / 'map.csv'
    csv.write_text(MAP_CSV, encoding='utf-8')
    monkeypatch.setattr(links, 'get_local_config_dir', lambda: str(tmp_path))
    monkeypatch.setattr(links, 'get_ad_account_map_path', lambda: str(csv))
    monkeypatch.setattr('src.web.app.get_ad_account_map_path', lambda: str(csv))

    def body(spec):
        amap = site.build_amap(web, links, monkeypatch, spec)
        return templates.env.get_template('_account_map.html').render(
            amap=amap, compare=None, compare_fragment_note='', mlx={})
    return {'before': body(BEFORE), 'after': body(AFTER)}


def stored(reasons):
    plain = _render({'Demo': [], 'Other': []})
    return decorate(StoredPage(plain, time.time() - 300, list(reasons)))


class Rig:
    """Поддельный сервер: страница, статус отчёта, фрагмент карты и очередь публикаций."""

    def __init__(self, html, queue_state=None):
        self.html = html
        self.status = {'ready': False, 'building': True, 'error': None, 'built_at': '2026-11-17 12:00',
                       'deferred': False}
        self.fragment = (200, '')
        self.documents = 0
        self.status_calls = 0
        self.fragment_urls = []
        self.progress_calls = 0
        self.inner = _route_for(html, queue_state if queue_state is not None else
                                {'posts': [], 'jobs': {}, 'cancels': []})

    def route(self, r):
        parsed = urlparse(r.request.url)
        path = parsed.path
        if r.request.resource_type == 'document':
            self.documents += 1
            r.fulfill(content_type='text/html', body=self.html)
        elif path.startswith('/static/') and path.endswith(('.js', '.css')):
            f = ROOT / 'src/web' / path.lstrip('/')
            r.fulfill(content_type='application/javascript' if path.endswith('.js') else 'text/css',
                      body=f.read_text(encoding='utf-8') if f.exists() else '')
        elif path == '/report/status':
            self.status_calls += 1
            r.fulfill(json=self.status)
        elif path.startswith('/campaign-inventory/'):
            # Дерево кампаний Home спрашивает статус чтения — отвечаем «ничего не идёт».
            r.fulfill(json={'running': [], 'accounts': {}, 'needs_api': False})
        elif path == '/account-map':
            self.fragment_urls.append(parsed.query)
            r.fulfill(status=self.fragment[0], content_type='text/html', body=self.fragment[1])
        else:
            if path.endswith('/progress'):
                self.progress_calls += 1
            self.inner(r)


def open_page(browser, html, home, url='http://acl.test/', queue_state=None):
    ctx = browser.new_context()
    p = ctx.new_page()
    errors = []
    p.on('pageerror', lambda e: errors.append(str(e) + ' | ' + str(getattr(e, 'stack', ''))[:600]))
    p.on('dialog', lambda d: d.accept())
    rig = Rig(html, queue_state)
    rig.fragment = (200, home['after'])
    p.route('**/*', rig.route)
    p.goto(url)
    p.evaluate('cbDrawer.open()')
    # Home страницы заменяется настоящим Home «до»; билдер и остальная страница — те же.
    p.evaluate("""(body) => { document.getElementById('amap-slot').innerHTML = body;
        if (window.aovApply) window.aovApply(); window.__alive = 1; }""", home['before'])
    p.evaluate(f"window.__before = ({HOME_PROBE})()")
    return ctx, p, rig, errors


def make_ready(rig):
    rig.status = {**rig.status, 'ready': True, 'building': False}


def home_now(p):
    return p.evaluate(HOME_PROBE)


def test_ready_report_updates_numbers_without_reloading(browser, home):
    ctx, p, rig, errors = open_page(browser, stored(['source']), home)
    loads = []
    p.on('load', lambda: loads.append(1))
    assert p.evaluate("document.getElementById('acl-stored-frame') !== null"), 'the OUTDATED frame is the case under test'
    before = home_now(p)
    make_ready(rig)
    p.wait_for_function(SWAPPED, timeout=10000)
    now = home_now(p)
    assert now['totals'] != before['totals'], 'the totals row did not change'
    assert now['block'] != before['block'], 'the country block did not change'
    assert p.evaluate("document.getElementById('acl-built-at').textContent") == '2026-11-17 12:00'
    assert p.evaluate("document.getElementById('acl-stored-bar')") is None
    assert p.evaluate("document.getElementById('acl-stored-frame')") is None, 'the red frame stayed'
    assert p.evaluate('window.__alive') == 1, 'the page was reloaded'
    assert rig.documents == 1 and loads == []
    ctx.close()
    assert not errors, errors


def test_builder_draft_and_running_job_survive_the_refresh(browser, home):
    state = {'posts': [], 'jobs': {}, 'cancels': []}
    ctx, p, rig, errors = open_page(browser, stored([]), home, queue_state=state)
    add(p, 0)
    ready(p)
    name = p.evaluate("(" + FORM_JS + ")('Demo').querySelector('.cb-slot [name$=\"_asset\"]').value")
    p.evaluate("window.__form = (" + FORM_JS + ")('Demo')")
    publish_one(p, state, 0)
    p.wait_for_function('window.CB_QUEUE && CB_QUEUE.active')
    p.evaluate('window.__queue = window.CB_QUEUE')
    before = rig.progress_calls
    make_ready(rig)
    p.wait_for_function(SWAPPED, timeout=10000)
    assert p.evaluate("window.__form === (" + FORM_JS + ")('Demo')"), 'the builder form node was replaced'
    assert p.evaluate("window.__form.querySelector('.cb-slot [name$=\"_asset\"]').value") == name
    assert p.evaluate('window.__queue === window.CB_QUEUE && CB_QUEUE.active') is True
    p.wait_for_timeout(2500)
    assert rig.progress_calls > before, 'the publish job stopped being polled'
    finish(p, state, 'Demo')
    expect(p.locator('.cbd-queue')).to_contain_text('Done', timeout=10000)
    assert rig.documents == 1
    ctx.close()
    assert not errors, errors


def test_borrowed_connect_block_stays_in_the_drawer(browser, home):
    ctx, p, rig, errors = open_page(browser, stored([]), home)
    # То же, что делает borrowConnect: блок уходит в ящик, на его месте остаётся метка.
    found = p.evaluate("""() => {
      const block = document.querySelector('#amap-slot .amap-add');
      if (!block) return false;
      const host = document.createElement('div'); host.className = 'cbd-connect-host';
      document.getElementById('cbd-root').appendChild(host);
      const mark = document.createComment('cbd-connect');
      block.parentNode.insertBefore(mark, block); host.appendChild(block);
      block.setAttribute('data-test-borrowed', '1'); window.__block = block; return true;
    }""")
    assert found, 'the real Home has no connect block to borrow'
    make_ready(rig)
    p.wait_for_function(SWAPPED, timeout=10000)
    assert p.evaluate("window.__block.parentNode.className") == 'cbd-connect-host', 'the block left the drawer'
    assert p.evaluate("document.querySelectorAll('[data-test-borrowed]').length") == 1
    assert p.evaluate("window.__block.isConnected")
    assert rig.documents == 1
    ctx.close()
    assert not errors, errors


def test_fragment_error_changes_nothing_and_does_not_reload(browser, home):
    ctx, p, rig, errors = open_page(browser, stored(['source']), home)
    rig.fragment = (500, 'boom')
    make_ready(rig)
    p.wait_for_timeout(4500)
    assert home_now(p) == p.evaluate('window.__before')
    assert p.evaluate("document.getElementById('acl-stored-bar') !== null")
    assert p.evaluate("document.getElementById('acl-stored-frame') !== null")
    assert p.evaluate("document.getElementById('acl-built-at').textContent") == BUILT_BEFORE
    assert rig.documents == 1 and p.evaluate('window.__alive') == 1
    calls = rig.status_calls
    p.wait_for_timeout(3500)
    assert rig.status_calls == calls, 'polling went on after the failure'
    ctx.close()
    assert not errors, errors


def test_unusable_fragment_changes_nothing_and_does_not_reload(browser, home):
    ctx, p, rig, errors = open_page(browser, stored(['source']), home)
    rig.fragment = (200, UNUSABLE)
    make_ready(rig)
    p.wait_for_timeout(4500)
    assert home_now(p) == p.evaluate('window.__before')
    assert p.evaluate("document.getElementById('acl-stored-bar') !== null")
    assert p.evaluate("document.getElementById('acl-built-at').textContent") == BUILT_BEFORE
    assert rig.documents == 1 and p.evaluate('window.__alive') == 1
    assert len(rig.fragment_urls) == 1
    calls = rig.status_calls
    p.wait_for_timeout(3500)
    assert rig.status_calls == calls, 'polling went on after the unusable answer'
    ctx.close()
    assert not errors, errors


def test_fragment_request_uses_the_page_period(browser, home):
    query = 'date_from=2026-10-01&date_to=2026-10-31&span=custom'
    ctx, p, rig, errors = open_page(browser, stored([]), home, url='http://acl.test/?' + query)
    make_ready(rig)
    p.wait_for_function(SWAPPED, timeout=10000)
    assert rig.fragment_urls == [query]
    ctx.close()
    assert not errors, errors


def test_page_rendered_by_other_code_still_reloads(browser, home):
    ctx, p, rig, errors = open_page(browser, stored(['code', 'source']), home)
    make_ready(rig)
    for _ in range(100):
        if rig.documents >= 2:
            break
        p.wait_for_timeout(100)
    assert rig.documents == 2, 'code differs: the page must reload'
    assert rig.fragment_urls == []
    ctx.close()
    assert not errors, errors


def test_page_rendered_by_the_same_code_does_not_reload(browser, home):
    ctx, p, rig, errors = open_page(browser, stored(['settings']), home)
    make_ready(rig)
    p.wait_for_function(SWAPPED, timeout=10000)
    assert rig.documents == 1 and p.evaluate('window.__alive') == 1
    ctx.close()
    assert not errors, errors
