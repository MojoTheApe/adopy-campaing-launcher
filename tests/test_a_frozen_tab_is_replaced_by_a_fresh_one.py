# -*- coding: utf-8 -*-
"""Свежая вкладка вместо зависшей — на настоящем Chromium (ACL-1320).

`browser.fresh_tab` не должен говорить с рендерером зависшей вкладки:
id вкладки, новая вкладка и закрытие старой — через CDP-сессию браузера.
Здесь рендерер вешается по-настоящему (бесконечный цикл в странице), и
проверяется, что проба живости это видит за секунды, а свежая вкладка
открывается в том же контексте, отвечает, а зависшая закрыта.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import browser as br  # noqa: E402
from tests._retry_once import launch_chromium  # noqa: E402

# Сессия на вкладке с зависшим рендерером (`context.new_cdp_session`) висела
# 197 с — замер ACL-1320 на настоящем Chromium. Это и есть медленный путь,
# от которого `fresh_tab` уходит. Граница — его половина, а не «30 с»: на
# нагруженном раннере честный `fresh_tab` шёл 31.2 с (CI 01.10, ACL-1491),
# а свои внутренние ожидания у него и так ограничены (10 с вкладка + 5 с
# проба), так что половины медленного пути хватает с запасом.
HUNG_TAB_SESSION_S = 197.0


# Вызовы `Page`, которые идут в рендерер вкладки. `close`, `url`,
# `context`, `is_closed` — нет: закрытие идёт через браузер, остальное —
# локальные поля Playwright.
_RENDERER_CALLS = (
    "evaluate", "evaluate_handle", "wait_for_function", "wait_for_selector",
    "wait_for_load_state", "wait_for_timeout", "title", "content", "reload",
    "goto", "set_content", "bring_to_front", "screenshot", "locator",
    "get_by_role", "get_by_text", "query_selector", "add_init_script",
)


class _AskedTheHungTab(Exception):
    """Вызов в зависшую вкладку: записан и не выполнен — иначе сломанный
    `fresh_tab` висел бы вместе с ней (сессия на ней — 197 с и дольше),
    а не падал."""


def _spy_on_renderer_calls(page) -> list[str]:
    asked: list[str] = []
    for name in _RENDERER_CALLS:
        def spy(*a, _name=name, **k):
            asked.append(_name)
            raise _AskedTheHungTab(_name)

        setattr(page, name, spy)
    return asked


@pytest.fixture(scope="module")
def chromium():
    with sync_playwright() as pw:
        b = launch_chromium(pw)
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
    # Главная проверка без часов (ACL-1491): `fresh_tab` не говорит с
    # рендерером зависшей вкладки — ни CDP-сессии на ней (висела 197 с), ни
    # вызовов, которые идут в её страницу. Время ниже — только страховка.
    sessions_on = []
    open_session = ctx.new_cdp_session

    def session_spy(target):
        sessions_on.append(target)
        if target is page:
            raise _AskedTheHungTab("new_cdp_session")
        return open_session(target)

    ctx.new_cdp_session = session_spy
    asked = _spy_on_renderer_calls(page)
    t0 = time.time()
    new = br.fresh_tab(page, "")
    assert new is not None, "свежая вкладка не открылась"
    assert not any(target is page for target in sessions_on), "CDP-сессию открыли на зависшей вкладке"
    assert asked == [], f"зависшую вкладку спрашивали: {asked}"
    took = time.time() - t0
    assert took < HUNG_TAB_SESSION_S / 2, f"свежая вкладка открывалась {took:.1f} с — говорили с зависшей?"
    assert new in ctx.pages, "вкладка открылась не в том контексте"
    assert new.evaluate("() => 1 + 1") == 2
    deadline = time.time() + 10
    while page in ctx.pages and time.time() < deadline:
        new.wait_for_timeout(200)
    assert page not in ctx.pages, "зависшая вкладка осталась открытой"
