# -*- coding: utf-8 -*-
"""«Add text option» на живой разметке Doc19 29.09 (ACL-1344, повторный отказ).

Живой прогон 29.09 (Doc19, классический редактор) дал
`ad_copy_second_text add_control_not_found {"boxes": 1, "zones": 7}`, хотя
кнопка на месте: `div[role=button]` с `data-surface`
`…/message_field/lib:dco-add-asset-button` и текстом «Add text option».
Зона раздела «Advantage+ creative text generation» поднималась от заголовка
до самого большого предка без поля основного текста — а этот предок накрывал
и кнопку, так что кнопка отфильтровывалась как «внутри генерации».

Фикстура `fixtures/ads_manager/ad_copy_add_text_option_2026_09_29.html` —
блок `ad_creative_section` с той страницы, вычищенный (текст объявления —
заглушки, id и ссылки убраны; см. README фикстур).

Проверяется в офлайн-Chromium:

* кнопка находится по разметочному имени, не попадает в зону генерации,
  а варианты Meta в зону по-прежнему попадают и нашими полями не считаются;
* второй текст добавляется нажатием именно этой кнопки; ничего внутри раздела
  генерации (его кнопки, варианты, «Generate new variations») не нажимается,
  даже если там стоит своя «Add text option».
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
from src.fb_ads_rpa import page_capture  # noqa: E402
from src.fb_ads_rpa import run_telemetry  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "ads_manager" / "ad_copy_add_text_option_2026_09_29.html"
OUR_ADD = '[data-surface$="/message_field/lib:dco-add-asset-button"]'
TEXT2 = "Second primary text option.\nWith a second line."

# Реакция Meta (разметки после нажатия мы ещё не видели): новое пустое поле
# рядом с первым в том же `message_field`. Любое нажатие внутри раздела
# генерации (`role=list` с заголовком «Advantage+ creative text generation»)
# считается ловушкой.
_META_REACTION_JS = r"""
() => {
  const btn = document.querySelector('[data-surface$="/message_field/lib:dco-add-asset-button"]');
  const body = document.querySelector('[data-rpa-field="body"]');
  window.__adds = 0; window.__trap = 0; window.__clicked = [];
  btn.addEventListener('click', () => {
    window.__adds++;
    const t = document.createElement('textarea');
    t.setAttribute('dir', 'auto'); t.id = 'option-2';
    body.parentElement.appendChild(t);
  });
  const gen = [...document.querySelectorAll('[role=list]')]
    .find(l => /creative text generation/i.test(l.textContent));
  window.__gen = !!gen;
  document.addEventListener('click', (e) => {
    const el = e.target;
    window.__clicked.push(el.closest('[data-surface]') ? el.closest('[data-surface]').getAttribute('data-surface') : el.tagName);
    if (gen && gen.contains(el)) window.__trap++;
  }, true);
}
"""

# Своя «Add text option» внутри раздела генерации — не наша кнопка.
_TRAP_IN_GENERATION_JS = r"""
() => {
  const gen = [...document.querySelectorAll('[role=list]')]
    .find(l => /creative text generation/i.test(l.textContent));
  const b = document.createElement('div');
  b.setAttribute('role', 'button'); b.tabIndex = 0; b.id = 'gen-add';
  b.style.cssText = 'display:inline-block;padding:6px';
  b.textContent = 'Add text option';
  gen.querySelector('[role=region]').appendChild(b);
}
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
def captures(monkeypatch):
    seen: list[dict] = []
    monkeypatch.setattr(page_capture, "capture", lambda page, **kw: seen.append(kw) or "captured")
    return seen


@pytest.fixture
def page(browser, monkeypatch, tmp_path, captures):
    monkeypatch.setattr(run_telemetry, "TELEMETRY_PATH", tmp_path / "telemetry.jsonl")
    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    monkeypatch.setattr(md, "_focus_via_cdp_click", lambda p, loc: (loc.focus(), loc.element_handle())[1])
    monkeypatch.setattr(md, "_cdp_mouse_click", lambda pg, x, y: pg.evaluate(
        "([x, y]) => { const e = document.elementFromPoint(x, y); if (e) e.click(); }", [x, y]))
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    p = ctx.new_page()
    p.route("**/*", lambda route: route.abort())
    p.set_content("<!doctype html><html><body style='width:640px'>"
                  + FIXTURE.read_text(encoding="utf-8") + "</body></html>")
    try:
        yield p
    finally:
        ctx.close()


