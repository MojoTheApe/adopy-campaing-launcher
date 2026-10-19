"""Reports preserve useful details without exporting secrets or claiming delivery."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
from contextlib import contextmanager

import pytest
from src import acl_sentry
from src.web import app as web


def test_nested_secret_fields_are_removed_at_final_event_boundary():
    event = {'contexts': {'diag': {'password': 'synthetic-secret', 'nested': [{'TOKEN': 'synthetic-token'}]}}}
    safe = acl_sentry._before_send(event, {})
    assert safe['contexts']['diag']['password'] == '<hidden>'
    assert safe['contexts']['diag']['nested'][0]['TOKEN'] == '<hidden>'
    assert event['contexts']['diag']['password'] == 'synthetic-secret'


def test_test_bootstrap_overrides_an_inherited_dsn():
    env = dict(os.environ, ACL_SENTRY_DSN='https://public@example.invalid/1')
    result = subprocess.run([sys.executable, '-c', 'import runpy, os; runpy.run_path("tests/conftest.py"); assert os.environ["ACL_SENTRY_DSN"] == ""'], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.fixture
def captured(monkeypatch):
    import sentry_sdk
    events = []
    class Scope:
        def __init__(self): self.contexts = {}
        def set_tag(self, *args): pass
        def set_context(self, key, val): self.contexts[key] = val
    scope = Scope()
    @contextmanager
    def new_scope(): yield scope
    monkeypatch.setattr(acl_sentry, 'init', lambda: True)
    monkeypatch.setattr(sentry_sdk, 'new_scope', new_scope)
    def capture(message, **kwargs):
        events.append(acl_sentry._before_send({'message': message, 'contexts': scope.contexts}, {}))
        return 'synthetic-event'
    monkeypatch.setattr(sentry_sdk, 'capture_message', capture)
    return events


def test_long_report_and_failure_reason_reach_the_event(captured):
    story = 'detail ' * 120 + 'the decisive last sentence'
    acl_sentry.report_problem(story)
    assert story in json.dumps(captured[-1])
    acl_sentry.report_step_failure('placements', 'timeout while opening synthetic category')
    assert 'timeout while opening synthetic category' in json.dumps(captured[-1])


def test_report_route_returns_queue_state_and_selected_dates(monkeypatch):
    seen = {}
    monkeypatch.setattr(acl_sentry, 'report_problem', lambda text, context: seen.update(context) or 'synthetic-event')
    class Request:
        async def form(self, **_limits): return {'text': 'Synthetic report', 'from': '2026-09-01', 'to': '2026-09-15', 'span': ''}
    response = asyncio.run(web.report_problem(Request()))
    assert response.status_code == 202
    assert json.loads(response.body)['queued'] is True
    assert seen['period']['from'] == '2026-09-01'


@pytest.mark.parametrize('success', [True, False])
def test_actual_report_dialog_preserves_text_and_distinguishes_queue(success):
    from playwright.sync_api import sync_playwright, expect
    source = Path('src/web/templates/report.html').read_text(encoding='utf-8')
    start = source.index('<dialog id="report-problem-box"')
    end = source.index('</script>', start) + len('</script>')
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get('ACL_BROWSER_EXECUTABLE') or None)
        try:
            context = browser.new_context()
            context.route('**/*', lambda route: route.fulfill(status=202 if success else 502, content_type='application/json', body=json.dumps({'ok': success, 'queued': success, 'event_id': 'synthetic-event'})))
            page = context.new_page()
            page.goto('http://acl.test/')
            page.set_content('<button id="report-problem-btn">Report</button>' + source[start:end])
            page.locator('#report-problem-btn').click()
            page.locator('#report-problem-text').fill('Synthetic report')
            page.locator('#report-problem-send').click()
            expect(page.locator('#report-problem-msg')).to_contain_text('Queued' if success else 'not confirmed')
            assert page.locator('#report-problem-text').input_value() == 'Synthetic report'
        finally:
            browser.close()
