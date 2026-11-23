"""ACL-2850: Pixel list loads faster - one profile session with Pages, reuse of an open session,
progress text, timing telemetry."""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent))
from acl2850_fakes import ACT, fake_session  # noqa: E402

PIXELS = [{'id': '1', 'name': 'Main'}, {'id': '2', 'name': 'Second'}]
PAGES = [{'id': '10', 'name': 'Page'}]


def _client(calls):
    def get_all(path, params, **kw):
        calls.append(path)
        return PIXELS
    return SimpleNamespace(allow_writes=False, reset_budget=lambda n: None, calls=0, writes=[], stopped=None, get_all=get_all)


def _setup(monkeypatch, tmp_path, *, pages=None, fail_pages=None):
    from src.web import account_pages as pg, account_pixels as px
    monkeypatch.setattr(px, '_cache_path', lambda: tmp_path / 'account_pixels_cache.json')
    monkeypatch.setattr(pg, '_cache_path', lambda: tmp_path / 'account_pages_cache.json')
    page_reads = []

    def pages_from_client(client, act, *, counts=None):
        page_reads.append(act)
        if fail_pages:
            raise ValueError(fail_pages)
        return list(PAGES if pages is None else pages)
    monkeypatch.setattr(pg, 'pages_from_client', pages_from_client)
    calls, opened = [], []
    fake_session(monkeypatch, opened=opened, client=_client(calls))
    events = []
    from src.analysis_agent import account_spend_api as api
    monkeypatch.setattr(api._tm, 'record', lambda event, *a, **k: events.append(event))
    return SimpleNamespace(px=px, pg=pg, calls=calls, opened=opened, page_reads=page_reads, events=events)


# ───────────────────────── one session for both lists ─────────────────────────

def test_pixel_click_opens_one_session_and_fills_both_caches(monkeypatch, tmp_path):
    t = _setup(monkeypatch, tmp_path)
    out = t.px.read_pixels('Demo')
    assert out['state'] == 'ready' and out['pixels'] == PIXELS
    assert t.opened == ['Demo']                                   # one profile session
    assert t.calls == [f'/{ACT}/adspixels']             # the single pixel request
    assert t.px.cached_only('Demo')['state'] == 'ready'
    assert t.pg.cached_only('Demo')['state'] == 'ready' and t.pg.cached_only('Demo')['pages'] == PAGES


def test_pages_click_also_fills_the_pixel_cache(monkeypatch, tmp_path):
    t = _setup(monkeypatch, tmp_path)
    out = t.pg.read_pages('Demo')
    assert out['state'] == 'ready' and out['pages'] == PAGES
    assert t.opened == ['Demo']
    assert t.px.cached_only('Demo')['pixels'] == PIXELS


def test_second_request_after_the_first_opens_nothing(monkeypatch, tmp_path):
    t = _setup(monkeypatch, tmp_path)
    t.px.read_pixels('Demo')
    assert t.pg.read_pages('Demo')['state'] == 'ready'            # the builder fires both at once
    assert t.opened == ['Demo']


def test_waiting_request_takes_the_caches_filled_while_it_queued(monkeypatch, tmp_path):
    from src.web import account_lists
    t = _setup(monkeypatch, tmp_path)
    t.px.read_pixels('Demo')
    t.opened.clear()
    # The second request passed the cache check, then queued behind the first one: inside the queue it re-checks.
    out = account_lists._job('Demo', 'pages', False, 0.0)
    assert out['pages']['pages'] == PAGES and t.opened == []


def test_pixel_click_reads_only_pixels_when_pages_are_cached(monkeypatch, tmp_path):
    t = _setup(monkeypatch, tmp_path)
    t.pg.remember_pages('Demo', {'ok': True, 'state': 'ready', 'pages': PAGES})
    t.px.read_pixels('Demo', force=True)
    assert t.opened == ['Demo'] and t.page_reads == []


def test_each_list_keeps_its_own_failure(monkeypatch, tmp_path):
    t = _setup(monkeypatch, tmp_path, fail_pages='Pages broke')
    out = t.px.read_pixels('Demo')
    assert out['state'] == 'ready'                                # pixels are fine
    assert t.px.cached_only('Demo')['state'] == 'ready'
    assert t.pg.cached_only('Demo')['state'] == 'not_loaded'      # failed pages are not cached (ACL-2816)
    shown = t.pg.read_pages('Demo')
    assert shown['state'] == 'failed' and 'Pages broke' in shown['message']


def test_pixel_failure_does_not_hide_the_pages(monkeypatch, tmp_path):
    t = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(t.px, '_read_in_session', lambda sess, account: (_ for _ in ()).throw(RuntimeError('no pixels')))
    out = t.px.read_pixels('Demo')
    assert out['state'] == 'failed' and 'no pixels' in out['message'] and out['ok'] is False
    assert t.pg.cached_only('Demo')['state'] == 'ready'


def test_empty_pixel_answer_is_still_not_cached(monkeypatch, tmp_path):
    t = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(t.px, 'pixels_from_client', lambda client, act: [])
    assert t.px.read_pixels('Demo')['state'] == 'empty'
    assert t.px.cached_only('Demo')['state'] == 'not_loaded'


def test_session_that_cannot_open_fails_both_without_caching(monkeypatch, tmp_path):
    t = _setup(monkeypatch, tmp_path)
    fake_session(monkeypatch, opened=t.opened, fail=RuntimeError('profile closed'))
    out = t.px.read_pixels('Demo')
    assert out['state'] == 'failed' and 'profile closed' in out['message']
    assert t.px.cached_only('Demo')['state'] == 'not_loaded' and t.pg.cached_only('Demo')['state'] == 'not_loaded'


# ───────────────────────── reuse inside Check plan / Publish ─────────────────────────

