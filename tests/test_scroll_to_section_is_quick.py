# -*- coding: utf-8 -*-
"""`scroll_to_section` не ждёт заголовок, которого нет (ACL-1313, 25.09).

У Meta разделы «Conversion location», «Audience», «Conversion event» — не
`role=heading`, а текст. Прежний вариант ждал 5 с заголовок, потом искал
текст: 5 с на каждую прокрутку, 10–15 с на кампанию. Теперь один опрос по
всем способам сразу, порядок предпочтения прежний.

Офлайн-Chromium, `page.set_content`.
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

from src.fb_ads_rpa import meta_dom as md  # noqa: E402

_TALL = "<div style='height:3000px'></div>"


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=os.environ.get("ACL_BROWSER_EXECUTABLE") or None)
        try:
            yield b
        finally:
            b.close()


@pytest.fixture
def page(browser, monkeypatch):
    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    ctx = browser.new_context(viewport={"width": 1000, "height": 600})
    p = ctx.new_page()
    try:
        yield p
    finally:
        ctx.close()


def _in_view(page, sel):
    return page.evaluate(
        "(s) => { const r = document.querySelector(s).getBoundingClientRect();"
        " return r.top >= 0 && r.bottom <= innerHeight; }", sel)


def test_a_text_only_section_is_reached_without_waiting_for_a_heading(page):
    page.set_content(f"<body>{_TALL}<div id='t'>Conversion location</div>{_TALL}</body>")
    t0 = time.time()
    assert md.scroll_to_section(page, "Conversion location", "Ubicación de la conversión") is True
    assert time.time() - t0 < 2.5, "ждали заголовок, которого нет"
    assert _in_view(page, "#t")


def test_the_second_label_is_found_without_waiting_for_the_first(page):
    page.set_content(f"<body>{_TALL}<div id='t'>Ubicación de la conversión</div>{_TALL}</body>")
    t0 = time.time()
    assert md.scroll_to_section(page, "Conversion location", "Ubicación de la conversión") is True
    assert time.time() - t0 < 2.5
    assert _in_view(page, "#t")


def test_a_heading_is_still_preferred_over_the_same_text(page):
    page.set_content(
        f"<body>{_TALL}<span id='x'>Audience</span>{_TALL}"
        f"<h2 id='h'>Audience</h2>{_TALL}</body>")
    assert md.scroll_to_section(page, "Audience") is True
    assert _in_view(page, "#h")
    assert not _in_view(page, "#x")


def test_a_section_that_appears_late_is_waited_for(page):
    page.set_content(f"<body>{_TALL}<div id='slot'></div>{_TALL}</body>")
    page.evaluate("setTimeout(() => { document.getElementById('slot').innerHTML ="
                  " '<div id=t>Conversion event</div>'; }, 1500)")
    assert md.scroll_to_section(page, "Conversion event") is True
    assert _in_view(page, "#t")


def test_an_absent_section_gives_up_within_the_budget(page):
    page.set_content("<body><div>nothing here</div></body>")
    t0 = time.time()
    assert md.scroll_to_section(page, "Audience", budget_s=1.0) is False
    assert time.time() - t0 < 3
