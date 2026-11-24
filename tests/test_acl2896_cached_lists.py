"""ACL-2896: ответы билдера только из кэша не ждут чтений профиля, Refresh не висит за ними."""
from __future__ import annotations

import asyncio
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent))
from test_builder_review_browser import browser, page  # noqa: E402,F401

ROOT = Path(__file__).resolve().parents[1]
HDR = {'Origin': 'http://127.0.0.1:9090'}


def _ask_while_pool_is_full(monkeypatch, url: str, data: dict) -> tuple[httpx.Response, set[str]]:
    """Общий пул цикла забит висящими «чтениями профиля»; запрос только из кэша должен ответить сразу."""
    from src.web import app as webapp

    async def run():
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=2))
        release = threading.Event()
        blocked = [loop.run_in_executor(None, release.wait) for _ in range(2)]
        try:
            transport = httpx.ASGITransport(app=webapp.app, raise_app_exceptions=False)
            async with httpx.AsyncClient(transport=transport, base_url='http://127.0.0.1:9090') as client:
                res = await asyncio.wait_for(client.post(url, headers=HDR, data=data), timeout=5)
        finally:
            release.set()
            await asyncio.gather(*blocked)
        return res

    return asyncio.run(run())


def _recording(result):
    threads: set[str] = set()

    def fn(*_a, **_k):
        threads.add(threading.current_thread().name)
        return result
    return fn, threads


def test_cached_pages_answer_while_default_pool_is_full(monkeypatch):
    from src.web import account_pages
    fn, threads = _recording({'ok': True, 'state': 'not_loaded', 'pages': []})
    monkeypatch.setattr(account_pages, 'cached_only', fn)
    res = _ask_while_pool_is_full(monkeypatch, '/api/account-pages', {'account_label': 'Demo', 'cached': '1'})
    assert res.status_code == 200 and res.json()['state'] == 'not_loaded'
    assert threads and all(t.startswith('acl-cached') for t in threads)


def test_cached_pixels_answer_while_default_pool_is_full(monkeypatch):
    from src.web import account_pixels
    fn, threads = _recording({'ok': True, 'state': 'ready', 'pixels': [{'id': '1', 'name': 'A'}]})
    monkeypatch.setattr(account_pixels, 'cached_only', fn)
    res = _ask_while_pool_is_full(monkeypatch, '/api/account-pixels', {'account_label': 'Demo', 'cached': '1'})
    assert res.status_code == 200 and res.json()['pixels'] == [{'id': '1', 'name': 'A'}]
    assert threads and all(t.startswith('acl-cached') for t in threads)


def test_cached_plan_answers_while_default_pool_is_full(monkeypatch):
    from src.web import app as webapp, publish_api
    monkeypatch.setattr(publish_api, 'api_enabled', lambda: True)
    monkeypatch.setattr(webapp, '_parse_build_upload_form', lambda form: ('Mexico', 'Demo', [{'name': 'C'}]))
    fn, threads = _recording({'account': 'Demo', 'results': [{'campaign_name': 'C', 'cached': True}]})
    monkeypatch.setattr(publish_api, 'cached_plan', fn)
    res = _ask_while_pool_is_full(monkeypatch, '/publish-plan/cached', {'dry_run': '1'})
    assert res.status_code == 200 and res.json()['results'][0]['campaign_name'] == 'C'
    assert threads and all(t.startswith('acl-cached') for t in threads)


def test_profile_reads_still_use_the_shared_pool(monkeypatch):
    """Не-кэшевые чтения не переехали в пул кэша: он остаётся только для ответов без профиля."""
    src = (ROOT / 'src/web/app.py').read_text(encoding='utf-8')
    assert 'asyncio.to_thread(read_pages, account, force=force)' in src
    assert 'asyncio.to_thread(read_pixels, account, force=force)' in src
    assert src.count('_in_cached_pool(') == 4  # определение + pages + pixels + cached plan


# ───────────────────────── Refresh во время ожидания кэша ─────────────────────────

