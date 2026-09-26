# -*- coding: utf-8 -*-
"""Свежая вкладка вместо зависшей — на настоящем Chromium (ACL-1320).

`browser.fresh_tab` не должен говорить с рендерером зависшей вкладки:
id вкладки, новая вкладка и закрытие старой — через CDP-сессию браузера.
Здесь рендерер вешается по-настоящему (бесконечный цикл в странице), и
проверяется, что проба живости это видит за секунды, а свежая вкладка
открывается в том же контексте, отвечает, а зависшая закрыта.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import browser as br  # noqa: E402


@pytest.fixture(scope="module")
def chromium():
    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=os.environ.get("ACL_BROWSER_EXECUTABLE") or None)
        try:
            yield b
        finally:
            b.close()


@pytest.fixture
def ctx(chromium):
    c = chromium.new_context(viewport={"width": 1280, "height": 800})
    try:
        yield c
    finally:
        c.close()


def test_a_hung_renderer_is_seen_within_the_probe(ctx):
    page = ctx.new_page()
    page.set_content("<p>list</p>")
    assert br.page_answers(page, 2.0) is True
    page.evaluate("() => { setTimeout(() => { while (true) {} }, 0); }")
    t0 = time.time()
    assert br.page_answers(page, 2.0) is False
    assert time.time() - t0 < 10


def test_a_hung_tab_is_replaced_and_closed(ctx):
    page = ctx.new_page()
    page.set_content("<p>list</p>")
    page.evaluate("() => { setTimeout(() => { while (true) {} }, 0); }")
    assert br.page_answers(page, 2.0) is False
    t0 = time.time()
    new = br.fresh_tab(page, "")
    assert new is not None, "свежая вкладка не открылась"
    assert time.time() - t0 < 30
    assert new in ctx.pages, "вкладка открылась не в том контексте"
    assert new.evaluate("() => 1 + 1") == 2
    deadline = time.time() + 10
    while page in ctx.pages and time.time() < deadline:
        new.wait_for_timeout(200)
    assert page not in ctx.pages, "зависшая вкладка осталась открытой"
