"""Manual browser regression suite for reviewed builder changes.

Run: ACL_BROWSER_EXECUTABLE=/path/to/chrome python tests/browser_review_cards.py
Without that variable, install Playwright Chromium first. All network requests
are intercepted; only synthetic account settings in a temporary directory exist.
"""
import asyncio
import csv
import json
import os
import sys
import tempfile
from email.parser import BytesParser
from email.policy import default
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
OUT = Path(os.environ.get('ACL_REVIEW_OUTPUT', tempfile.mkdtemp(prefix='acl-browser-review-')))
OUT.mkdir(exist_ok=True)
sys.path.insert(0, str(ROOT))
from playwright.sync_api import sync_playwright
from starlette.datastructures import FormData
from src.web import app as web, account_links
from src.analysis_agent import ad_text_registry as ar
from src.fb_ads_rpa import enrich_draft as en
from src.meta_campaign_agent.ad_account_map import load_ad_account_map
from tests.test_web_build_upload import _minimal_report

def request(data):
    return NS(json=AsyncMock(return_value=data))

def invoke(coro):
    # Playwright's sync callbacks still run inside its own asyncio loop.
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(asyncio.run, coro).result(timeout=15)

def form_data(req):
    msg = BytesParser(policy=default).parsebytes(
        ('Content-Type: ' + req.headers['content-type'] + '\r\n\r\n').encode() + req.post_data_buffer)
    return FormData([(p.get_param('name', header='content-disposition'), p.get_content()) for p in msg.iter_parts()])

