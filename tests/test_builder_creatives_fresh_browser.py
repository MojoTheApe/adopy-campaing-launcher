# -*- coding: utf-8 -*-
"""ACL-1651: файл, положенный в папку креативов, находится поиском Билдера без
перезагрузки страницы и без переключения типа. Настоящий /api/creatives читает
временную папку (get_creatives_root), настоящий JS Билдера идёт в Chromium."""
import asyncio
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from jinja2 import Environment
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
REPORT = ROOT / 'src/web/templates/report.html'


@pytest.fixture(scope='module')
def browser():
    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=os.environ.get('ACL_BROWSER_EXECUTABLE') or None)
        yield b
        b.close()


@pytest.fixture
def env(browser, tmp_path, monkeypatch):
    from src.web import app as webapp
    monkeypatch.setattr(webapp, 'get_creatives_root', lambda: str(tmp_path))
    (tmp_path / '1111_old.jpg').write_bytes(b'x')
    hits = []

    def scan() -> bytes:
        return asyncio.run(webapp.api_creatives()).body

    pool = ThreadPoolExecutor(max_workers=1)

    def route(r):
        if r.request.url.endswith('/api/creatives') or '/api/creatives?' in r.request.url:
            hits.append(1)
            r.fulfill(content_type='application/json', body=pool.submit(scan).result())
        else:
            r.fulfill(content_type='application/json', body='{"files":[],"texts":[]}')

    src = REPORT.read_text(encoding='utf-8')
    start = src.index('// ── Campaign Builder JS')
    end = src.index('// ── Preview & Publish', start)
    esc = src[src.index('    function aclEscHtml'):src.index('\n    }', src.index('    function aclEscHtml')) + 6]
    script = Environment().from_string(src[start:end]).render(
        ref_options_json={'targetings': [], 'os': ['All']}, account_alias_map_json={},
        account_preflight_json={})
    ctx = browser.new_context()
    ctx.route('**/*', route)
    pg = ctx.new_page()
    errors = []
    pg.on('pageerror', lambda e: errors.append(str(e)))
    pg.goto('http://acl.test/')
    pg.set_content('<form id="form" data-country="Mexico"><input name="country" value="Mexico">'
                   '<input name="account_label" value="Demo"><input name="tracking_link" value="">'
                   '<input name="camp_count" class="cb-count" value="0"><div class="cb-slots"></div></form>')
    pg.add_script_tag(content=esc + '\n' + script)
    pg.evaluate("cbAddSlot(document.querySelector('form'))")
    pg.wait_for_function('window.CB_CREATIVES_REFRESH_COUNT >= 1')
    pg.wait_for_function('document.querySelector(".cb-slot") !== null')
    pg.wait_for_timeout(300)
    yield pg, tmp_path, hits
    assert not errors, errors
    ctx.close()
    pool.shutdown()


def _options(pg):
    return pg.locator('.cb-sel-dropdown:not([hidden]) .cb-sel-opt').all_inner_texts()


def test_new_file_is_found_on_focus_without_reload_or_type_switch(env):
    pg, folder, hits = env
    box = pg.locator('.cb-creative-search').first
    n0 = len(hits)
    # Страница открыта давно: прошлый запрос за порогом троттлинга.
    pg.evaluate("cbCreativesAge(60000)")
    (folder / '2222_new.jpg').write_bytes(b'x')
    box.fill('2222_n')  # промах -> ровно один фоновый запрос -> повторный поиск
    pg.wait_for_function('document.querySelector(".cb-sel-dropdown:not([hidden]) .cb-sel-opt")'
                         ' && document.querySelector(".cb-sel-dropdown:not([hidden]) .cb-sel-opt").textContent.includes("2222_new.jpg")', timeout=5000)
    assert len(hits) == n0 + 1
    assert any('2222_new.jpg' in t for t in _options(pg))


def test_miss_triggers_exactly_one_refresh(env):
    pg, folder, hits = env
    box = pg.locator('.cb-creative-search').first
    n = len(hits)
    pg.evaluate("cbCreativesAge(60000)")
    box.fill('nope')
    pg.wait_for_timeout(500)
    assert len(hits) == n + 1  # промах = ровно один запрос
    n = len(hits)
    # Повторные символы сразу после промаха не плодят запросов.
    box.press_sequentially('xyz', delay=20)
    pg.wait_for_timeout(500)
    assert len(hits) == n  # троттлинг промаха
    assert 'No files found' in ''.join(_options(pg))


def test_deleted_file_disappears(env):
    pg, folder, hits = env
    box = pg.locator('.cb-creative-search').first
    box.fill('1111')
    pg.wait_for_function('document.querySelector(".cb-sel-dropdown:not([hidden]) .cb-sel-opt")', timeout=5000)
    assert any('1111_old.jpg' in t for t in _options(pg))
    (folder / '1111_old.jpg').unlink()
    # Фокус после паузы больше троттлинга: сдвигаем «часы» последнего запроса.
    pg.evaluate("cbCreativesAge(1e12)")
    pg.locator('body').click()
    box.focus()
    pg.wait_for_function('!document.querySelector(".cb-sel-dropdown:not([hidden])")'
                         ' || !document.querySelector(".cb-sel-dropdown:not([hidden])").textContent.includes("1111_old.jpg")',
                         timeout=5000)


def test_focus_refresh_is_throttled(env):
    pg, folder, hits = env
    box = pg.locator('.cb-creative-search').first
    pg.evaluate("cbCreativesAge(1e12)")
    n = len(hits)
    for _ in range(4):
        pg.locator('body').click()
        box.focus()
        pg.wait_for_timeout(80)
    pg.wait_for_timeout(300)
    assert len(hits) == n + 1  # четыре фокуса в пределах 10 с = один запрос
    pg.evaluate("cbCreativesAge(10500)")
    pg.locator('body').click()
    box.focus()
    pg.wait_for_timeout(300)
    assert len(hits) == n + 2
