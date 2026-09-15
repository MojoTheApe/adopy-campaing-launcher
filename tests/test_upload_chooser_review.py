"""Real offline Chrome events, including an out-of-process media frame."""
import os

import pytest
from playwright.sync_api import sync_playwright

from src.fb_ads_rpa import meta_dom


@pytest.mark.parametrize('in_child_frame', [False, True])
def test_delayed_chooser_uploads_by_path_in_its_own_target(tmp_path, in_child_frame):
    creative = tmp_path / 'synthetic-large.mp4'
    with creative.open('wb') as fh:
        fh.truncate(51 * 1024 * 1024)
    child_html = '''<button onclick="setTimeout(()=>document.querySelector('input').click(),150)">Upload</button><input type="file">'''
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get('ACL_BROWSER_EXECUTABLE') or None,
                                     args=['--site-per-process'])
        try:
            context = browser.new_context()
            def route(r):
                html = '<iframe src="http://media.test/"></iframe>' if in_child_frame and 'parent.test' in r.request.url else child_html
                r.fulfill(body=html, content_type='text/html')
            context.route('**/*', route)
            page = context.new_page()
            page.goto('http://parent.test/')
            frame = page.frame(url='http://media.test/') if in_child_frame else page.main_frame
            diag = {}
            ok = meta_dom._upload_heavy_via_chooser_event(
                page, str(creative), diag, lambda: frame.locator('button').click() or True, wait_s=2)
            assert ok, diag
            assert frame.locator('input').evaluate('(e)=>[e.files[0].name,e.files[0].size]') == [creative.name, creative.stat().st_size]
            # Our temporary interception must leave Playwright's next chooser usable.
            with page.expect_file_chooser(timeout=3000) as chooser:
                frame.locator('button').click()
            chooser.value.set_files([])
        finally:
            browser.close()


def test_early_upload_failure_does_not_reuse_previous_creative_diagnostics(monkeypatch):
    meta_dom._LAST_UPLOAD_DIAG = {"file": "previous.mp4", "heavy_error": "previous error"}
    def stop(*args, **kwargs):
        raise RuntimeError('synthetic failure before upload UI')
    monkeypatch.setattr(meta_dom, '_scroll_until_mounted', stop)
    with pytest.raises(RuntimeError):
        meta_dom.upload_ad_creative(object(), 'video', '/synthetic/current.mp4')
    assert meta_dom._LAST_UPLOAD_DIAG == {"file": "current.mp4", "attempts": []}