def _typed(handle, text: str) -> bool:
    handle.fill(text)
    return True


def _body_text(page) -> str:
    return page.input_value('[data-rpa-field="body"]')


def test_the_live_add_text_option_is_found_by_its_surface_not_hidden_in_the_generation_zone(page):
    state = md._text_options(page, "add_point")
    assert state["add_found"] is True, state
    assert state["add_how"] == "surface", state
    hit = page.evaluate(
        "([x, y, sel]) => !!document.elementFromPoint(x, y).closest(sel)",
        [state["point"]["x"], state["point"]["y"], OUR_ADD])
    assert hit, "точка нажатия — не кнопка нашего поля"
    # Варианты Meta в разделе генерации по-прежнему в зоне: поле одно — наше.
    assert [b["body"] for b in state["boxes"]] == [True], state["boxes"]


def test_second_text_goes_through_our_button_and_nothing_in_generation_is_pressed(page, captures):
    page.evaluate(_META_REACTION_JS)
    page.evaluate(_TRAP_IN_GENERATION_JS)
    assert page.evaluate("window.__gen") is True
    report: dict = {}
    ok = md.add_second_primary_text(page, _body_text(page), TEXT2, _typed, report)
    assert ok is True, report
    assert report["second_text"]["add_how"] == "surface"
    assert page.evaluate("window.__adds") == 1
    assert page.evaluate("window.__trap") == 0, page.evaluate("window.__clicked")
    assert md._copy_matches(page.input_value("#option-2"), TEXT2)
    # Варианты Meta не тронуты.
    gen = page.evaluate("() => [...document.querySelectorAll('[role=region] textarea')].map(t => t.value)")
    assert gen == [f"Generated variation placeholder {i}." for i in range(1, 6)], gen
    assert captures == []


def test_a_generation_add_is_never_pressed_when_our_button_is_gone(page, captures):
    """Нет кнопки нашего поля — «Add text option» раздела генерации всё
    равно не наша: мягкий отказ, ничего не нажато, блок креатива снят."""
    page.evaluate(_META_REACTION_JS)
    page.evaluate(_TRAP_IN_GENERATION_JS)
    page.evaluate(f"() => document.querySelector('{OUR_ADD}').closest('#add-bodies-option-button').remove()")
    report: dict = {}
    assert md.add_second_primary_text(page, _body_text(page), TEXT2, _typed, report) is False
    assert report["second_text"]["why"] == "add_control_not_found"
    assert page.evaluate("window.__trap") == 0
    assert len(captures) == 1
    assert captures[0]["step"] == "ad_second_text_add_not_found"
    assert set(captures[0]["blocks"]) == {"ad_creative_section"}
    assert report["second_text_capture"] == "captured"


def test_the_creative_section_is_captured_once_per_ad(page, captures):
    page.evaluate(f"() => document.querySelector('{OUR_ADD}').remove()")
    report: dict = {}
    for _ in range(3):
        assert md.add_second_primary_text(page, _body_text(page), TEXT2, _typed, report) is False
        assert report["second_text"]["why"] == "add_control_not_found"
    assert len(captures) == 1
    # Блок снимка находит ровно этот раздел.
    surface = page.evaluate(
        "(expr) => { const el = (new Function('return (' + expr + ')'))();"
        " return el ? el.getAttribute('data-surface') : null; }",
        md.AD_CREATIVE_SECTION_BLOCK_JS)
    assert surface == "/am/editor_drawer/editor:ad/ad_creative_section"
    # Следующее объявление — свой `report`, свой снимок.
    assert md.add_second_primary_text(page, _body_text(page), TEXT2, _typed, {}) is False
    assert len(captures) == 2


def test_our_surface_button_inside_a_generation_surface_is_not_pressed(page, captures):
    """Разметочное имя не отменяет зону: кнопка, которую Meta вложила в
    раздел генерации (`data-surface` с `text_generation`), не нажимается."""
    page.evaluate(_META_REACTION_JS)
    page.evaluate("""() => {
        const slot = document.getElementById('add-bodies-option-button');
        const wrap = document.createElement('div');
        wrap.setAttribute('data-surface', '/am/editor_drawer/editor:ad/text_generation_section');
        slot.replaceWith(wrap); wrap.appendChild(slot);
    }""")
    report: dict = {}
    assert md.add_second_primary_text(page, _body_text(page), TEXT2, _typed, report) is False
    assert report["second_text"]["why"] == "add_control_not_found"
    assert page.evaluate("window.__adds") == 0
