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
        def __init__(self): self.contexts, self.tags, self.fingerprint = {}, {}, None
        def set_tag(self, key, val): self.tags[key] = val
        def set_context(self, key, val): self.contexts[key] = val
    current = []
    @contextmanager
    def new_scope():
        # Свой scope на каждое событие, как у настоящего SDK: иначе отпечаток
        # прошлого вызова «протёк» бы в следующий и тест ничего бы не доказал.
        current.append(Scope())
        yield current[-1]
    monkeypatch.setattr(acl_sentry, 'init', lambda: True)
    monkeypatch.setattr(sentry_sdk, 'new_scope', new_scope)
    def capture(message, **kwargs):
        scope = current[-1]
        # SDK переносит отпечаток scope в событие до before_send — значит, он
        # проходит через вычиститель, и проверять его надо уже после.
        event = {'message': message, 'contexts': scope.contexts,
                 'tags': dict(scope.tags), 'fingerprint': scope.fingerprint}
        events.append(acl_sentry._before_send(event, {}))
        return 'synthetic-event'
    monkeypatch.setattr(sentry_sdk, 'capture_message', capture)
    return events


def test_long_report_and_failure_reason_reach_the_event(captured):
    story = 'detail ' * 120 + 'the decisive last sentence'
    acl_sentry.report_problem(story)
    assert story in json.dumps(captured[-1])
    acl_sentry.report_step_failure('placements', 'timeout while opening synthetic category')
    assert 'timeout while opening synthetic category' in json.dumps(captured[-1])


def test_each_manual_report_is_its_own_sentry_issue(captured):
    """ACL-1273: Sentry уведомляет только о новых тикетах, а тикет = отпечаток."""
    acl_sentry.report_problem('Publish button does nothing')
    acl_sentry.report_problem('Placements step is stuck')
    acl_sentry.report_problem('still broken')
    acl_sentry.report_problem('still broken')
    prints = [tuple(e['fingerprint']) for e in captured]
    assert len(set(prints)) == 4, prints
    for event, fp in zip(captured, prints):
        assert fp[:2] == ('acl-report', 'manual')
        # Номер пережил вычиститель целиком и совпадает с тегом обращения.
        assert fp[2] == event['tags']['report_id']
        assert fp[2].startswith('r') and len(fp[2]) == 33


def test_report_id_survives_the_scrubber_even_when_all_digits(captured, monkeypatch):
    class DigitsOnly:
        hex = '1' * 32
    monkeypatch.setattr(acl_sentry.uuid, 'uuid4', lambda: DigitsOnly())
    acl_sentry.report_problem('synthetic report')
    assert captured[-1]['fingerprint'] == ['acl-report', 'manual', 'r' + '1' * 32]


def test_step_failures_still_group_by_step_and_reason(captured):
    acl_sentry.report_step_failure('placements', 'timeout while opening synthetic category')
    acl_sentry.report_step_failure('placements', 'timeout while opening another synthetic category')
    first, second = captured[-2]['fingerprint'], captured[-1]['fingerprint']
    assert first == second
    assert first[:2] == ['acl-step', 'placements'] and len(first) == 3


def test_report_keeps_its_tags_and_machine_context(captured, monkeypatch):
    monkeypatch.setattr(acl_sentry, '_machine_tags', {'machine': 'synthetic-pc'})
    failures = [{'step': 'placements', 'reason': 'timeout'}]
    acl_sentry.report_problem('synthetic report', context={'where': 'builder', 'last_failures': failures})
    event = captured[-1]
    assert event['tags']['kind'] == 'report'
    assert event['tags']['machine'] == 'synthetic-pc'
    assert event['contexts']['acl']['last_failures'] == failures
    assert event['contexts']['acl']['where'] == 'builder'


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
