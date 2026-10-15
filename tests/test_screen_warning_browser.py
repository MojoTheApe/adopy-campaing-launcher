"""Render the real dashboard and poll synthetic job snapshots, without live IO."""
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from urllib.parse import urlparse

import pytest
from playwright.sync_api import sync_playwright, expect
from test_builder_per_account import _render


@pytest.fixture(scope='module')
def browser():
    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=os.environ.get('ACL_BROWSER_EXECUTABLE') or None)
        yield b
        b.close()


@pytest.mark.parametrize('status,publishable,locked,warning', [
    ('done', False, True, True),
    ('done', False, False, False),
    ('done', False, None, False),
    ('done', True, True, False),
    ('cancelled', False, True, False),
    ('running', False, True, False),
])
def test_warning_preserves_actual_error_and_only_explains_failed_jobs(browser, status, publishable, locked, warning):
    html = _render({'Demo': []})
    context = browser.new_context()
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    job = {'status': status, 'publishable': publishable, 'screen_was_locked': locked,
           'total': 1, 'done_count': 1,
           'results': [{'success': publishable, 'publishable': publishable,
                        'campaign_name': 'Synthetic campaign',
                        'error': None if publishable else 'Creative file rejected <details>'}],
           'errors': [] if publishable else ['Original account error <details>']}
    def route(r):
        if r.request.resource_type == 'document':
            r.fulfill(content_type='text/html', body=html)
        elif urlparse(r.request.url).path.startswith('/publish-jobs/'):
            r.fulfill(json=job)
        else:
            r.fulfill(json={'files': [], 'texts': [], 'profiles': [], 'waiting': False})
    page.route('**/*', route)
    try:
        page.goto('http://acl.test/')
        page.evaluate('''() => {
          const p=document.querySelector('.cb-status-panel');
          p.style.display='block';
          cbPollJob('synthetic-job',p,null);
        }''')
        panel = page.locator('.cb-status-panel').first
        expect(panel.locator('.cb-status-title')).to_contain_text('1/1')
        expect(panel.locator('.cb-status-cause')).to_have_count(1 if warning else 0)
        if warning:
            expect(panel.locator('.cb-status-cause')).to_contain_text('screen was locked')
            expect(panel.locator('.cb-status-cause')).to_contain_text('Check the errors below')
            expect(panel.locator('.cb-status-cause')).not_to_contain_text('всё в порядке')
        if not publishable:
            expect(panel.locator('.cb-status-fatal')).to_contain_text('Original account error <details>')
            expect(panel.locator('.cb-status-err')).to_contain_text('Creative file rejected <details>')
            expect(panel.locator('details')).to_have_count(0)
        assert not errors, errors
    finally:
        context.close()
