"""Country reads update summaries without discarding in-progress account setup."""
import json
import os
from pathlib import Path
import pytest


@pytest.fixture
def page():
    from playwright.sync_api import sync_playwright
    source = Path('src/web/templates/report.html').read_text(encoding='utf-8')
    script = source[source.index('function amapSplitMark('):source.index('function amapMoney(')]
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get('ACL_BROWSER_EXECUTABLE') or None)
        context = browser.new_context()
        context.route('**/*', lambda route: route.fulfill(content_type='text/html', body=''))
        pg = context.new_page()
        pg.goto('http://acl.test/')
        pg.set_content('''<div id="amap-slot"><div class="amap"><div class="amap-head"><span class="amap-note">1 country</span><input id="amap-new-country" value="unsaved setup"></div><details class="amap-country" data-country="Peru" open><summary><span class="amap-live-summary"><i class="amap-country-read">old reading</i></span></summary><input id="account-setup" value="pending profile"></details><details class="amap-available"></details></div></div><table><tr><td class="amap-live" id="cell" data-account="Demo"><span class="amap-live-ok">$10.00</span></td></tr></table><button id="read" class="amap-btn amap-read">Read spend</button>''')
        pg.add_script_tag(content="var amapReadAllBusy=false; var period={from:'2026-09-01',to:'2026-09-15',span:''}; function amapPeriod(){return period;}\n"+script)
        pg.evaluate('window.savedInput=document.querySelector("#account-setup")')
        yield pg
        browser.close()


def test_new_country_keeps_existing_setup_nodes_and_text(page):
    html = '''<div class="amap"><div class="amap-head"><span class="amap-note">2 countries</span><input id="amap-new-country"></div><details class="amap-country" data-country="Peru"><summary><span class="amap-live-summary"><i class="amap-country-read">fresh reading</i></span></summary><input id="account-setup"></details><details class="amap-country" data-country="Chile"><summary><span class="amap-live-summary"><i class="amap-country-read">Chile $44.17</i></span></summary></details><details class="amap-available"></details></div>'''
    page.route('**/account-map?*', lambda route: route.fulfill(content_type='text/html', body=html))
    page.evaluate('amapRefreshCountryLines(period)')
    assert page.locator('#account-setup').input_value() == 'pending profile'
    assert page.locator('#amap-new-country').input_value() == 'unsaved setup'
    assert page.evaluate('savedInput === document.querySelector("#account-setup")')
    assert 'fresh reading' in page.locator('[data-country="Peru"]').inner_text()
    assert page.locator('[data-country="Chile"]').count() == 1


def test_failed_retry_removes_the_previous_country_total(page):
    """Неудача разбивки: страновой итог снимается, спенд и кнопка остаются.

    ACL-1282: кнопка теперь одна на обе половины чтения, поэтому «failed ·
    retry» на ней означало бы «спенд не прочитан». Отказ разбивки живёт в
    клетке — там же, где прочитанное число, которое он не отменяет.
    """
    from playwright.sync_api import expect
    page.route('**/account-spend/read-countries', lambda route: route.fulfill(content_type='application/json', body=json.dumps({'ok': True})))
    page.route('**/account-spend/status?*', lambda route: route.fulfill(content_type='application/json', body=json.dumps({'ok':True,'split':{'state':'failed','error':'synthetic read failure'}})))
    page.route('**/account-map?*', lambda route: route.fulfill(content_type='text/html', body='<div class="amap"><details class="amap-country" data-country="Peru"><summary><span class="amap-live-summary"></span></summary></details></div>'))
    page.evaluate('window.splitState = amapRunSplit("Demo", document.querySelector("#read"), period)')
    expect(page.locator('#cell .amap-split-bad')).to_have_text('countries failed', timeout=10000)
    assert 'synthetic read failure' in page.locator('#cell .amap-split-bad').get_attribute('title')
    expect(page.locator('#read')).to_have_text('Read spend')
    assert page.locator('#cell .amap-live-ok').inner_text() == '$10.00'
    # Итог страны снимается: чтения за эти даты больше нет.
    expect(page.locator('.amap-country-read')).to_have_count(0)


def test_a_second_attempt_clears_the_previous_country_failure(page):
    """Пометка об отказе — про текущее чтение, а не вечная надпись в клетке."""
    from playwright.sync_api import expect
    page.evaluate('amapSplitMark("Demo", "first failure")')
    assert page.locator('#cell .amap-split-bad').count() == 1
    page.route('**/account-spend/read-countries', lambda route: route.fulfill(content_type='application/json', body=json.dumps({'ok': True})))
    page.route('**/account-spend/status?*', lambda route: route.fulfill(content_type='application/json', body=json.dumps({'ok':True,'split':{'state':'ok','rows':[]}})))
    page.route('**/account-map?*', lambda route: route.fulfill(content_type='text/html', body='<div class="amap"><details class="amap-country" data-country="Peru"><summary><span class="amap-live-summary"><i class="amap-country-read">Peru $12.00</i></span></summary></details></div>'))
    page.evaluate('amapRunSplit("Demo", document.querySelector("#read"), period)')
    expect(page.locator('#cell .amap-split-bad')).to_have_count(0, timeout=10000)
    expect(page.locator('#read')).to_have_text('Read spend')
