"""Run the real builder factory, duplicate and targeting handlers offline."""
import asyncio
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import re

import pytest
from jinja2 import Environment
from playwright.sync_api import sync_playwright

REPORT = Path(__file__).resolve().parents[1] / 'src/web/templates/report.html'
sys.path.insert(0, str(REPORT.parents[3]))

@pytest.fixture(scope='module')
def browser():
    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=os.environ.get('ACL_BROWSER_EXECUTABLE') or None)
        yield b
        b.close()

@pytest.fixture
def page(browser):
    src = REPORT.read_text(encoding='utf-8')
    start = src.index('// ── Campaign Builder JS')
    end = src.index('// ── Preview & Publish', start)
    script = src[start:end]
    collect = src[src.index('function cbCollectFormData'):src.index('// A diagnostic belongs')]
    publish = src[src.index('window.cbTrackingWarning'):src.index('function _doCbPublish')]
    esc = src[src.index('    function aclEscHtml'):src.index('\n    }',src.index('    function aclEscHtml'))+6]
    script = Environment().from_string(script).render(
        ref_options_json={'targetings':[{'code':'FX','label':'Forex','fb_interest':'Foreign exchange'},
                                       {'code':'Trading','label':'Trading','fb_interest':'Day trading'}], 'os':['All','iOS']},
        account_alias_map_json={}, account_preflight_json={})
    ctx = browser.new_context()
    ctx.route('**/*', lambda r:r.fulfill(content_type='application/json',body='{"files":[],"texts":[]}'))
    pg = ctx.new_page()
    errors = []
    pg.on('pageerror', lambda e:errors.append(str(e)))
    pg.goto('http://acl.test/')  # real origin for the shipped sessionStorage code
    pg.set_content('<form id="form" data-country="Mexico"><input name="country" value="Mexico"><input name="account_label" value="Demo"><input name="tracking_link" value=""><input name="camp_count" class="cb-count" value="0"><div class="cb-slots"></div></form>')
    pg.add_script_tag(content=esc+'\n'+script+'\n'+collect+'\n'+publish)
    pg.evaluate("window._safeConfirm = (title,body,run,opts) => { window.confirmation = {body,opts}; }; window._doCbPublish = () => { throw Error('No live action in tests'); }; cbAddSlot(document.querySelector('form')); cbAddSlot(document.querySelector('form'))")
    yield pg
    assert not errors, errors
    ctx.close()


def test_changing_selected_targeting_never_submits_previous_interests(page):
    inp = page.locator('.cb-targeting-search').first
    inp.fill('FX')
    page.evaluate('cbTargetingCommit(document.querySelector(".cb-targeting-search"))')
    assert page.locator('.cb-targeting-hidden').first.input_value() == 'FX'
    inp.fill('Tradin')
    assert page.locator('.cb-targeting-hidden').first.input_value() == ''
    page.evaluate('cbPublish(document.querySelector("form"))')
    assert 'Tradin' in page.evaluate('confirmation.body')
    assert 'Targeting not selected' in page.evaluate('confirmation.body')
    inp.fill('Trading')
    data = page.evaluate('Object.fromEntries(cbCollectFormData(document.querySelector("form")))')
    assert data['camp_0_targeting'] == 'Trading'


