"""Country reads update summaries without discarding in-progress account setup."""
import json
import os
from pathlib import Path
import pytest


@pytest.fixture
def page():
    from playwright.sync_api import sync_playwright
    source = Path('src/web/templates/report.html').read_text(encoding='utf-8')
    script = source[source.index('function amapReadCountries('):source.index('function amapMoney(')]
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get('ACL_BROWSER_EXECUTABLE') or None)
        context = browser.new_context()
        context.route('**/*', lambda route: route.fulfill(content_type='text/html', body=''))
        pg = context.new_page()
        pg.goto('http://acl.test/')
        pg.set_content('''<div id="amap-slot"><div class="amap"><div class="amap-head"><span class="amap-note">1 country</span><input id="amap-new-country" value="unsaved setup"></div><details class="amap-country" data-country="Peru" open><summary><span class="amap-live-summary"><i class="amap-country-read">old reading</i></span></summary><input id="account-setup" value="pending profile"></details><details class="amap-available"></details></div></div><button id="read">Countries</button>''')
        pg.add_script_tag(content="var period={from:'2026-09-01',to:'2026-09-15',span:''}; function amapPeriod(){return period;}\n"+script)
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
    from playwright.sync_api import expect
    page.route('**/account-spend/status?*', lambda route: route.fulfill(content_type='application/json', body=json.dumps({'ok':True,'split':{'state':'failed','error':'synthetic read failure'}})))
    page.route('**/account-map?*', lambda route: route.fulfill(content_type='text/html', body='<div class="amap"><details class="amap-country" data-country="Peru"><summary><span class="amap-live-summary"></span></summary></details></div>'))
    page.evaluate('amapPollCountries("Demo", document.querySelector("#read"), 0, period)')
    expect(page.locator('#read')).to_have_text('failed · retry', timeout=10000)
    expect(page.locator('.amap-country-read')).to_have_count(0)
