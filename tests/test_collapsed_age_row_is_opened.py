# -*- coding: utf-8 -*-
"""Свёрнутое поле Age раскрывается своей строкой, а открытое не сворачивается
(прогон 5e29e66da497, CL_ML_All_Video_FX, 25.09).

Что было: поле Age стояло открытым (выпадашки «18» / «65+», снимок
00_advantage_plus_still_on). Первая граница нашлась, но Meta перерисовала
поле, и замер рамки упал по таймауту. Старый запасной путь «раскрыть строку»
кликнул по первой подписи «Age» — а это заголовок открытого поля, кнопка
`aria-expanded="true"`, — и свернул поле в сводку «18 - 65+» (снимок
01_audience_age), после чего 24 тика опроса съели четыре минуты и шаг
отказал через 318 с.

Разметка — карточка Audience этого прогона (fixtures/ads_manager/
audience_age_collapsed_2026_09_25.html): свёрнутая (все строки — кнопки
`aria-expanded=false`, карандаш — безымянная иконка) и открытое поле Age.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import meta_dom as md  # noqa: E402

FIX = (Path(__file__).parent / "fixtures" / "ads_manager" / "audience_age_collapsed_2026_09_25.html").read_text(encoding="utf-8")
FRAG = {m.group(1): m.group(2).strip() for m in re.finditer(
    r"<!-- fragment: (\w+) \| source: [^>]*-->\n(.*?)(?=\n<!-- fragment: |\Z)", FIX, re.S)}

# Как у Meta: клик по строке Age раскрывает поле, клик по заголовку открытого
# поля его сворачивает. Все клики пишутся в window.clicked.
_META = r"""
([openAge, collapsedAge]) => {
  window.clicked = [];
  const norm = s => (s || '').replace(/[\s​]+/g, ' ').trim();
  document.addEventListener('click', (e) => {
    const row = e.target.closest('[aria-expanded]');
    const leaf = row && [...row.querySelectorAll('span')].find(s => s.children.length === 0 && norm(s.textContent));
    const name = leaf ? norm(leaf.textContent) : norm(e.target.textContent).slice(0, 30);
    window.clicked.push(name + ':' + (row ? row.getAttribute('aria-expanded') : '-'));
    const wrap = e.target.closest('[data-surface$="age_editor_wrapper"]');
    if (!wrap || !row || row.getAttribute('role') !== 'button') return;
    if (row.getAttribute('aria-expanded') === 'false') wrap.outerHTML = openAge;
    else if (row.getAttribute('aria-expanded') === 'true') wrap.outerHTML = collapsedAge;
  }, true);
}
"""


def _collapsed_age() -> str:
    m = re.search(r'<span data-surface-wrapper="1" data-surface="[^"]*age_editor_wrapper".*?</span>(?=</div>)',
                  FRAG["collapsed"], re.S)
    assert m, "в свёрнутой карточке нет поля Age"
    return m.group(0)


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
    monkeypatch.setattr(md._telemetry, "record", lambda *a, **k: None)
    monkeypatch.setattr(md, "_cdp_mouse_click", lambda pg, x, y: pg.mouse.click(x, y))
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    p = ctx.new_page()
    p.route("**/*", lambda route: route.abort())
    try:
        yield p
    finally:
        ctx.close()


def _load(page, card: str) -> None:
    page.set_content(f"<div style='width:600px'>{FRAG[card]}</div>")
    page.evaluate(_META, [FRAG["open_age"], _collapsed_age()])


def _clicked(page) -> list[str]:
    return page.evaluate("window.clicked")


def test_the_collapsed_age_row_is_opened_by_its_own_row(page):
    _load(page, "collapsed")
    assert md._age_triggers_read(page) == []
    assert md._open_collapsed_age_row(page, wait_s=2.0) is True
    assert _clicked(page) == ["Age:false"]
    assert md._age_triggers_read(page) == ["18", "65+"]


def test_an_open_age_field_is_not_clicked(page):
    _load(page, "open_card")
    assert md._open_collapsed_age_row(page, wait_s=1.0) is True
    assert _clicked(page) == []


def test_a_failed_pass_never_collapses_the_open_field(page, monkeypatch):
    """Обход границ не удался (Meta перерисовала поле) — по заголовку
    открытого поля не кликаем, поле остаётся открытым."""
    _load(page, "open_card")
    passes = []
    monkeypatch.setattr(md, "_age_boundaries_pass", lambda *a: passes.append(a) or False)
    assert md._fill_audience_age_controls(page, 35, 65) is False
    assert len(passes) == 2, "обход границ повторяется один раз"
    assert not [c for c in _clicked(page) if c.endswith(":true")], _clicked(page)
    assert md._age_triggers_read(page) == ["18", "65+"]


def test_a_pass_after_opening_sets_nothing_twice(page, monkeypatch):
    """Минимум уже стоит на нужном значении — повтор обхода его не трогает
    (и не ищет «18» где-то рядом, ACL-1250)."""
    measured = []
    monkeypatch.setattr(md, "_measure_age_trigger", lambda root, value, tries=3: measured.append(value))
    _load(page, "open_card")
    page.evaluate("""() => { const v = [...document.querySelectorAll('div')]
        .find(e => e.children.length === 0 && e.textContent.trim() === '18'); v.textContent = '35'; }""")
    assert md._age_boundaries_pass(page, "35", "65+") is True
    assert _clicked(page) == [] and measured == []


def test_locations_and_languages_rows_open_by_their_own_rows(page):
    _load(page, "collapsed")
    assert md._click_row_expander(page, "Locations|Ubicaciones") is True
    assert md._click_languages_edit(page) is True
    assert _clicked(page) == ["Locations:false", "Languages:false"]


def test_a_trigger_redrawn_between_mark_and_measure_is_marked_again(page):
    page.set_content("<div style='width:600px'><span>Age</span><div><div id='v'>18</div><div>65+</div></div></div>")
    page.evaluate("""() => {
      window.marks = 0;
      new MutationObserver((ms) => {
        for (const m of ms) {
          if (m.attributeName !== 'data-acl-age-trigger' || !m.target.hasAttribute('data-acl-age-trigger')) continue;
          window.marks += 1;
          if (window.marks === 1) {   // Meta перерисовала поле: помеченный узел заменён
            const fresh = document.createElement('div'); fresh.textContent = '18';
            m.target.replaceWith(fresh);
          }
        }
      }).observe(document.body, {attributes: true, subtree: true});
    }""")
    box = md._measure_age_trigger(page, "18")
    assert box and box["width"] > 0
    assert page.evaluate("window.marks") == 2


def test_the_age_step_opens_a_collapsed_field_before_setting_it(page, monkeypatch):
    _load(page, "collapsed")
    seen = []
    monkeypatch.setattr(md, "_age_boundaries_pass",
                        lambda pg, lo, hi: seen.append(md._age_triggers_read(pg)) or seen[-1] == ["18", "65+"])
    assert md._fill_audience_age_controls(page, 35, 65) is True
    assert seen == [["18", "65+"]]


# ── повторное ревью PR #314 (A2/A3): открытое поле не сворачивается и с одной подписью ──

def _open_one_label_blank_values(page):
    """Открытое поле Age с ОДНОЙ подписью (заголовок строки, aria-expanded=true),
    значения на миг не читаются — Meta перерисовывает поле (5e29e66da497)."""
    one = re.sub(r"<label[^>]*><span><span>Age</span></span></label>", "", FRAG["open_age"])
    page.set_content(f"<div style='width:600px'>{one}</div>")
    page.evaluate(_META, [FRAG["open_age"], _collapsed_age()])
    page.evaluate(r"""() => document.querySelectorAll('[role=combobox] div').forEach(d => {
        if (d.children.length === 0 && /^(18|65\+)$/.test(d.textContent.trim())) d.textContent = ''; })""")


def test_an_open_field_with_unreadable_values_is_not_clicked(page, monkeypatch):
    tried = []
    for name in ("_click_row_expander", "_click_summary_row"):
        real = getattr(md, name)
        monkeypatch.setattr(md, name, lambda *a, _n=name, _r=real: tried.append(_n) or _r(*a))
    _open_one_label_blank_values(page)
    assert md._age_triggers_read(page) == []
    assert md._open_collapsed_age_row(page, wait_s=0.5) is False
    assert tried == [], "открытое поле пытались «раскрыть»"
    assert _clicked(page) == []


def test_the_row_expander_never_presses_an_open_rows_header(page):
    _open_one_label_blank_values(page)
    md._click_row_expander(page, "Age|Edad")
    assert not [c for c in _clicked(page) if c.endswith(":true")], _clicked(page)