def test_duplicate_copies_actual_slot_and_ad_fields_and_keeps_the_next_slot(page):
    page.evaluate('''() => {
      const f = document.querySelector('form'), slot=f.querySelector('.cb-slot');
      for (const [k,v] of Object.entries({age:'48',budget:'42',asset:'Demo offer',language:'Spanish',os:'iOS'})) {
        slot.querySelector('[name="camp_0_'+k+'"]').value=v;
      }
      slot.querySelector('.cb-targeting-search').value='FX'; cbTargetingCommit(slot.querySelector('.cb-targeting-search'));
      slot.querySelector('.cb-sel-wrap:has([name="camp_0_os"]) .cb-sel-text').value='iOS';
      slot.querySelector('[name="camp_0_ad_0_creative_file"]').value='folder/example.mp4';
      slot.querySelector('.cb-creative-search').value='example.mp4';
      slot.querySelector('[name="camp_0_ad_0_copy"]').value='Example title';
      slot.querySelector('.cb-adtitle-search').value='Example title';
      // ACL-1344: «Primary text 2» заменил выбор CTA в карточке объявления.
      slot.querySelector('[name="camp_0_ad_0_copy2"]').value='Second title';
      slot.querySelector('.cb-text2-search').value='Second title';
      slot.querySelector('[name="camp_0_ad_0_creative_type"][value="Video"]').checked=true;
      f.querySelector('[name="camp_1_budget"]').value='99';
      cbDuplicateSlot(slot.querySelector('.cb-dup-slot-btn'));
    }''')
    data = page.evaluate('Object.fromEntries(cbCollectFormData(document.querySelector("form")))')
    for field in ('age','budget','asset','language','os','targeting','ad_count','ad_0_creative_file','ad_0_copy','ad_0_copy2','ad_0_creative_type'):
        assert data['camp_1_'+field] == data['camp_0_'+field], field
    assert data['camp_2_budget'] == '99'
    assert data['camp_count'] == '3'
    assert data['camp_1_ad_0_creative_type'] == 'Video'
    assert page.locator('.cb-targeting-search').nth(1).input_value() == 'FX'
    assert page.locator('.cb-creative-search').nth(1).input_value() == 'example.mp4'


def test_publish_warning_and_creative_size_use_the_actual_handlers(page):
    page.evaluate('cbPublish(document.querySelector("form"))')
    assert 'Do not lock the screen' in page.evaluate('confirmation.body')
    assert '1.5 GB' in page.evaluate('cbSizeTag(1500000000)')
    assert 'warn' in page.evaluate('cbSizeTag(1500000000)')
    assert page.evaluate('cbSizeTag(0)') == ''


def test_mouse_selected_targeting_reaches_the_server_library(page, monkeypatch, tmp_path):
    from src.analysis_agent import targeting_registry as tr
    monkeypatch.setattr(tr, "_store", lambda: tmp_path / "targeting.json")
    tr.add("FX", "Foreign exchange")
    inp = page.locator('.cb-targeting-search').first
    inp.fill('FX')
    option = page.locator('.cb-targeting-wrap').first.locator('[data-code="FX"]')
    option.dispatch_event('mousedown')
    data = page.evaluate('Object.fromEntries(cbCollectFormData(document.querySelector("form")))')
    assert data['camp_0_targeting'] == 'FX'
    assert tr.resolve_interests(data['camp_0_targeting']) == 'Foreign exchange'


# ── ACL-1651: список креативов обновляется, новый файл находится без перезагрузки ──
# Настоящий /api/creatives читает временную папку (get_creatives_root), JS Билдера настоящий.

@pytest.fixture
def creatives_env(browser, tmp_path, monkeypatch):
    from src.web import app as webapp
    monkeypatch.setattr(webapp, 'get_creatives_root', lambda: str(tmp_path))
    (tmp_path / '1111_old.jpg').write_bytes(b'x')
    hits = []

    def scan() -> bytes:
        return asyncio.run(webapp.api_creatives()).body

    pool = ThreadPoolExecutor(max_workers=1)

    def route(r):
        if r.request.url.endswith('/api/creatives') or '/api/creatives?' in r.request.url:
            hits.append(1)
            r.fulfill(content_type='application/json', body=pool.submit(scan).result())
        else:
            r.fulfill(content_type='application/json', body='{"files":[],"texts":[]}')

    src = REPORT.read_text(encoding='utf-8')
    start = src.index('// ── Campaign Builder JS')
    end = src.index('// ── Preview & Publish', start)
    esc = src[src.index('    function aclEscHtml'):src.index('\n    }', src.index('    function aclEscHtml')) + 6]
    script = Environment().from_string(src[start:end]).render(
        ref_options_json={'targetings': [], 'os': ['All']}, account_alias_map_json={},
        account_preflight_json={})
    ctx = browser.new_context()
    ctx.route('**/*', route)
    pg = ctx.new_page()
    errors = []
    pg.on('pageerror', lambda e: errors.append(str(e)))
    pg.goto('http://acl.test/')
    pg.set_content('<form id="form" data-country="Mexico"><input name="country" value="Mexico">'
                   '<input name="account_label" value="Demo"><input name="tracking_link" value="">'
                   '<input name="camp_count" class="cb-count" value="0"><div class="cb-slots"></div></form>')
    pg.add_script_tag(content=esc + '\n' + script)
    pg.evaluate("cbAddSlot(document.querySelector('form'))")
    pg.wait_for_function('window.CB_CREATIVES_REFRESH_COUNT >= 1')
    pg.wait_for_function('document.querySelector(".cb-slot") !== null')
    pg.wait_for_timeout(300)
    yield pg, tmp_path, hits
    assert not errors, errors
    ctx.close()
    pool.shutdown()


