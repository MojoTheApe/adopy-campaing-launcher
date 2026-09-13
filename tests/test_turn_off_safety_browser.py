"""Offline execution of the production turn-off flow and its real DOM guards."""
import os
from types import SimpleNamespace as NS

import pytest
from playwright.sync_api import sync_playwright
from src.analysis_agent import account_spend as sp
from src.fb_ads_rpa import browser as br


def markup(kind='CAMPAIGN_GROUP', state='true', name='Example', mode=''):
    def cell(field, body):
        return f'<div data-surface="/am/table/table_row:111unit/table_cell:forObjectType({field},{kind})">{body}</div>'
    switch = f'<button role="switch" aria-label="On/off" aria-checked="{state}" onclick="window.clicks++;this.setAttribute(\'aria-checked\',this.getAttribute(\'aria-checked\')===\'true\'?\'false\':\'true\')">toggle</button>'
    if mode == 'disabled': switch=switch.replace('role="switch"','role="switch" disabled')
    if mode == 'forbidden': switch=switch.replace('On/off','Publish')
    row = cell('name',name)+cell('toggle',switch)+cell('delivery','Processing')
    if mode == 'ambiguous-row': row+=cell('toggle',switch)
    table='<div role="table">'+row+'</div>'
    if mode == 'loading': table=table.replace('role="table"','role="table" aria-busy="true"')
    if mode == 'ambiguous-table': table+=table
    if mode == 'hidden': table='<div hidden>'+table+'</div>'
    return '<div role="button" aria-label="Date range: Sep 1, 2026">Sep 1, 2026</div>'+table+'<script>window.clicks=0</script>'

@pytest.fixture(scope='module')
def browser():
    with sync_playwright() as pw:
        b=pw.chromium.launch(executable_path=os.environ.get('ACL_BROWSER_EXECUTABLE') or None)
        yield b
        b.close()

@pytest.fixture
def run(browser, monkeypatch):
    contexts=[]
    def execute(mode='', level='campaign'):
        ctx=browser.new_context();contexts.append(ctx)
        ctx.route('**/*',lambda r:r.fulfill(content_type='text/html',body=''))
        pg=ctx.new_page()
        class Page:
            visits=0
            clicks=0
            confirmations=0
            closed=False
            @property
            def url(self):return pg.url
            def goto(self,url,**kwargs):
                if self.visits:self.clicks+=pg.evaluate('window.clicks')
                self.visits+=1
                if mode == 'wrong-account' or (self.visits > 1 and mode == 'redirect-after-click'):
                    url=url.replace('act=123','act=999')
                if mode == 'wrong-parent':url=url.replace('selected_campaign_ids=222','selected_campaign_ids=333')
                pg.goto(url)
                state='false' if mode == 'already-off' or (self.visits>1 and mode != 'reverted' and mode != 'delayed') else 'true'
                kind='ADGROUP' if level=='ad' else 'CAMPAIGN_GROUP'
                if mode=='wrong-kind':kind='CAMPAIGN'
                pg.set_content(markup(kind,state,'Other' if mode=='wrong-name' else 'Example',mode))
            def evaluate(self,js,arg=None):
                if js==sp._SWITCH_ACTION_JS:
                    if mode=='off-before-click':pg.locator('[role="switch"]').evaluate("e=>e.setAttribute('aria-checked','false')")
                    if mode=='period-before-click':pg.locator('[aria-label^="Date range"]').evaluate("e=>e.setAttribute('aria-label','Date range: Sep 2, 2026')")
                    if mode=='name-before-click':pg.locator('[data-surface*="forObjectType(name"]').evaluate("e=>e.textContent='Other'")
                if js==sp._FIND_SWITCH_JS and self.visits>1:
                    self.confirmations+=1
                    if mode=='delayed' and self.confirmations==2:pg.locator('[role="switch"]').evaluate("e=>e.setAttribute('aria-checked','false')")
                return pg.evaluate(js,arg)
            def locator(self,*args):return pg.locator(*args)
            def wait_for_timeout(self,ms):pg.wait_for_timeout(1)
            def close(self):
                self.closed=True
                self.clicks+=pg.evaluate('window.clicks')
                pg.close()
        page=Page()
        monkeypatch.setattr(sp,'_entry_for',lambda _:NS(ad_account_id='123'))
        monkeypatch.setattr(sp,'_attach_only',lambda *a:NS(page=page,close=lambda **kw:None))
        monkeypatch.setattr(sp,'_wait_for_grid',lambda _:True)
        monkeypatch.setattr(sp,'TURN_OFF_TRIES',3)
        result=sp._turn_off('Example','ad' if level=='ad' else 'campaign','111',parent_id='222' if level=='ad' else '',expected_name='Example',date_from='2026-09-01')
        assert page.closed
        return result,page
    yield execute
    for ctx in contexts:ctx.close()

@pytest.mark.parametrize('level',['campaign','ad'])
def test_confirmed_off_requires_reloaded_correct_context(run,level):
    result,page=run(level=level)
    assert result.state=='off' and result.delivery=='Processing'
    assert page.clicks==1 and page.visits==2

@pytest.mark.parametrize('mode',['wrong-account','wrong-parent','wrong-kind','wrong-name','hidden','disabled','loading','ambiguous-row','ambiguous-table','forbidden','period-before-click','name-before-click'])
def test_unsafe_or_changed_target_is_never_clicked(run,mode):
    result,page=run(mode,level='ad')
    assert result.state=='failed',result
    assert page.clicks==0

@pytest.mark.parametrize('mode',['already-off','off-before-click'])
def test_an_off_switch_can_never_be_turned_on(run,mode):
    result,page=run(mode)
    assert result.state=='already_off' and page.clicks==0

@pytest.mark.parametrize('mode',['redirect-after-click','reverted'])
def test_a_click_without_persistent_confirmation_is_not_success(run,mode):
    result,page=run(mode)
    assert page.clicks==1 and result.state=='failed'


def test_confirmation_waits_for_the_second_observation(run):
    result,page=run('delayed')
    assert result.state=='off' and page.confirmations==2


def test_period_ignores_hidden_dates_and_refuses_visible_ambiguity(browser):
    p=browser.new_page()
    p.set_content('<div hidden><div role="button">Sep 2, 2026</div></div><div role="button">Sep 1, 2026</div>')
    assert sp._period_once(p)=='Sep 1, 2026'
    p.set_content('<div role="button">Sep 2, 2026</div><div role="button">Sep 1, 2026</div>')
    assert sp._period_once(p)==''
    p.close()


def test_background_tab_matches_actual_cdp_identity(browser):
    ctx=browser.new_context()
    original=ctx.new_page()
    owned=br._new_background_page(ctx)
    assert owned is not None and owned is not original
    assert not original.is_closed()
    owned.close()
    assert not original.is_closed()
    ctx.close()


def test_background_timeout_closes_only_owned_target():
    sent=[];detached=[]
    base=NS(wait_for_timeout=lambda ms:None)
    def session(page):
        def send(method,params=None):
            sent.append((method,params))
            return {'targetId':'owned'} if method=='Target.createTarget' else {'targetInfo':{'targetId':'user'}}
        return NS(send=send,detach=lambda:detached.append(True))
    assert br._new_background_page(NS(pages=[base],new_cdp_session=session)) is None
    assert ('Target.closeTarget',{'targetId':'owned'}) in sent
    assert len(detached)==61
