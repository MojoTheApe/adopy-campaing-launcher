import sys,os
from pathlib import Path
sys.path.insert(0,str(Path.cwd()));sys.path.insert(0,'tests')
from urllib.parse import urlparse
from test_builder_per_account import _render
from playwright.sync_api import sync_playwright,expect
import pytest
from acl_dialog_capture import capture_dialogs


def test_hidden_accounts_survive_navigation_and_bulk_actions(tmp_path, monkeypatch):
    monkeypatch.setenv('ACL_CONFIG_DIR', str(tmp_path))
    html=_render({'Demo':[]})
    with sync_playwright() as pw:
        b=pw.chromium.launch(executable_path=os.environ.get('ACL_BROWSER_EXECUTABLE') or None)
        page=b.new_page();errors=[];posts=[];hidden=set();removed=set()
        page.on('pageerror',lambda e:errors.append(str(e)));page.on('dialog',lambda d:d.accept());capture_dialogs(page, [])
        def overview():
            return {'ok':True,'open_profiles':[],'suggestions':[], 'accounts':[
                {'label':label,'connected':connected,'local_only':False,'missing':[] if connected else ['profile_id'],
                 'country':'','ad_account_id':'123' if connected else '', 'profile_id':'','hidden':label in hidden}
                for label,connected in [('Local',True),('Sheet A',False),('Sheet B',False)] if label not in removed]}
        def route(r):
            path=urlparse(r.request.url).path
            if r.request.resource_type=='document':r.fulfill(content_type='text/html',body=html)
            elif path=='/api/accounts-overview':r.fulfill(json=overview())
            elif path=='/api/accounts-hidden':
                d=r.request.post_data_json;posts.append((path,d));hidden.add(d['label']) if d['hidden'] else hidden.discard(d['label']);r.fulfill(json={'ok':True})
            elif path=='/account-map-remove':
                d=r.request.post_data_json;posts.append((path,d));removed.update(d.get('account_names') or [d['account_name']]);r.fulfill(json={'ok':True})
            else:r.fulfill(json={'ok':True,'files':[],'texts':[],'profiles':[],'waiting':False,'plan':[]})
        page.route('**/*',route);page.goto('http://acl.test/#accounts')
        expect(page.locator('#ac-list tbody tr')).to_have_count(3)
        page.locator('tr[data-label="Sheet A"] .ac-drop').click()
        expect(page.locator('#ac-list tbody tr')).to_have_count(2)
        expect(page.locator('.acc-hidden-bar')).to_contain_text('1 account hidden')
        page.reload();expect(page.locator('#ac-list tbody tr')).to_have_count(2)
        page.locator('#ac-show-hidden').click();expect(page.locator('#ac-list tbody tr')).to_have_count(3)
        page.locator('tr[data-label="Sheet A"] .ac-hide').click();expect(page.locator('.acc-hidden-bar')).to_have_count(0)
        page.locator('#ac-check-all').check();page.locator('#ac-bulk-drop').click()
        expect(page.locator('#ac-list tbody tr')).to_have_count(2) # showHidden stays true; local is removed
        assert removed=={'Local'} and hidden=={'Sheet A','Sheet B'},(posts,hidden,removed)
        assert [d for p,d in posts if p=='/account-map-remove']==[{'account_names':['Local']}],posts
        page.reload();expect(page.locator('#ac-list tbody tr')).to_have_count(0)
        expect(page.locator('.acc-hidden-bar')).to_contain_text('2 accounts hidden')
        assert not errors,errors
        print('PASS: real Accounts UI hide, reload, unhide, mixed bulk removal, endpoint isolation, no JS errors')
        b.close()