def _options(pg):
    return pg.locator('.cb-sel-dropdown:not([hidden]) .cb-sel-opt').all_inner_texts()


def _options(pg):
    return pg.locator('.cb-sel-dropdown:not([hidden]) .cb-sel-opt').all_inner_texts()


def _focus_and_settle(pg, box, hits):
    """Фокус в поиске делает свой запрос; ждём, пока он отработает."""
    pg.evaluate("cbCreativesAge(1e12)")
    n = len(hits)
    pg.locator('body').click()
    box.focus()
    pg.wait_for_function('window.CB_CREATIVES_REFRESH_COUNT >= %d' % (n + 1), timeout=5000)
    pg.wait_for_timeout(300)
    assert len(hits) == n + 1


def test_creatives_new_file_found_on_focus_and_deleted_file_disappears(creatives_env):
    pg, folder, hits = creatives_env
    box = pg.locator('.cb-creative-search').first
    pg.evaluate("cbCreativesAge(60000)")
    (folder / '2222_new.jpg').write_bytes(b'x')
    box.fill('2222')  # fill() ставит фокус: фокусный запрос приносит новый файл
    pg.wait_for_function('document.querySelector(".cb-sel-dropdown:not([hidden])")'
                         ' && document.querySelector(".cb-sel-dropdown:not([hidden])").textContent.includes("2222_new.jpg")',
                         timeout=5000)
    box.fill('1111')
    pg.wait_for_function('document.querySelector(".cb-sel-dropdown:not([hidden]) .cb-sel-opt")', timeout=5000)
    assert any('1111_old.jpg' in t for t in _options(pg))
    (folder / '1111_old.jpg').unlink()
    pg.evaluate("cbCreativesAge(1e12)")
    pg.locator('body').click()
    box.focus()
    pg.wait_for_function('!document.querySelector(".cb-sel-dropdown:not([hidden])")'
                         ' || !document.querySelector(".cb-sel-dropdown:not([hidden])").textContent.includes("1111_old.jpg")',
                         timeout=5000)


def test_creatives_miss_refreshes_once_per_streak(creatives_env):
    pg, folder, hits = creatives_env
    box = pg.locator('.cb-creative-search').first
    _focus_and_settle(pg, box, hits)
    # Фокусный запрос был 3 с назад: выше порога промаха (2 с), ниже фокусного (10 с).
    # Значит новый запрос может дать только ветка промаха.
    (folder / 'zz_new.jpg').write_bytes(b'x')
    pg.evaluate("cbCreativesAge(3000)")
    n = len(hits)
    box.press_sequentially('zz', delay=30)
    pg.wait_for_function('document.querySelector(".cb-sel-dropdown:not([hidden])")'
                         ' && document.querySelector(".cb-sel-dropdown:not([hidden])").textContent.includes("zz_new.jpg")',
                         timeout=5000)
    assert len(hits) == n + 1
    box.fill('')
    pg.evaluate("cbCreativesAge(3000)")
    n = len(hits)
    box.press_sequentially('qq', delay=30)
    pg.wait_for_timeout(400)
    assert len(hits) == n + 1
    pg.evaluate("cbCreativesAge(3000)")  # даже за порогом: серия промахов не повторяет запрос
    box.press_sequentially('qqq', delay=30)
    pg.wait_for_timeout(400)
    assert len(hits) == n + 1
    box.fill('1111')  # попадание сбрасывает защёлку
    pg.evaluate("cbCreativesAge(3000)")
    box.fill('qq')
    pg.wait_for_timeout(400)
    assert len(hits) == n + 2


