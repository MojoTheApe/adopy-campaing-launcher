"""Offline browser integration of the shipped builder and Page picker."""
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent))
from test_builder_review_browser import browser, page  # noqa: F401
from src.fb_ads_rpa.page_identity import ensure_page_identity

SCRIPT = Path(__file__).resolve().parents[1] / 'src/web/static/account_pages.js'
PAGES = [{'id': '123', 'name': 'First Page'}, {'id': '456', 'name': 'Second Page'}]


def test_manual_plan_does_not_promise_ads(full_page):
    html = full_page.evaluate("""() => cbPlanHtml({results:[{campaign_name:'Manual', warnings:[],
      plan:{summary:{daily_budget:50,currency:'USD',manual_required:['Add Page and ads manually'],
      ad_sets:[{name:'Set',ads:[],ads_not_created:true,pending_ad_count:2}]}}}]})""")
    assert 'Manual completion required' in html
    assert 'Ads will not be created (2 kept in the builder)' in html
    assert '<li><strong>' not in html


def install(page, payload):
    page.route('**/api/account-pages', lambda r: r.fulfill(json=payload))
    page.add_script_tag(path=str(SCRIPT))


def test_auto_explicit_duplicate_and_account_change(page):
    install(page, {'ok': True, 'state': 'ready', 'pages': PAGES})
    page.wait_for_function("document.querySelector('[data-page-note]').textContent.includes('First Page')")
    select = page.locator('.cb-page-select').first
    select.select_option('456')
    assert page.locator('[name="camp_0_page_id"]').input_value() == '456'
    assert 'Second Page' in page.locator('[data-page-note]').first.inner_text()
    page.locator('[name="camp_0_budget"]').fill('73')
    page.evaluate("cbDuplicateSlot(document.querySelector('.cb-dup-slot-btn'))")
    assert page.locator('[name="camp_1_page_id"]').input_value() == ''
    assert page.locator('[name="camp_1_budget"]').input_value() == '73'
    assert 'First Page' in page.locator('[data-page-note]').nth(1).inner_text()
    page.evaluate("document.querySelector('[name=account_label]').value='Other'; cbSyncPages(document.querySelector('.cb-slot'))")
    assert page.locator('[name="camp_0_page_id"]').input_value() == ''


def test_error_retry_and_confirmed_empty(page):
    install(page, {'ok': False, 'state': 'failed', 'message': 'Could not load pages; retry'})
    page.wait_for_function("document.querySelector('[data-page-note]').textContent.includes('Could not load')")
    page.route('**/api/account-pages', lambda r: r.fulfill(json={'ok': True, 'state': 'empty', 'pages': [], 'message': 'No Facebook Pages available'}))
    page.evaluate("cbRefreshPages(document.querySelector('.cb-slot'), true)")
    page.wait_for_function("document.querySelector('[data-page-note]').textContent.includes('No Facebook Pages')")
    assert page.locator('.cb-page-select').first.is_enabled()
    page.route('**/api/account-pages', lambda r: r.fulfill(json={'ok': True, 'state': 'ready', 'pages': PAGES}))
    page.evaluate("cbRefreshPages(document.querySelector('.cb-slot'), true)")
    page.wait_for_function("document.querySelector('[data-page-note]').textContent.includes('First Page')")


def test_native_identity_selects_id_and_refuses_missing_id(page):
    page.set_content('<label>Facebook Page<select><option value="123">First Page</option><option value="456">Second Page</option></select></label>')
    assert ensure_page_identity(page, '456', 'Second Page', True)
    assert page.locator('select').input_value() == '456'
    assert not ensure_page_identity(page, '789', 'Missing', True)
    assert not ensure_page_identity(page, '', '', False)


def test_captured_identity_accepts_verified_unique_page(page):
    fixture = Path(__file__).parent / 'fixtures/ads_manager/simple_create_2026_09_27.html'
    page.set_content(fixture.read_text(encoding='utf-8'))
    assert ensure_page_identity(page, '123', 'Sample Page', True)
    assert not ensure_page_identity(page, '456', 'Sample Page', False), 'duplicate names do not prove identity'