PIXELS = [{'id': '222', 'name': 'Main pixel'}, {'id': '333', 'name': 'Second pixel'}]
PAGES = [{'id': '123', 'name': 'First Page'}, {'id': '456', 'name': 'Second Page'}]


def _hang_cached(page, url, ready):
    """Ответ кэша не приходит (висит за чтениями профиля); настоящее чтение отвечает списком."""
    held, real = [], []

    def handle(route):
        if 'name="cached"' in (route.request.post_data or ''):
            held.append(route)
        else:
            real.append(route.request)
            route.fulfill(json=ready)
    page.route(url, handle)
    return held, real


def test_refresh_pixels_starts_a_real_read_while_cached_request_hangs(page):
    held, real = _hang_cached(page, '**/api/account-pixels', {'ok': True, 'state': 'ready', 'pixels': PIXELS})
    page.add_script_tag(path=str(ROOT / 'src/web/static/account_pixels.js'))
    page.wait_for_function("document.querySelector('[data-pixel-note]').textContent.includes('Loading pixels')")
    assert held and not real
    page.evaluate("cbRefreshPixels(document.querySelector('.cb-slot'), true)")
    page.wait_for_function("document.querySelector('[data-pixel-note]').textContent.includes('2 pixels')")
    assert len(real) == 1 and 'name="force"' in real[0].post_data
    assert page.locator('.cb-pixel-select').first.is_enabled()
    # Поздний ответ кэша не затирает прочитанный список.
    held[0].fulfill(json={'ok': True, 'state': 'not_loaded', 'pixels': [], 'message': 'Pixels are not loaded yet.'})
    page.wait_for_timeout(200)
    assert '2 pixels' in page.locator('[data-pixel-note]').first.inner_text()


def test_refresh_pages_starts_a_real_read_while_cached_request_hangs(page):
    held, real = _hang_cached(page, '**/api/account-pages', {'ok': True, 'state': 'ready', 'pages': PAGES})
    page.add_script_tag(path=str(ROOT / 'src/web/static/account_pages.js'))
    page.wait_for_function("document.querySelector('[data-page-note]').textContent.includes('Loading Facebook Pages')")
    assert held and not real
    page.evaluate("cbRefreshPages(document.querySelector('.cb-slot'), true)")
    page.wait_for_function("document.querySelector('[data-page-note]').textContent.includes('First Page')")
    assert len(real) == 1 and 'name="force"' in real[0].post_data
    held[0].fulfill(json={'ok': True, 'state': 'not_loaded', 'pages': [], 'message': 'Pages are not loaded yet.'})
    page.wait_for_timeout(200)
    assert 'First Page' in page.locator('[data-page-note]').first.inner_text()


def test_refresh_does_not_duplicate_a_real_read_in_flight(page):
    """Дедупликация настоящего чтения осталась: второй Refresh при идущем чтении профиля ничего не шлёт."""
    reads = []

    def handle(route):
        if 'name="cached"' in (route.request.post_data or ''):
            route.fulfill(json={'ok': True, 'state': 'not_loaded', 'pixels': []})
        else:
            reads.append(route)  # чтение профиля идёт и не отвечает
    page.route('**/api/account-pixels', handle)
    page.add_script_tag(path=str(ROOT / 'src/web/static/account_pixels.js'))
    page.wait_for_function("!document.querySelector('[data-pixel-note]').textContent.includes('Loading')")
    page.evaluate("cbRefreshPixels(document.querySelector('.cb-slot'), true)")
    page.wait_for_function("document.querySelector('[data-pixel-note]').textContent.includes('Loading pixels')")
    page.evaluate("cbRefreshPixels(document.querySelector('.cb-slot'), true)")
    page.evaluate("cbSyncPixels(document.querySelector('.cb-slot'))")
    page.wait_for_timeout(200)
    assert len(reads) == 1
    reads[0].fulfill(json={'ok': True, 'state': 'ready', 'pixels': PIXELS})
    page.wait_for_function("document.querySelector('[data-pixel-note]').textContent.includes('2 pixels')")
