"""Run the real builder factory, duplicate and targeting handlers offline."""
import json
import os
from pathlib import Path
import re

import pytest
from jinja2 import Environment
from playwright.sync_api import sync_playwright

REPORT = Path(__file__).resolve().parents[1] / 'src/web/templates/report.html'

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