def test_check_plan_facts_fill_an_empty_pixel_cache_without_a_call(monkeypatch, tmp_path):
    t = _setup(monkeypatch, tmp_path)
    from src.fb_api.creator import AccountFacts
    facts = AccountFacts(ACT, 'USD', ['123'], ['1', '2'], pixel_names={'1': 'Main'})
    t.px.fill_from_facts('Demo', facts)
    got = t.px.cached_only('Demo')
    assert got['state'] == 'ready' and got['pixels'] == [{'id': '1', 'name': 'Main'}, {'id': '2', 'name': '2'}]
    assert t.opened == [] and t.calls == []
    # A fresh cache is left alone; no pixels on the account caches nothing (ACL-2835).
    t.px.fill_from_facts('Demo', AccountFacts(ACT, 'USD', ['123'], ['9']))
    assert [p['id'] for p in t.px.cached_only('Demo')['pixels']] == ['1', '2']
    t.px.fill_from_facts('Other', AccountFacts(ACT, 'USD', ['123'], []))
    assert t.px.cached_only('Other')['state'] == 'not_loaded'


def test_check_plan_session_fills_the_pixel_cache(monkeypatch, tmp_path):
    from src.web import publish_api
    t = _setup(monkeypatch, tmp_path)
    from src.fb_api.creator import AccountFacts
    from src.analysis_agent import account_spend as accounts, account_spend_api as api
    facts = AccountFacts(ACT, 'USD', ['123'], ['7'], pixel_names={'7': 'Seven'})
    monkeypatch.setattr(publish_api, 'preflight_account', lambda client, act: facts)
    monkeypatch.setattr(publish_api, 'TargetingResolver', lambda *a, **k: SimpleNamespace(facts_stats=lambda: {}))
    monkeypatch.setattr(accounts, 'action_queued', lambda label: __import__('contextlib').nullcontext())
    publish_api._plan_in_read_session('Demo', [])
    assert t.opened == ['Demo']                                   # the one Check plan session
    assert t.px.cached_only('Demo')['pixels'] == [{'id': '7', 'name': 'Seven'}]
    assert t.calls == []                                          # no extra Graph request


# ───────────────────────── progress text ─────────────────────────

def test_stream_sends_the_wait_note_then_the_result(monkeypatch):
    from src.analysis_agent import account_spend as accounts
    from src.web import account_lists
    release, started = threading.Event(), threading.Event()

    def slow(account, primary, force=False):
        started.set()
        release.wait(5)
        return {'ok': True, 'state': 'ready', 'pixels': PIXELS}
    monkeypatch.setattr(account_lists, 'read_lists', slow)
    items = []

    def consume():
        for item in account_lists.stream('Demo', 'pixels'):
            items.append(item)
    th = threading.Thread(target=consume)
    th.start()
    assert started.wait(5)
    accounts._set_wait_note('Demo', accounts.SLOT_WAIT_NOTE)
    deadline = time.monotonic() + 5
    while not items and time.monotonic() < deadline:
        time.sleep(0.01)
    accounts._set_wait_note('Demo', '')
    release.set()
    th.join(5)
    assert not th.is_alive()
    notes = [i['progress'] for i in items if 'progress' in i]
    assert notes == [accounts.SLOT_WAIT_NOTE]
    assert items[-1] == {'result': {'ok': True, 'state': 'ready', 'pixels': PIXELS}}


def test_endpoint_streams_ndjson_for_the_pixel_field(monkeypatch):
    from fastapi.testclient import TestClient
    from src.web import account_pixels as px, app as webapp
    monkeypatch.setattr(px, 'stream_pixels', lambda account, force=False: iter(
        [{'progress': 'Waiting for a free profile slot'}, {'result': {'ok': True, 'state': 'ready', 'pixels': PIXELS}}]))
    r = TestClient(webapp.app, base_url='http://127.0.0.1:9090').post(
        '/api/account-pixels', headers={'Origin': 'http://127.0.0.1:9090'},
        data={'account_label': 'Demo', 'stream': '1'})
    assert r.headers['content-type'].startswith('application/x-ndjson')
    rows = [json.loads(line) for line in r.text.splitlines()]
    assert rows[0] == {'progress': 'Waiting for a free profile slot'} and rows[1]['result']['pixels'] == PIXELS


def test_pixel_field_shows_the_progress_text_and_uses_no_timers():
    js = (Path(__file__).parents[1] / 'src/web/static/account_pixels.js').read_text(encoding='utf-8')
    assert "state.progress || 'Loading pixels" in js
    assert 'setInterval' not in js and 'setTimeout' not in js


# ───────────────────────── telemetry ─────────────────────────

def test_pixel_read_writes_fb_api_read_with_wait_capture_and_call_seconds(monkeypatch, tmp_path):
    t = _setup(monkeypatch, tmp_path)
    t.px.read_pixels('Demo')
    reads = [e for e in t.events if e.get('event') == 'fb_api_read']
    assert len(reads) == 1
    event = reads[0]
    assert event['kind'] == 'pixels' and event['ok'] is True and event['lists'] == 'pixels+pages'
    for key in ('wait_s', 'capture_s', 'call_s', 'took_s'):
        assert isinstance(event[key], float) and event[key] >= 0


def test_failed_open_still_writes_the_timing_event(monkeypatch, tmp_path):
    t = _setup(monkeypatch, tmp_path)
    fake_session(monkeypatch, opened=t.opened, fail=RuntimeError('profile closed'))
    t.px.read_pixels('Demo')
    event = next(e for e in t.events if e.get('event') == 'fb_api_read')
    assert event['kind'] == 'pixels' and event['ok'] is False and event['call_s'] == 0.0
    assert 'capture_s' in event and 'wait_s' in event