def test_creatives_focus_refresh_is_throttled(creatives_env):
    pg, folder, hits = creatives_env
    box = pg.locator('.cb-creative-search').first
    pg.evaluate("cbCreativesAge(1e12)")
    n = len(hits)
    for _ in range(4):
        pg.locator('body').click()
        box.focus()
        pg.wait_for_timeout(80)
    pg.wait_for_timeout(300)
    assert len(hits) == n + 1  # четыре фокуса в пределах 10 с = один запрос
    pg.evaluate("cbCreativesAge(10500)")
    pg.locator('body').click()
    box.focus()
    pg.wait_for_timeout(300)
    assert len(hits) == n + 2


def test_replacement_photo_is_found_after_immediate_post_refresh_miss(creatives_env):
    pg, folder, hits = creatives_env
    box = pg.locator('.cb-creative-search').first
    _focus_and_settle(pg, box, hits)
    box.fill('1111')
    pg.locator('.cb-sel-dropdown:not([hidden]) .cb-sel-opt').first.dispatch_event('mousedown')
    assert pg.locator('[name="camp_0_ad_0_creative_file"]').input_value() == '1111_old.jpg'
    pg.locator('[name="camp_0_budget"]').fill('42')
    # Replace the selected photo immediately after a successful refresh. Unlike
    # the older miss test, do not age the clock past the throttle window.
    (folder / 'zz_replacement.jpg').write_bytes(b'x')
    n = len(hits)
    box.fill('zz')
    box.press_sequentially('_replacement', delay=30)
    pg.wait_for_function('document.querySelector(".cb-sel-dropdown:not([hidden])")'
                         ' && document.querySelector(".cb-sel-dropdown:not([hidden])").textContent.includes("zz_replacement.jpg")',
                         timeout=5000)
    assert len(hits) == n + 1
    pg.locator('.cb-sel-dropdown:not([hidden]) .cb-sel-opt').first.dispatch_event('mousedown')
    assert pg.locator('[name="camp_0_ad_0_creative_file"]').input_value() == 'zz_replacement.jpg'
    assert pg.locator('[name="camp_0_budget"]').input_value() == '42'
    assert pg.locator('[name="camp_0_ad_0_creative_type"][value="Image"]').is_checked()
    assert pg.locator('.cb-sel-dropdown:not([hidden])').count() == 0


def test_pending_miss_refresh_does_not_reopen_closed_dropdown(creatives_env):
    pg, folder, hits = creatives_env
    box = pg.locator('.cb-creative-search').first
    _focus_and_settle(pg, box, hits)
    (folder / 'zz_replacement.jpg').write_bytes(b'x')
    n = len(hits)
    box.fill('zz_replacement')
    pg.locator('[name="camp_0_budget"]').click()
    pg.locator('[name="camp_0_budget"]').fill('42')
    assert pg.locator('.cb-sel-dropdown:not([hidden])').count() == 0
    pg.wait_for_function('window.CB_CREATIVES_REFRESH_COUNT >= %d' % (n + 1), timeout=5000)
    pg.wait_for_timeout(300)
    assert len(hits) == n + 1
    assert pg.locator('.cb-sel-dropdown:not([hidden])').count() == 0
    assert pg.locator('[name="camp_0_budget"]').input_value() == '42'


def test_immediate_miss_refreshes_without_further_typing(creatives_env):
    pg, folder, hits = creatives_env
    box = pg.locator('.cb-creative-search').first
    _focus_and_settle(pg, box, hits)
    (folder / 'zz_immediate.jpg').write_bytes(b'x')
    n = len(hits)
    box.fill('zz_immediate')
    pg.wait_for_function('document.querySelector(".cb-sel-dropdown:not([hidden])")'
                         ' && document.querySelector(".cb-sel-dropdown:not([hidden])").textContent.includes("zz_immediate.jpg")',
                         timeout=5000)
    assert len(hits) == n + 1


