# -*- coding: utf-8 -*-
"""Текст объявления сверяется с тем, что вводили, а не «непусто» (16.09, 18:53).

Три браузера разом: в заголовок легло «★★★★☆.6k» — часть нажатий пропала, а
проверка «в поле что-то есть» приняла это за успех; основной текст остался
пустым. Теперь поле читается обратно и сравнивается с исходной строкой; при
расхождении — стереть и вставить целиком.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import meta_dom as md  # noqa: E402

_PAGE = """
<!doctype html><html><body>
<label>Primary text</label>
<div role="combobox" contenteditable="true" aria-label="Tell people what your ad is about" id="body"></div>
<label>Headline</label>
<input id="title" placeholder="Headline">
</body></html>
"""


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
    monkeypatch.setattr(md, "_sleep_h", lambda s: None)
    monkeypatch.setattr(md, "_reveal_ad_copy_fields", lambda p: True)

    def mark(p, key, label, m):
        sel = "#body" if key == "message_field" else "#title"
        p.evaluate("([s, m]) => document.querySelector(s).setAttribute('data-rpa-field', m)", [sel, m])
        return True

    monkeypatch.setattr(md, "mark_ad_copy_field", mark)
    monkeypatch.setattr(md, "_focus_via_cdp_click", lambda p, loc: (loc.focus(), loc.element_handle())[1])
    ctx = browser.new_context(viewport={"width": 1280, "height": 800})
    p = ctx.new_page()
    p.set_content(_PAGE)
    try:
        yield p
    finally:
        ctx.close()


def _lossy(page, text):
    # Как вживую: половина нажатий не дошла.
    page.keyboard.insert_text(text[::2])


def test_lost_keystrokes_are_replaced_by_a_whole_insert(page, monkeypatch):
    monkeypatch.setattr(md, "_type_human", _lossy)
    report: dict = {}
    assert md.fill_ad_copy(page, "★★★★★ 4.6k", "Regístrese hoy, cupos limitados", report) is True
    assert page.input_value("#title") == "★★★★★ 4.6k"
    assert page.inner_text("#body").strip() == "Regístrese hoy, cupos limitados"
    assert any(a.get("stage") == "typed_text_mismatch" for a in report["attempts"])


def test_clean_typing_needs_no_second_pass(page, monkeypatch):
    monkeypatch.setattr(md, "_type_human", lambda p, t: p.keyboard.insert_text(t))
    report: dict = {}
    assert md.fill_ad_copy(page, "Title", "Body text", report) is True
    assert not report["attempts"]


def test_a_field_that_never_shows_the_text_fails(page, monkeypatch):
    monkeypatch.setattr(md, "_type_human", lambda p, t: None)
    page.evaluate("document.getElementById('title').addEventListener('input', e => e.target.value = 'x')")
    assert md.fill_ad_copy(page, "Title", "", {}) is False


def test_whitespace_and_line_breaks_do_not_count():
    assert md._copy_matches("Line one\nLine two​", "Line one\n\nLine two")
    assert not md._copy_matches("★★★★☆.6k", "★★★★★ 4.6k")


def test_meta_text_variations_are_switched_off_before_typing(browser, monkeypatch):
    """21.09, решение пользователя: Advantage+ creative подставляет к нашему
    тексту свои варианты, и в показ идёт что попало. Снимаем «Check all text
    variations» до ввода."""
    clicks = []
    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    monkeypatch.setattr(md._telemetry, "record", lambda *a, **k: None)
    monkeypatch.setattr(md, "_cdp_mouse_click", lambda pg, x, y: (
        clicks.append((x, y)),
        pg.evaluate("([x,y]) => { const e = document.elementFromPoint(x,y); if (e) e.click(); }", [x, y]),
    ))
    ctx = browser.new_context()
    page = ctx.new_page()
    try:
        page.set_content(
            "<div><div role='checkbox' aria-checked='true' id='all'"
            " style='width:18px;height:18px'></div>"
            "<span>Check all text variations</span></div>"
            "<script>document.getElementById('all').addEventListener('click',"
            " () => document.getElementById('all').setAttribute('aria-checked','false'));</script>"
        )
        assert md.disable_text_variations(page) is True, "галочка должна сняться"
        assert clicks, "кликов не было"
        assert page.get_attribute("#all", "aria-checked") == "false"

        page.set_content("<div>Ad without variations</div>")
        assert md.disable_text_variations(page) is None
    finally:
        ctx.close()


def test_a_second_attempt_replaces_the_text_instead_of_appending(page, monkeypatch):
    """21.09, скриншот пользователя: в поле лежал наш текст, повторённый
    подряд много раз, со склейкой посреди слова — каждая попытка дописывала
    его к прежнему вместо замены."""
    monkeypatch.setattr(md, "_type_human", lambda p, t: p.keyboard.insert_text(t))
    page.evaluate("() => { document.getElementById('body').textContent = 'СТАРЫЙ ТЕКСТ'; }")
    assert md.fill_ad_copy(page, "Заголовок", "Новый текст") is True
    body = page.inner_text("#body").strip()
    assert body == "Новый текст", body
    assert "СТАРЫЙ" not in body


def test_a_truncated_paste_is_retyped_whole_without_doubling(page, monkeypatch):
    """22.09 01:05 (скриншот пользователя, CR_ait IPO): дописывание хвоста
    задвоило текст — поле ещё дорисовывало вставку. Теперь поле стирается и
    набирается заново, а задвоения быть не должно."""
    full = "Primera linea del anuncio. Segunda linea. Empieza hoy."
    tries = {"n": 0}

    def lose_tail_once(p, t):
        tries["n"] += 1
        p.keyboard.insert_text(t[:-12] if (t == full and tries["n"] == 1) else t)

    monkeypatch.setattr(md, "_type_human", lose_tail_once)
    assert md.fill_ad_copy(page, "Заголовок", full) is True
    body = page.inner_text("#body").strip()
    assert body == full, body
    assert body.count("Empieza hoy.") == 1, "текст не должен задваиваться"


def test_meta_text_generation_is_expanded_and_switched_off(browser, monkeypatch):
    """22.09, решение пользователя: снятой галочки «Check all text variations»
    мало — сам раздел Advantage+ creative text generation остаётся включённым.
    Раскрываем свёрнутый раздел и выключаем его."""
    clicks = []
    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    monkeypatch.setattr(md._telemetry, "record", lambda *a, **k: None)
    monkeypatch.setattr(md, "_cdp_mouse_click", lambda pg, x, y: (
        clicks.append((x, y)),
        pg.evaluate("([x,y]) => { const e = document.elementFromPoint(x,y); if (e) e.click(); }", [x, y]),
    ))
    ctx = browser.new_context(viewport={"width": 1280, "height": 800})
    page = ctx.new_page()
    try:
        page.set_content(
            "<div id='sec'><span>Advantage+ creative text generation</span>"
            "<div role='button' aria-expanded='false' id='chev'"
            " style='width:20px;height:20px'></div>"
            "<div id='body' hidden><div role='switch' aria-checked='true' id='sw'"
            " style='width:24px;height:24px'></div></div></div>"
            "<script>"
            "document.getElementById('chev').addEventListener('click', () => {"
            " document.getElementById('chev').setAttribute('aria-expanded','true');"
            " document.getElementById('body').removeAttribute('hidden'); });"
            "document.getElementById('sw').addEventListener('click', () =>"
            " document.getElementById('sw').setAttribute('aria-checked','false'));"
            "</script>"
        )
        assert md.disable_text_generation(page) is True
        assert page.get_attribute("#sw", "aria-checked") == "false"
        assert page.get_attribute("#chev", "aria-expanded") == "true", "раздел должен раскрыться"

        page.set_content("<div>Ad without generation block</div>")
        assert md.disable_text_generation(page) is None
    finally:
        ctx.close()
