"""ACL-1604: «Publish all» показывает «Not ready» и не ставит такие аккаунты (headless).

Страница настоящая, сеть подменена: проверка отвечает, что Demo разлогинен, а
Other готов. По умолчанию уходит только Other; Demo — только с «Run anyway».
"""
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

from playwright.sync_api import expect
from test_builder_multi_publish_browser import (  # noqa: F401 — фикстуры
    browser, html, ui, add, ready, press_publish,
)

PREFLIGHT = {'accounts': [
    {'account': 'Demo', 'ready': False, 'notes': [], 'reasons': [
        {'code': 'logged_out', 'message': 'logged out of Facebook', 'seen_label': '05.10 14:02',
         'hint': 'log in inside Multilogin, close the window, then publish'}]},
    {'account': 'Other', 'ready': True, 'reasons': [], 'notes': []}]}


def test_not_ready_account_is_listed_and_skipped_unless_run_anyway(ui):
    p, state = ui
    decisions = []

    def route(r):
        if urlparse(r.request.url).path == '/api/publish-preflight/decision':
            decisions.append(r.request.post_data_json)
            r.fulfill(json={'ok': True})
        else:
            r.fulfill(json=PREFLIGHT)
    p.route('**/api/publish-preflight**', route)
    add(p, 0)
    add(p, 1)
    ready(p)

    p.locator('.cbd-publish-all').click()
    modal = p.locator('#safe-confirm-modal')
    expect(modal.locator('.cb-preflight')).to_contain_text('Demo — logged out of Facebook (seen 05.10 14:02)')
    expect(modal.locator('.cb-preflight')).to_contain_text('log in inside Multilogin')
    p.locator('#sfc-ok').click()
    for _ in range(40):
        if state['posts']:
            break
        p.wait_for_timeout(100)
    p.wait_for_timeout(400)
    assert [f['account_label'] for f in state['posts']] == ['Other']
    for _ in range(30):
        if decisions:
            break
        p.wait_for_timeout(100)
    assert decisions == [{'queued': ['Other'], 'skipped': ['Demo'], 'run_anyway': []}]


def test_single_card_publish_is_checked_too(ui):
    """Одиночный Publish: без галки «Run anyway» неготовый аккаунт не уходит."""
    p, state = ui
    decisions = []

    def route(r):
        if urlparse(r.request.url).path == '/api/publish-preflight/decision':
            decisions.append(r.request.post_data_json)
            r.fulfill(json={'ok': True})
        else:
            r.fulfill(json=PREFLIGHT)
    p.route('**/api/publish-preflight**', route)
    add(p, 0)
    ready(p)

    press_publish(p, 0)
    expect(p.locator('#safe-confirm-modal .cb-preflight')).to_contain_text('Demo — logged out of Facebook')
    p.locator('#sfc-ok').click()
    p.wait_for_timeout(800)
    assert state['posts'] == []
    assert any('not ready' in d for d in state['dialogs']), state['dialogs']
    assert decisions[-1] == {'queued': [], 'skipped': ['Demo'], 'run_anyway': []}

    press_publish(p, 0)
    p.locator('#cb-pf-anyway').check()
    p.locator('#sfc-ok').click()
    for _ in range(40):
        if state['posts']:
            break
        p.wait_for_timeout(100)
    assert [f['account_label'] for f in state['posts']] == ['Demo']
    for _ in range(30):
        if len(decisions) >= 2:
            break
        p.wait_for_timeout(100)
    assert decisions[-1] == {'queued': ['Demo'], 'skipped': [], 'run_anyway': ['Demo']}


def test_single_card_publish_falls_back_to_the_old_confirm_when_the_check_fails(ui):
    """Проверка отвечает 500: подтверждение без «Not ready», аккаунт уходит, как раньше."""
    p, state = ui
    p.route('**/api/publish-preflight**', lambda r: r.fulfill(status=500, json={'error': 'boom'}))
    add(p, 0)
    ready(p)
    press_publish(p, 0)
    modal = p.locator('#safe-confirm-modal')
    expect(modal).to_be_visible()
    expect(modal.locator('.cb-preflight')).to_have_count(0)
    p.locator('#sfc-ok').click()
    for _ in range(40):
        if state['posts']:
            break
        p.wait_for_timeout(100)
    assert [f['account_label'] for f in state['posts']] == ['Demo']