def test_creatives_list_change_keeps_closed_dropdown_and_unchanged_previews(creatives_env):
    pg, folder, hits = creatives_env
    box = pg.locator('.cb-creative-search').first
    pg.evaluate("""() => {
      const row = document.querySelector('.cb-ad-row');
      row.querySelector('[name$="_creative_file"]').value = '1111_old.jpg';
      row.querySelector('.cb-creative-search').value = '1111_old.jpg';
      row.querySelector('.cb-creative-search').dispatchEvent(new Event('input', {bubbles: true}));
    }""")
    pg.wait_for_selector('.cbp-media', state='attached', timeout=5000)
    box.fill('1111')
    pg.wait_for_function('document.querySelector(".cb-sel-dropdown:not([hidden]) .cb-sel-opt")', timeout=5000)
    pg.evaluate("document.querySelector('.cb-sel-dropdown:not([hidden]) .cb-sel-opt').dispatchEvent(new MouseEvent('mousedown', {bubbles: true, cancelable: true}))")
    assert pg.locator('.cb-sel-dropdown:not([hidden])').count() == 0
    pg.wait_for_timeout(400)  # отложенная перерисовка от самого ввода уже прошла
    pg.evaluate("document.querySelector('.cbp-media').dataset.mark = 'same'")
    (folder / '3333_new.jpg').write_bytes(b'x')
    pg.evaluate("cbCreativesAge(1e12)")
    n = len(hits)
    pg.evaluate("window.dispatchEvent(new Event('focus'))")
    pg.wait_for_function('window.CB_CREATIVES_REFRESH_COUNT >= %d' % (n + 1), timeout=5000)
    pg.wait_for_timeout(400)
    assert pg.locator('.cb-sel-dropdown:not([hidden])').count() == 0  # закрытый список не открываем
    assert pg.locator('.cbp-media').first.get_attribute('data-mark') == 'same'  # чужой файл: превью не трогаем


# ── ACL-1668: выбранный креатив удалили/переименовали — строка помечена, публикация отказывает ──

@pytest.fixture
def missing_env(browser, tmp_path, monkeypatch):
    from src.web import app as webapp
    monkeypatch.setattr(webapp, 'get_creatives_root', lambda: str(tmp_path))
    (tmp_path / '1111_old.jpg').write_bytes(b'x')
    (tmp_path / '2222_keep.jpg').write_bytes(b'x')
    pool = ThreadPoolExecutor(max_workers=1)

    def route(r):
        if r.request.url.split('?')[0].endswith('/api/creatives'):
            body = pool.submit(lambda: asyncio.run(webapp.api_creatives()).body).result()
            r.fulfill(content_type='application/json', body=body)
        else:
            r.fulfill(content_type='application/json', body='{"files":[],"texts":[]}')

    src = REPORT.read_text(encoding='utf-8')
    start = src.index('// ── Campaign Builder JS')
    end = src.index('// ── Preview & Publish', start)
    esc = src[src.index('    function aclEscHtml'):src.index('\n    }', src.index('    function aclEscHtml')) + 6]
    script = Environment().from_string(src[start:end]).render(
        ref_options_json={'targetings': [], 'os': ['All']}, account_alias_map_json={},
        account_preflight_json={})
    pub_all = src[src.index('window.cbPublishAll = '):src.index('/* ── ACL-1819: серверные проверки')]
    helpers = src[src.index('function cbSlotTitle'):src.index('/* Все аккаунты уходят на сервер СРАЗУ')]
    publish = src[src.index('window.cbTrackingWarning'):src.index('function _doCbPublish')]
    ctx = browser.new_context()
    ctx.route('**/*', route)
    pg = ctx.new_page()
    errors = []
    pg.on('pageerror', lambda e: errors.append(str(e)))
    pg.goto('http://acl.test/')
    pg.set_content('<form class="open-account-form" id="form" data-country="Mexico"><input name="country" value="Mexico">'
                   '<input name="account_label" value="Demo"><input name="tracking_link" value="">'
                   '<input name="camp_count" class="cb-count" value="0"><div class="cb-slots"></div></form>')
    pg.add_script_tag(content=esc + '\n' + script + '\n' + helpers + '\n' + pub_all + '\n' + publish)
    pg.evaluate("window._safeConfirm = (t,b,run,o) => { window.confirmation = {body:b, run}; };"
                "window.alerts = []; window.alert = m => window.alerts.push(m);"
                "window._doCbPublish = () => { window.sent = true; };"
                "window._runPublishQueue = () => { window.sent = true; return {skipped: []}; };"
                "cbAddSlot(document.querySelector('form'))")
    pg.wait_for_function('window.CB_CREATIVES_REFRESH_COUNT >= 1')
    pg.wait_for_timeout(300)
    yield pg, tmp_path
    assert not errors, errors
    ctx.close()
    pool.shutdown()


