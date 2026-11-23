"""Пиксель кампании выбирается в билдере, как Страница: план, эндпоинт, форма, поле."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_builder_review_browser import browser, page  # noqa: E402,F401

from src.fb_api.creator import AccountFacts, PlanError, resolve_pixel

ACT = 'act_1234567890'


def _plan(pixel='', source='', *, instant=False):
    return SimpleNamespace(pixel_id=pixel, pixel_source=source,
                           ad_sets=[SimpleNamespace(name='Set', pixel_id='', instant=instant)])


def _facts(pixels):
    return AccountFacts(ACT, 'USD', ['123'], list(pixels))


# ───────────────────────── resolve_pixel ─────────────────────────

def test_no_pixel_on_account_names_the_pixel_field():
    with pytest.raises(PlanError, match='no pixel/dataset') as e:
        resolve_pixel(_plan(), _facts([]))
    assert 'Pixel field' in str(e.value)


def test_one_pixel_automatic_works_as_before():
    plan = _plan()
    resolve_pixel(plan, _facts(['222']))
    assert (plan.pixel_id, plan.pixel_source) == ('222', 'account default')


def test_two_pixels_without_choice_asks_to_choose_in_the_pixel_field():
    with pytest.raises(PlanError) as e:
        resolve_pixel(_plan(), _facts(['222', '333']))
    assert 'has 2 pixels/datasets' in str(e.value)
    assert 'Choose one in the Pixel field' in str(e.value)
    assert 'needs exactly one' not in str(e.value)


def test_chosen_pixel_is_kept():
    plan = _plan('333', 'draft')
    resolve_pixel(plan, _facts(['222', '333']))
    assert (plan.pixel_id, plan.pixel_source) == ('333', 'draft')


def test_chosen_pixel_not_on_account_is_refused_without_substitution():
    plan = _plan('999', 'draft')
    with pytest.raises(PlanError, match=r'pixel from the draft \(999\) is not on this ad account'):
        resolve_pixel(plan, _facts(['222', '333']))
    assert plan.pixel_id == '999'


def test_instant_form_only_needs_no_pixel():
    plan = _plan(instant=True)
    resolve_pixel(plan, _facts(['222', '333']))
    assert plan.pixel_id == ''


# ───────────────────────── список пикселей ─────────────────────────

def test_pixel_discovery_reads_id_and_name_in_order():
    from src.web.account_pixels import pixels_from_client
    calls = []
    client = SimpleNamespace(get_all=lambda path, params, **k: calls.append((path, params, k)) or [
        {'id': '333', 'name': 'Main'}, {'id': '222'}, {'name': 'no id'}])
    assert pixels_from_client(client, ACT) == [{'id': '333', 'name': 'Main'}, {'id': '222', 'name': '222'}]
    assert calls[0][0] == f'/{ACT}/adspixels' and calls[0][1]['fields'] == 'id,name'


def test_read_in_session_is_read_only_and_inside_the_profile(monkeypatch):
    from src.web import account_pixels as px
    client = SimpleNamespace(allow_writes=False, reset_budget=lambda n: None,
                             get_all=lambda *a, **k: [{'id': '1', 'name': 'A'}, {'id': '2', 'name': 'B'}])
    sess = SimpleNamespace(client=client, act=ACT)
    out = px._read_in_session(sess, 'Demo')
    assert out['state'] == 'ready' and [p['id'] for p in out['pixels']] == ['1', '2']
    assert 'Choose the one' in out['message']
    client.allow_writes = True
    with pytest.raises(AssertionError):
        px._read_in_session(sess, 'Demo')


def test_failed_read_differs_from_empty_and_is_not_cached(monkeypatch, tmp_path):
    from src.web import account_pixels as px
    from acl2850_fakes import fake_session, quiet, reader
    fake_session(monkeypatch)
    quiet(monkeypatch, 'pages')
    monkeypatch.setattr(px, '_cache_path', lambda: tmp_path / 'account_pixels_cache.json')

    def failed(account):
        raise RuntimeError('failed')
    monkeypatch.setattr(px, '_read_in_session', reader(failed))
    assert px.read_pixels('Demo')['state'] == 'failed'
    assert px.cached_only('Demo')['state'] == 'not_loaded'
    monkeypatch.setattr(px, '_read_in_session', reader(lambda a: {'ok': True, 'state': 'ready', 'pixels': [{'id': '1', 'name': 'A'}]}))
    assert px.read_pixels('Demo')['state'] == 'ready'
    monkeypatch.setattr(px, '_read_in_session', reader(failed))
    assert px.read_pixels('Demo')['state'] == 'ready', 'second read comes from the machine cache'
    assert px.cached_only('Demo')['pixels'] == [{'id': '1', 'name': 'A'}]


def _client():
    from fastapi.testclient import TestClient
    from src.web import app as webapp
    return TestClient(webapp.app, base_url='http://127.0.0.1:9090')


def test_account_pixels_endpoint(monkeypatch, tmp_path):
    from src.web import account_pixels as px
    monkeypatch.setattr(px, '_cache_path', lambda: tmp_path / 'account_pixels_cache.json')
    monkeypatch.setattr(px, '_read_in_session', lambda a: (_ for _ in ()).throw(AssertionError('opened')))
    hdr = {'Origin': 'http://127.0.0.1:9090'}
    c = _client()
    assert c.post('/api/account-pixels', headers=hdr, data={}).status_code == 400
    r = c.post('/api/account-pixels', headers=hdr, data={'account_label': 'Demo', 'cached': '1'})
    assert r.status_code == 200 and r.json()['state'] == 'not_loaded'
    seen = []
    monkeypatch.setattr(px, 'read_pixels', lambda account, force=False: seen.append((account, force)) or
                        {'ok': True, 'state': 'ready', 'pixels': [{'id': '1', 'name': 'A'}]})
    r = c.post('/api/account-pixels', headers=hdr, data={'account_label': 'Demo', 'force': '1'})
    assert r.json()['pixels'] == [{'id': '1', 'name': 'A'}] and seen == [('Demo', True)]


# ───────────────────────── форма → черновик ─────────────────────────

def test_form_and_draft_carry_campaign_pixel(tmp_path):
    from src.web.app import _parse_build_upload_form
    from src.fb_ads_rpa.build_draft import slots_to_campaign_drafts
    from src.fb_ads_rpa.enrich_draft import enrich_campaign_drafts
    _, _, slots = _parse_build_upload_form({'account_label': 'Demo', 'country': 'MX', 'camp_count': '2',
        'camp_0_name': 'First', 'camp_1_name': 'Second', 'camp_0_pixel_id': '333', 'camp_1_pixel_id': ''})
    assert slots[0]['campaign_pixel_id'] == '333' and 'campaign_pixel_id' not in slots[1]
    assert 'pixel_id' not in slots[0], 'the ad-set pixel key stays untouched'
    mapping = tmp_path / 'map.csv'
    mapping.write_text('account_name,ad_account_id,pixel_id\nDemo,act_1234567890,777\n')
    for chosen, expected, source in [('333', '333', 'draft'), ('', '777', 'map')]:
        slot = {'name': 'C', 'budget': '50'}
        if chosen:
            slot['campaign_pixel_id'] = chosen
        d = slots_to_campaign_drafts([slot], account_label='Demo', country='MX')[0]
        enrich_campaign_drafts([d], account_label='Demo', upload_fb_path=str(tmp_path / 'none.xlsx'),
                               ad_account_map_path=str(mapping), creatives_root=str(tmp_path), preview_mode=True)
        assert (d.pixel_id, d.pixel_source) == (expected, source)


def test_same_name_campaigns_with_different_pixels_are_refused():
    from src.fb_ads_rpa.build_draft import slots_to_campaign_drafts
    d = slots_to_campaign_drafts([{'name': 'C', 'budget': '50', 'campaign_pixel_id': '1'},
                                  {'name': 'C', 'budget': '50', 'campaign_pixel_id': '2'}],
                                 account_label='Demo', country='MX')[0]
    assert any('different Pixel choices' in e for e in d.errors)


# ───────────────────────── поле в карточке ─────────────────────────

SCRIPT = Path(__file__).resolve().parents[1] / 'src/web/static/account_pixels.js'
PIXELS = [{'id': '222', 'name': 'Main pixel'}, {'id': '333', 'name': 'Second pixel'}]


def _install(page, payload):
    page.route('**/api/account-pixels', lambda r: r.fulfill(json=payload))
    page.add_script_tag(path=str(SCRIPT))


def test_pixel_picker_choose_duplicate_account_change_and_instant(page):
    _install(page, {'ok': True, 'state': 'ready', 'pixels': PIXELS})
    page.wait_for_function("document.querySelector('[data-pixel-note]').textContent.includes('2 pixels')")
    assert page.locator('[data-pixel-label]').first.inner_text() == 'Pixel'
    page.locator('.cb-pixel-select').first.select_option('333')
    assert page.locator('[name="camp_0_pixel_id"]').input_value() == '333'
    assert 'Second pixel' in page.locator('[data-pixel-note]').first.inner_text()
    page.evaluate("cbDuplicateSlot(document.querySelector('.cb-dup-slot-btn'))")
    assert page.locator('[name="camp_1_pixel_id"]').input_value() == ''
    page.evaluate("document.querySelector('[name=account_label]').value='Other'; cbSyncPixels(document.querySelector('.cb-slot'))")
    assert page.locator('[name="camp_0_pixel_id"]').input_value() == ''
    # ACL-1775: only Instant Form ad sets → the field is hidden.
    page.evaluate("""() => { var s = document.querySelector('.cb-slot');
      s.querySelectorAll('.cb-destination').forEach(function (d) {
        if (!d.querySelector('option[value=instant_form]')) d.add(new Option('Instant Form', 'instant_form'));
        d.value = 'instant_form'; });
      cbSyncPixels(s); }""")
    assert page.locator('.cb-pixel-choice').first.is_hidden()
    assert page.locator('[data-pixel-label]').first.is_hidden()


def test_pixel_picker_single_pixel_is_automatic(page):
    _install(page, {'ok': True, 'state': 'ready', 'pixels': PIXELS[:1]})
    page.wait_for_function("document.querySelector('[data-pixel-note]').textContent.includes('Automatic: Main pixel')")
    assert page.locator('[name="camp_0_pixel_id"]').input_value() == ''


def test_pixel_picker_failed_then_refresh(page):
    _install(page, {'ok': False, 'state': 'failed', 'message': 'Could not load pixels; retry'})
    page.wait_for_function("document.querySelector('[data-pixel-note]').textContent.includes('Could not load')")
    assert page.locator('.cb-pixel-select').first.is_enabled()
    page.route('**/api/account-pixels', lambda r: r.fulfill(json={'ok': True, 'state': 'ready', 'pixels': PIXELS}))
    page.evaluate("cbRefreshPixels(document.querySelector('.cb-slot'), true)")
    page.wait_for_function("document.querySelector('[data-pixel-note]').textContent.includes('2 pixels')")


def test_empty_answer_is_not_cached_and_next_read_rereads(monkeypatch, tmp_path):
    from src.web import account_pixels as px
    from acl2850_fakes import fake_session, quiet, reader
    fake_session(monkeypatch)
    quiet(monkeypatch, 'pages')
    monkeypatch.setattr(px, '_cache_path', lambda: tmp_path / 'account_pixels_cache.json')
    calls = []

    def empty(account):
        calls.append(account)
        return {'ok': True, 'state': 'empty', 'pixels': []}
    monkeypatch.setattr(px, '_read_in_session', reader(empty))
    assert px.read_pixels('Demo')['state'] == 'empty'
    assert px.cached_only('Demo')['state'] == 'not_loaded'
    # Пиксель подключили в Business Settings: следующее чтение идёт в профиль заново.
    monkeypatch.setattr(px, '_read_in_session', reader(lambda a: {'ok': True, 'state': 'ready', 'pixels': [{'id': '1', 'name': 'A'}]}))
    assert px.read_pixels('Demo')['state'] == 'ready'
    assert len(calls) == 1
    assert px.cached_only('Demo')['state'] == 'ready'


def test_pixel_picker_empty_list_says_no_pixel_found(page):
    _install(page, {'ok': True, 'state': 'empty', 'pixels': [], 'message': 'This ad account has no pixel/dataset.'})
    page.wait_for_function("document.querySelector('[data-pixel-note]').textContent.includes('no pixel')")
    first = page.locator('.cb-pixel-select').first.locator('option').first.inner_text()
    assert first == 'No pixel found on this account'
    assert 'only pixel' not in first
