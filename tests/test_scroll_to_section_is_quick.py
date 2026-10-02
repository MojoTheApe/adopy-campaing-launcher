# -*- coding: utf-8 -*-
"""`scroll_to_section` не ждёт заголовок, которого нет (ACL-1313, 25.09).

У Meta разделы «Conversion location», «Audience», «Conversion event» — не
`role=heading`, а текст. Прежний вариант ждал 5 с заголовок, потом искал
текст: 5 с на каждую прокрутку, 10–15 с на кампанию. Теперь один опрос по
всем способам сразу, порядок предпочтения прежний.

Офлайн-Chromium, `page.set_content`.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import meta_dom as md  # noqa: E402
from tests._retry_once import launch_chromium  # noqa: E402

_TALL = "<div style='height:3000px'></div>"

# Прежний вариант ждал отсутствующий заголовок 5 с (и столько же стоит
# `scroll_into_view_if_needed` на невидимом). ACL-1491: время пути «только
# текст» сравниваем не с абсолютными 2.5 с, а с тем же вызовом, когда
# заголовок есть, — разница должна быть много меньше этих 5 с. Нагрузка
# раннера замедляет оба замера одинаково, а лишнее ожидание — нет.
HEADING_WAIT_S = 5.0


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        b = launch_chromium(pw)
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


def _timed_scroll(page, body, *labels):
    page.set_content(f"<body>{body}</body>")
    t0 = time.perf_counter()
    assert md.scroll_to_section(page, *labels) is True
    return time.perf_counter() - t0


def _heading_reference(page, *labels):
    """Тот же вызов, когда первый же способ (заголовок) находит раздел сразу.

    Первый вызов на свежей вкладке — прогрев, его не считаем."""
    body = f"{_TALL}<h2>{labels[0]}</h2>{_TALL}"
    _timed_scroll(page, body, *labels)
    return _timed_scroll(page, body, *labels)


def test_a_text_only_section_is_reached_without_waiting_for_a_heading(page):
    labels = ("Conversion location", "Ubicación de la conversión")
    ref = _heading_reference(page, *labels)
    took = _timed_scroll(page, f"{_TALL}<div id='t'>Conversion location</div>{_TALL}", *labels)
    assert took - ref < HEADING_WAIT_S / 2, f"ждали заголовок, которого нет: {took:.2f} с против {ref:.2f} с"
    assert _in_view(page, "#t")


def test_the_second_label_is_found_without_waiting_for_the_first(page):
    labels = ("Conversion location", "Ubicación de la conversión")
    ref = _heading_reference(page, *labels)
    took = _timed_scroll(page, f"{_TALL}<div id='t'>Ubicación de la conversión</div>{_TALL}", *labels)
    assert took - ref < HEADING_WAIT_S / 2, f"ждали первую подпись: {took:.2f} с против {ref:.2f} с"
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