def _pick_file(pg, name):
    pg.evaluate("""n => { const s = document.querySelector('.cb-slot');
      s.querySelector('[name$="_creative_file"]').value = n;
      s.querySelector('.cb-creative-search').value = n; }""", name)


def test_deleted_chosen_creative_is_marked_and_publish_is_refused_with_row_named(missing_env):
    pg, folder = missing_env
    _pick_file(pg, '1111_old.jpg')
    pg.evaluate("cbCreativesAge(1e12); cbRefreshCreatives(0)")
    pg.wait_for_timeout(400)
    assert pg.locator('.cb-file-missing-note').count() == 0
    (folder / '1111_old.jpg').unlink()
    pg.evaluate("cbCreativesAge(1e12)")
    pg.evaluate("cbRefreshCreatives(0)")
    pg.wait_for_selector('.cb-file-missing-note', timeout=5000)
    note = pg.locator('.cb-file-missing-note')
    assert note.inner_text() == 'File missing \u2014 pick again'
    assert 'color:var(--bad)' in note.get_attribute('style').replace(' ', '')  # error colour of the theme
    assert pg.locator('.cb-creative-search').input_value() == '1111_old.jpg'  # имя остаётся
    # Publish (одна форма): подтверждение называет строку, подтверждение ничего не шлёт.
    pg.evaluate('cbPublish(document.querySelector("form"))')
    body = pg.evaluate('confirmation.body')
    assert 'File missing' in body and 'Ad 1' in body and '1111_old.jpg' in body
    pg.evaluate('confirmation.run()')
    assert pg.evaluate('window.sent') is None and pg.evaluate('alerts.length') == 1
    # Publish all: то же самое.
    pg.evaluate('window.confirmation = null; cbPublishAll(null)')
    body = pg.evaluate('confirmation.body')
    assert 'File missing' in body and '1111_old.jpg' in body
    pg.evaluate('confirmation.run()')
    assert pg.evaluate('window.sent') is None and pg.evaluate('alerts.length') == 2
    # Выбрали файл заново — пометка уходит, публикация не отказывает.
    _pick_file(pg, '2222_keep.jpg')
    pg.evaluate("cbCreativesAge(1e12); cbRefreshCreatives(0)")
    pg.wait_for_function('!document.querySelector(".cb-file-missing-note")', timeout=5000)
    pg.evaluate('window.confirmation = null; cbPublish(document.querySelector("form"))')
    assert 'File missing' not in pg.evaluate('confirmation.body')
    pg.evaluate('confirmation.run()')
    assert pg.evaluate('window.sent') is True


def test_unlisted_chosen_file_is_marked_once_the_list_is_loaded(missing_env):
    pg, folder = missing_env
    _pick_file(pg, 'typed-but-never-listed.jpg')
    pg.evaluate("cbCreativesAge(1e12); cbRefreshCreatives(0)")
    pg.wait_for_timeout(400)
    # Выбранного нет в папке, а список загружен: помечено. Без загрузки списка молчим.
    assert pg.locator('.cb-file-missing-note').count() == 1
    assert pg.evaluate('cbMissingCreatives(document).length') == 1
