"""Exercise the actual upload entry point and dashboard on synthetic pages."""
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

import pytest
from playwright.sync_api import sync_playwright, expect

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_builder_per_account import _render
from src.fb_ads_rpa import creative_compress as cc, meta_dom


@pytest.fixture(scope='module')
def browser():
    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=os.environ.get('ACL_BROWSER_EXECUTABLE') or None)
        yield b
        b.close()


@pytest.mark.parametrize("reuse", ["disabled", "miss", "hit"])
def test_upload_waits_for_the_actual_compressed_filename(browser, tmp_path, monkeypatch, reuse):
    original = tmp_path / 'original.mp4'
    original.write_bytes(b'original')
    derived = tmp_path / 'original.cache-key.mp4'
    derived.write_bytes(b'encoded')
    monkeypatch.setattr(cc, 'ensure_uploadable', lambda path: cc.Prepared(str(derived), 247, 24, True, 1))
    monkeypatch.setattr(meta_dom, '_scroll_until_mounted', lambda *a, **k: None)
    monkeypatch.setattr(meta_dom, '_sleep_h', lambda *a, **k: None)
    monkeypatch.setattr(meta_dom, 'get_reuse_library_media', lambda: reuse != 'disabled')
    monkeypatch.setattr(meta_dom, 'wait_editor_saved', lambda *a, **k: None)
    context = browser.new_context()
    context.route('**/*', lambda r: r.fulfill(body='<html></html>', content_type='text/html'))
    page = context.new_page()
    page.goto('http://ads.test/ads/edit/')
    page.set_content('''<div id="wizard"><h1>Set up your creative</h1>
      <input placeholder="Search media">
      <button>Upload</button><input type="file" onchange="
        window.uploadFilter=document.querySelector('input[placeholder]').value;
        document.getElementById('tile').style.display=window.uploadFilter ? 'none' : 'inline';
        document.getElementById('tile').textContent=this.files[0].name;
        document.getElementById('selected').textContent='1 selected';">
      <span id="tile"></span><span id="selected">0 selected</span>
      <button onclick="document.getElementById('wizard').remove();
        document.getElementById('editor').textContent='Edit media';">Done</button></div>
      <div id="editor"></div>''')
    searched = []
    def reuse_copy(p, name):
        searched.append(name)
        p.get_by_placeholder('Search media').fill(name)
        if reuse == 'hit' and name == derived.name:
            p.evaluate("document.getElementById('wizard').remove(); document.getElementById('editor').textContent='Edit media'")
            return True
        return False
    monkeypatch.setattr(meta_dom, '_try_reuse_library_media', reuse_copy)
    notices = []
    try:
        with cc.observe_preparation(notices.append):
            assert meta_dom.upload_ad_creative(page, 'video', str(original))
        assert page.locator('#editor').inner_text() == 'Edit media'
        if reuse != 'disabled':
            assert searched == [original.name, derived.name]
        if reuse == 'hit':
            assert meta_dom._LAST_UPLOAD_DIAG['reused'] is True
            assert meta_dom._LAST_UPLOAD_DIAG['attempts'] == []
            assert page.evaluate('window.uploadFilter === undefined')
        else:
            assert len(meta_dom._LAST_UPLOAD_DIAG['attempts']) == 1
            assert meta_dom._LAST_UPLOAD_DIAG['attempts'][0]['tile'] is True
            assert page.evaluate('window.uploadFilter') == ''
        assert meta_dom._LAST_UPLOAD_DIAG['prepared']['seconds'] == 1
        assert notices and 'original.mp4' in notices[0]
    finally:
        context.close()


def test_compression_notice_visible_and_escaped_while_running(browser):
    html = _render({'Demo': []})
    context = browser.new_context()
    page = context.new_page()
    notice = '<img src=x onerror=alert(1)>.mp4: сжат перед отправкой: 247 МБ → 24.0 МБ за 2с'
    def route(r):
        if r.request.resource_type == 'document':
            r.fulfill(content_type='text/html', body=html)
        elif urlparse(r.request.url).path.startswith('/publish-jobs/'):
            r.fulfill(json={'status': 'running', 'total': 1, 'done_count': 0,
                            'creative_notices': [notice], 'results': [], 'errors': []})
        else:
            r.fulfill(json={'files': [], 'texts': [], 'profiles': [], 'waiting': False})
    page.route('**/*', route)
    try:
        page.goto('http://acl.test/')
        page.evaluate('''() => {const p=document.querySelector('.cb-status-panel');
          p.style.display='block'; cbPollJob('synthetic-job',p,null);}''')
        panel = page.locator('.cb-status-panel').first
        expect(panel.locator('.cb-status-cause')).to_have_text(notice)
        expect(panel.locator('img')).to_have_count(0)
        expect(panel.locator('.cb-status-title')).to_contain_text('running')
    finally:
        context.close()