results, errors, seen, previews = {}, [], [], []
with tempfile.TemporaryDirectory(dir=OUT) as config:
    cfg = Path(config)
    mapfile = cfg / 'map.csv'
    os.environ['ACL_CONFIG_DIR'] = config
    os.environ['META_AD_ACCOUNT_MAP_PATH'] = str(mapfile)
    with mapfile.open('w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=['account_name','ad_account_id','profile_id','folder_id','country','tracking_link'])
        w.writeheader()
        w.writerow(dict(account_name='Demo',ad_account_id='act_1234567890',profile_id='demo-profile',folder_id='demo-folder',country='SA',tracking_link='https://example.test/saved'))
    ar.add('Demo text', 'Local headline', 'Local body', 'Description')
    account_links.link('Demo','country','SA')
    cached = _minimal_report()
    cached['my_cm'] = 'Demo'
    cached['countries'][0]['open_recs'] = {}
    web.get_cached_report = lambda: (cached, None, [])
    web._get_upload_ref_cached = lambda: {}
    web.get_upload_fb_excel_path = en.get_upload_fb_excel_path = lambda: str(cfg / 'missing.xlsx')
    en.get_creatives_root = lambda: config
    web._load_ref_options = lambda: {}
    web._build_goals_view = lambda *a: {}
    web._installed_release = lambda: {}
    web._load_draft_status = lambda: {}
    web.use_rpa_publish = lambda: True
    web._accounts_spending_today = lambda *a: []

    def html():
        response = web._render_report(MagicMock())
        text = response.body.decode()
        (OUT/'dashboard.html').write_text(text)
        return text

    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ.get('ACL_BROWSER_EXECUTABLE'), headless=True)
        ctx = browser.new_context(viewport={'width':1440,'height':1100})
        def route(r):
            req = r.request
            path = urlparse(req.url).path
            seen.append(path)
            if req.is_navigation_request():
                return r.fulfill(content_type='text/html', body=html())
            if path == '/static/logo.svg':
                return r.fulfill(content_type='image/svg+xml', body=(ROOT/'src/web/static/logo.svg').read_text())
            if 'htmx.org' in req.url:
                return r.fulfill(content_type='application/javascript', body='')
            status, data = 200, {'ok':True}
            handlers = {'/account-link-patch':web.account_link_patch, '/account-map-patch':web.account_map_patch,
                        '/account-links':web.account_links_edit}
            if path in handlers:
                response = invoke(handlers[path](request(req.post_data_json)))
                status, data = response.status_code, json.loads(response.body)
            elif path == '/preview-publish':
                response = invoke(web.preview_publish(NS(form=AsyncMock(return_value=form_data(req)))))
                status, data = response.status_code, json.loads(response.body)
                previews.append(data)
            elif path == '/api/ad-copy': data['rows'] = web._ad_copy_rows()
            elif path == '/api/creatives': data['files'] = []
            elif path == '/api/buyers': data.update(buyers=['Demo'],selected='Demo',default='Demo')
            elif path == '/api/accounts-overview': data.update(accounts=[],open_profiles=[],suggestions=[])
            elif path == '/api/targeting-profiles': data.update(profiles=[],sheet={},duplicates={})
            elif path == '/multilogin-search': data.update(profiles=[],folders=[])
            elif path == '/api/resolve-targeting': data['matched'] = False
            elif path == '/mlx-profile-open': data['message'] = 'profile opened (synthetic)'
            elif path == '/mlx-profile-details': data.update(ad_account_id='',folder_id='new-folder')
            elif path == '/account-probe/ad-account': data.update(ok=False,error='No account yet (synthetic)')
            r.fulfill(status=status,content_type='application/json',body=json.dumps(data))
        ctx.route('**/*',route)
        page = ctx.new_page()
        page.set_default_timeout(10000)
        page.on('pageerror',lambda e: errors.append(str(e)))
        page.on('dialog',lambda d: d.accept('New Demo' if d.message.startswith('Ad account name') else ''))
        page.goto('http://acl-review.test/#country-saudi-arabia-manage')
        page.wait_for_function("document.querySelector('.view-panel.active')?.id === 'view-country-saudi-arabia-manage'")
        page.evaluate("aclOpenCountryBuilder('saudi-arabia')")
        page.locator('#manage-builder-saudi-arabia .cb-acc-header').click()
        form = page.locator('#manage-builder-saudi-arabia form.open-account-form')
        form.get_by_role('button',name='+ Add Campaign',exact=True).click()
        slot=form.locator('.cb-slot').first
        slot.get_by_role('button',name='+ Ad',exact=True).click()
        slot.get_by_role('button',name='+ Ad',exact=True).click()
        slot.locator('.cb-ad-row').nth(0).locator('input[value="Video"]').check()
        slot.locator('.cb-ad-row').nth(0).locator('[name$="_creative_file"]').evaluate("e=>e.value='first.mp4'")
        slot.locator('.cb-ad-row').nth(1).locator('[name$="_creative_file"]').evaluate("e=>e.value='second.jpg'")
        slot.locator('.cb-ad-row').nth(2).locator('[name$="_creative_file"]').evaluate("e=>e.value='third.jpg'")
        slot.locator('.cb-dup-ad-btn').first.click()
        state=slot.evaluate("s=>Array.from(s.querySelectorAll('.cb-ad-row')).map(r=>({type:r.querySelector('input:checked')?.value,file:r.querySelector('[name$=\"_creative_file\"]').value}))")
        assert state==[{'type':'Video','file':'first.mp4'},{'type':'Video','file':'first.mp4'},{'type':'Image','file':'second.jpg'},{'type':'Image','file':'third.jpg'}],state
        slot.locator('[name$="_language"]').fill('Spanish')
        slot.locator('[name$="_budget"]').fill('37')
        form.get_by_role('button',name='+ Add Campaign',exact=True).click()
        form.locator('.cb-slot').nth(1).locator('[name$="_language"]').fill('Arabic')
        slot.locator('.cb-dup-slot-btn').click()
        states=form.evaluate("f=>Array.from(f.querySelectorAll('.cb-slot')).map(s=>({idx:s.dataset.idx,lang:s.querySelector('[name$=\"_language\"]').value,types:Array.from(s.querySelectorAll('input[type=radio]:checked')).map(e=>e.value)}))")
        assert states==[{'idx':'0','lang':'Spanish','types':['Video','Video','Image','Image']},{'idx':'1','lang':'Spanish','types':['Video','Video','Image','Image']},{'idx':'2','lang':'Arabic','types':['Image']}],states
        assert form.locator('[name="camp_1_budget"]').input_value()=='37'
        form.locator('[name="camp_1_language"]').fill('')
        assert form.locator('[name="camp_0_language"]').input_value()=='Spanish'
        assert 'cb-language-empty' in form.locator('[name="camp_1_language"]').get_attribute('class')
        print('PASS: campaign copy preserves language, all ad types, budget and independent fields')
        page.wait_for_function("Array.from(document.querySelectorAll('#manage-builder-saudi-arabia .cb-slot')).every(s=>s.querySelectorAll('.cbp-item').length===s.querySelectorAll('.cb-ad-row').length)")
        # Synthetic preview responses exercise the real rendered diagnostic UI.
        page.evaluate("""() => {
          window.realFetch=window.fetch;
          window.previewPlan=[
            {'Campaign Name':'First <campaign>', Language:'Spanish', errors:[], warnings:['Check audience']},
            {'Campaign Name':'Second', Language:'Arabic', errors:['Missing creative'], warnings:[]},
            {'Campaign Name':'Third', errors:[], warnings:[]}];
          window.fetch=(url,opts)=>url==='/preview-publish'
            ? Promise.resolve({ok:true,json:async()=>({plan:window.previewPlan})}) : realFetch(url,opts);
        }""")
        form.locator('.cb-preview-btn').click()
        page.wait_for_function("document.querySelectorAll('.cbp-diag-chip').length===3")
        assert form.locator('.cbp-diag-chip-bad').count()==1
        assert form.locator('.cb-publish-btn').is_disabled()
        chip=form.locator('.cbp-diag-chip').first
        assert chip.evaluate("e=>!!e.closest('.cb-ads-section')")
        top=form.locator('.cb-actions').evaluate('e=>e.getBoundingClientRect().top+scrollY')
        chip.click()
        drawer=page.locator('#cbp-diag-drawer')
        assert drawer.locator(".cbp-diag-pop").is_visible()
        assert 'First <campaign>' in drawer.inner_text()
        assert 'Check audience' in drawer.inner_text()
        assert abs(form.locator('.cb-actions').evaluate('e=>e.getBoundingClientRect().top+scrollY')-top)<1
        page.screenshot(path=str(OUT/'diagnostic.png'))
        page.keyboard.press('Tab')
        assert drawer.locator('.cbp-diag-pop-close').evaluate('e=>e===document.activeElement')
        page.keyboard.press('Escape')
        assert not drawer.locator(".cbp-diag-pop").is_visible()
        assert chip.evaluate('e=>e===document.activeElement')
        form.locator('[data-view="visual"]').click()
        assert not chip.is_visible()
        form.locator('[data-view="diag"]').click()
        assert chip.is_visible()
        form.locator('[name="camp_0_budget"]').fill('40')
        assert form.locator('.cbp-diag-chip').count()==0
        assert 'preview again' in form.locator('.cb-preview-panel').inner_text()
        # Reverse response order: older validation must not replace the latest.
        page.evaluate("window.pending=[];window.fetch=(url,opts)=>url==='/preview-publish'?new Promise(resolve=>pending.push(resolve)):realFetch(url,opts)")
        form.locator('.cb-preview-btn').click()
        form.locator('.cb-preview-btn').click()
        page.evaluate("pending[1]({ok:true,json:async()=>({plan:[{'Campaign Name':'Newest',errors:[],warnings:[]}]})})")
        page.wait_for_function("document.querySelector('.cbp-diag-chip')?.textContent.includes('Newest')")
        page.evaluate("pending[0]({ok:true,json:async()=>({plan:previewPlan})})")
        page.wait_for_timeout(100)
        assert form.locator('.cbp-diag-chip').count()==1
        assert 'Newest' in form.locator('.cbp-diag-chip').inner_text()
        form.locator('.cb-preview-btn').click()
        assert form.locator('.cbp-diag-chip').count()==0
        page.evaluate("pending[2]({ok:false,status:500,json:async()=>({error:'Synthetic server failure'})})")
        page.wait_for_function("document.querySelector('.cb-preview-panel')?.textContent.includes('Synthetic server failure')")
        assert form.locator('.cbp-diag-chip').count()==0
        assert form.locator('.cb-publish-btn').is_disabled()
        print('PASS: preview refresh, errors, drawer layout/focus, toggles, edits, stale responses and failure clearing')
        # Render the shared account map fragment and run its actual Read all buttons.
        def map_row(name):
            return dict(account=name, has_data=False, status={}, workbook_alias='', elsewhere={},
                        assets=[], countries=[], ready=True, in_workbook=False)
        amap=dict(dates=[], countries=[
            dict(country='Alpha',key='Alpha',rows=[map_row('First'),map_row('Broken')],spend=0,leads=0,deposits=0),
            dict(country='Beta',key='Beta',rows=[map_row('First'),map_row('toString')],spend=0,leads=0,deposits=0)],
            available=[],all_accounts=['First','Broken','toString'],live={},date_from='2026-09-01',date_to='2026-09-09',span='')
        fragment=web.templates.env.get_template('_account_map.html').render(amap=amap,mlx={'A':'A','B':'B'})
        page.evaluate("html=>{location.hash='#home';document.querySelector('#view-home .amap').outerHTML=html;}",fragment)
        page.evaluate("""() => {
          window._amapPick={from:'2026-09-01',to:'2026-09-09',span:''};
          window.readRequests=[]; window.finished=[]; window.holdFirst=true;
          window.nativeTimeout=window.setTimeout;
          window.setTimeout=(fn,ms)=>nativeTimeout(fn,ms===3000?1:ms);
          window.fetch=async (url,opts={})=>{
            if (!url.startsWith('/account-spend/')) return realFetch(url,opts);
            const account=opts.body?.get('account') || new URL(url,location.origin).searchParams.get('account');
            if (url==='/account-spend/read') {
              readRequests.push([account,opts.body.get('date_from'),opts.body.get('date_to')]);
              if (account==='Broken') throw new Error('Synthetic offline account');
            } else {
              if (account==='First' && holdFirst) await new Promise(resolve=>window.releaseFirst=resolve);
              finished.push(account);
            }
            return {ok:true,json:async()=>({ok:true,reading:{state:'ok',spend:12.34,currency:'EUR',date_from:'2026-09-01',date_to:'2026-09-09'}})};
          };
        }""")
        # Select directly by the live map: no navigation or external reads occur.
        page.evaluate("()=>{window.queuePromise=amapReadAll(document.querySelector('#view-home .amap-read-all'));}")
        page.wait_for_function("typeof releaseFirst==='function'")
        assert page.evaluate('readRequests')==[['First','2026-09-01','2026-09-09']]
        assert page.locator('#view-home .amap-read-all').first.is_disabled()
        page.evaluate("amapReadAll(document.querySelector('#view-home .amap-read-all'));amapReadSpend(document.querySelector('#view-home .amap-read'))")
        assert len(page.evaluate('readRequests'))==1
        page.evaluate("holdFirst=false;releaseFirst()")
        page.wait_for_function('amapReadAllBusy===false')
        assert page.evaluate('readRequests')==[[a,'2026-09-01','2026-09-09'] for a in ['First','Broken','toString']]
        assert page.locator('#view-home .amap-read-all').first.get_attribute('title')=='2 read, 1 failed (spend)'
        assert page.locator('#view-home .amap-live[data-account="First"]').count()==2
        for cell in page.locator('#view-home .amap-live[data-account="First"]').all():
            assert 'EUR 12.34' in cell.text_content(), cell.text_content()
        assert 'Synthetic offline account' in page.locator('#view-home .amap-live[data-account="Broken"]').text_content()
        page.evaluate("()=>{readRequests=[];window.queuePromise=amapReadAll(document.querySelector('#view-home details[data-country=Beta] .amap-read-all'));}")
        page.wait_for_function('amapReadAllBusy===false')
        assert [r[0] for r in page.evaluate('readRequests')]==['First','toString']
        # A never-answering POST aborts; the following account still runs.
        page.evaluate("""() => {
          readRequests=[]; const previous=window.fetch;
          window.setTimeout=(fn,ms)=>nativeTimeout(fn,ms===3000?1:ms===20000?50:ms);
          window.fetch=(url,opts={})=>url==='/account-spend/read' && opts.body.get('account')==='First'
            ? new Promise((resolve,reject)=>opts.signal.addEventListener('abort',()=>reject(new DOMException('Timed out','AbortError'))))
            : previous(url,opts);
          window.queuePromise=amapReadAll(document.querySelector('#view-home details[data-country=Beta] .amap-read-all'));
        }""")
        page.wait_for_function('amapReadAllBusy===false')
        assert [r[0] for r in page.evaluate('readRequests')]==['toString']
        assert page.locator('#view-home details[data-country="Beta"] .amap-read-all').get_attribute('title')=='1 read, 1 failed (spend)'
        assert not page.locator('#view-home .amap-read-all').first.is_disabled()
        beta=page.locator('#view-home details[data-country="Beta"] .amap-live-summary')
        assert 'EUR 12.34' in beta.text_content() and '1/2 spend read' in beta.text_content()
        page.evaluate("amapApplyReading('First',{state:'ok',spend:7,currency:'KWD'})")
        assert 'KWD 7.00' in beta.text_content() and 'EUR 12.34' in beta.text_content()
        assert '2/2 spend read' in beta.text_content()
        page.evaluate("amapApplyReading('First',{state:'running'})")
        assert 'KWD 7.00' not in beta.text_content() and '1/2 spend read' in beta.text_content()
        page.evaluate("amapApplyReading('First',{state:'ok',spend:0,currency:'EUR'})")
        assert beta.locator('b').count()==1 and 'EUR 12.34' in beta.text_content()
        page.evaluate("amapApplyReading('First',{state:'ok',spend:2,currency:''});amapApplyReading('toString',{state:'ok',spend:3,currency:''})")
        # ACL-1258: без «currency unknown» и имени кабинета, суммы — порознь.
        assert '2.00' in beta.text_content() and '3.00' in beta.text_content()
        assert 'currency unknown' not in beta.text_content()
        assert '(First)' not in beta.text_content() and '(toString)' not in beta.text_content()
        page.set_viewport_size({'width':1100,'height':900})
        for summary in page.locator('#view-home details[data-country] > summary').all():
            assert summary.evaluate('e=>e.scrollWidth<=e.clientWidth+1'), summary.text_content()
        page.screenshot(path=str(OUT/'country-totals.png'))
        print('PASS: live country summaries refresh, retain zero and separate known/unknown currencies without overflow')
        page.evaluate("readRequests=[];_amapPick={from:'',to:'',span:'all'};amapReadAll(document.querySelector('#view-home .amap-read-all'))")
        assert page.evaluate('readRequests')==[]
        print('PASS: Read all sequential order, duplicates, selected period, country scope, failure continuation, timeout and re-entry guards')
        assert not errors, errors
        print('PASS: real rendered ad card duplicates selected video, preserves later radio groups and order')
        browser.close()