def test_custom_identity_edit_selection_is_read_back(page):
    page.set_content('''<div role="button" onclick="document.querySelector('#identityEditorComponent').hidden=false;this.hidden=true">
      <div><div role="heading" aria-level="4">Facebook Page</div></div><div>First Page</div></div>
      <div id="identityEditorComponent" hidden><div id="chosen">First Page</div>
      <button onclick="document.querySelector('#picker').removeAttribute('hidden')">Edit</button></div>
      <div role="dialog" id="picker" hidden><div role="option" onclick="document.querySelector('#chosen').textContent='Second Page';document.querySelector('#picker').remove()">Second Page</div></div>''')
    assert ensure_page_identity(page, '456', 'Second Page', True)
    assert page.locator('#chosen').inner_text() == 'Second Page'


def test_auto_resolution_changes_on_refresh_and_old_response_cannot_select(page):
    install(page, {'ok': True, 'state': 'ready', 'pages': PAGES})
    page.wait_for_function("cbResolvedPage(document.querySelector('.cb-slot')) === '123'")
    page.evaluate("window.cbForms={'Demo|123':{state:'ok',ok:true,page_id:'123',forms:[{id:'789',name:'Old Page form'}]}}")
    page.route('**/api/account-pages', lambda r: r.fulfill(json={'ok': True, 'state': 'ready', 'pages': list(reversed(PAGES))}))
    page.evaluate("cbRefreshPages(document.querySelector('.cb-slot'),true)")
    page.wait_for_function("cbResolvedPage(document.querySelector('.cb-slot')) === '456'")
    # A delayed response for the previous Page retains its own key, not the automatic key.
    assert page.evaluate("cbForms['Demo|' + cbResolvedPage(document.querySelector('.cb-slot'))] === undefined")

from test_builder_duplicate_buttons import page as full_page  # noqa: F401,E402
from test_drawer_review_browser import add  # noqa: E402


def test_full_builder_drops_old_automatic_page_forms(full_page):
    p = full_page
    p.evaluate("window.ACL_FB_BACKEND='api'")
    install(p, {'ok': True, 'state': 'ready', 'pages': PAGES})
    add(p)
    p.locator('.cbd-camp-row').first.click()
    p.wait_for_function("cbResolvedPage(document.querySelector('.cb-slot'))==='123'")
    p.evaluate("cbForms['Demo|123']={state:'ok',ok:true,page_id:'123',forms:[{id:'789',name:'Old form',status:'ACTIVE'}]}; cbFillFormSelects(document.querySelector('.cb-slot'))")
    assert 'Old form' in p.locator('.cb-form-select').first.inner_text()
    p.route('**/api/account-pages', lambda r: r.fulfill(json={'ok': True, 'state': 'ready', 'pages': list(reversed(PAGES))}))
    p.evaluate("cbRefreshPages(document.querySelector('.cb-slot'),true)")
    p.wait_for_function("cbResolvedPage(document.querySelector('.cb-slot'))==='456'")
    assert 'Old form' not in p.locator('.cb-form-select').first.inner_text()
    # A late response is stored under the requested Page, never the current automatic choice.
    p.evaluate("cbForms['Demo|123']={state:'ok',ok:true,page_id:'123',forms:[{id:'790',name:'Late form'}]}; cbFillFormSelects(document.querySelector('.cb-slot'))")
    assert 'Late form' not in p.locator('.cb-form-select').first.inner_text()


def test_render_requests_cache_only_and_click_loads(page):
    seen = []
    def handler(route):
        body = route.request.post_data or ''
        seen.append(body)
        if 'cached=1' in body or 'name="cached"' in body:
            route.fulfill(json={'ok': True, 'state': 'not_loaded', 'pages': [], 'message': 'Pages are not loaded yet.'})
        else:
            route.fulfill(json={'ok': True, 'state': 'ready', 'pages': PAGES})
    page.route('**/api/account-pages', handler)
    page.add_script_tag(path=str(SCRIPT))
    page.wait_for_function("document.querySelector('[data-page-note]').textContent.includes('not loaded')")
    assert seen and all('cached' in b for b in seen)
    page.locator('.cb-page-select').first.focus()
    page.wait_for_function("document.querySelector('[data-page-note]').textContent.includes('First Page')")
    assert any('cached' not in b for b in seen)
