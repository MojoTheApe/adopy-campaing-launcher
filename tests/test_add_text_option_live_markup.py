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
    gen = page.evaluate("() => [...document.querySelectorAll('textarea[aria-labelledby]')].map(t => t.value)")
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


# ── ревью cf086fa: кнопки раздела генерации с нашим разметочным именем ────
#
# Кнопки раздела генерации на живой странице тоже носят `/message_field/lib:*`
# (feedback-кнопки). Кнопка с именем нашей «Add text option» внутри раздела
# не должна ни сужать зону генерации, ни нажиматься.

def _gen_button(page, where: str, text: str = "Add text option") -> None:
    page.evaluate("""([where, text, surface]) => {
      const gen = [...document.querySelectorAll('[role=list]')]
        .find(l => /creative text generation/i.test(l.textContent));
      const b = document.createElement('div');
      b.setAttribute('role', 'button'); b.tabIndex = 0; b.id = 'gen-own';
      b.setAttribute('data-surface', surface);
      b.style.cssText = 'display:inline-block;padding:6px';
      b.textContent = text;
      const reg = gen.querySelector('[role=region]');
      if (where === 'end') reg.appendChild(b);
      else if (where === 'row') reg.querySelector('textarea').parentElement.appendChild(b);
      else gen.insertBefore(b, gen.firstChild);
    }""", [where, text, "/am/editor_drawer/editor:ad/ad_creative_section/message_field/lib:dco-add-asset-button"])


def _generated(page) -> list[str]:
    return page.evaluate("() => [...document.querySelectorAll('textarea[aria-labelledby]')].map(t => t.value)")


@pytest.mark.parametrize("where", ["end", "row", "top"])
def test_a_generation_button_with_our_surface_is_not_pressed_when_ours_is_gone(page, where):
    page.evaluate(_META_REACTION_JS)
    _gen_button(page, where)
    page.evaluate("() => document.getElementById('add-bodies-option-button').remove()")
    report: dict = {}
    assert md.add_second_primary_text(page, _body_text(page), TEXT2, _typed, report) is False
    assert report["second_text"]["why"] == "add_control_not_found"
    assert page.evaluate("window.__trap") == 0, page.evaluate("window.__clicked")


@pytest.mark.parametrize("where", ["end", "row", "top"])
def test_generation_between_the_text_and_our_button_does_not_win_the_pick(page, where):
    """Раздел генерации встал между полем текста и нашей кнопкой, в нём
    кнопка с нашим именем: «ближайшая под полем» — не она."""
    page.evaluate(_META_REACTION_JS)
    _gen_button(page, where, "Add")
    page.evaluate("""() => {
      const gen = [...document.querySelectorAll('[role=list]')]
        .find(l => /creative text generation/i.test(l.textContent));
      const shared = gen.parentElement.parentElement;
      shared.insertBefore(gen.parentElement, shared.firstChild);
    }""")
    report: dict = {}
    assert md.add_second_primary_text(page, _body_text(page), TEXT2, _typed, report) is True, report
    assert page.evaluate("window.__adds") == 1
    assert page.evaluate("window.__trap") == 0, page.evaluate("window.__clicked")


@pytest.mark.parametrize("where", ["end", "row", "top"])
def test_generation_right_above_our_button_is_never_pressed(page, where):
    """Раздел генерации вплотную над нашей кнопкой, в нём кнопка с нашим
    именем выше нашей: ничего внутри раздела не нажимается (наша кнопка
    теперь делит ветку с вариантами Meta — мягкий отказ допустим)."""
    page.evaluate(_META_REACTION_JS)
    _gen_button(page, where, "Add")
    page.evaluate("""() => {
      const gen = [...document.querySelectorAll('[role=list]')]
        .find(l => /creative text generation/i.test(l.textContent));
      const real = document.getElementById('add-bodies-option-button');
      real.parentElement.insertBefore(gen, real);
    }""")
    report: dict = {}
    md.add_second_primary_text(page, _body_text(page), TEXT2, _typed, report)
    assert page.evaluate("window.__trap") == 0, (report, page.evaluate("window.__clicked"))
    assert _generated(page) == [f"Generated variation placeholder {i}." for i in range(1, 6)]


@pytest.mark.parametrize("where", ["end", "row", "top"])
def test_a_generation_button_with_our_surface_does_not_let_meta_variations_in(page, where):
    _gen_button(page, where)
    state = md._text_options(page, "scan")
    assert [b["body"] for b in state["boxes"]] == [True], state["boxes"]
    page.evaluate(_META_REACTION_JS)
    report: dict = {}
    assert md.add_second_primary_text(page, _body_text(page), TEXT2, _typed, report) is True, report
    assert page.evaluate("window.__trap") == 0, page.evaluate("window.__clicked")
    assert _generated(page) == [f"Generated variation placeholder {i}." for i in range(1, 6)]


def test_a_headline_add_is_never_taken_for_ours(page):
    """Кнопка заголовка, даже подписанная «Add text option», — не наша."""
    page.evaluate("() => document.getElementById('add-bodies-option-button').remove()")
    page.evaluate("""() => {
      const h = document.querySelector('[data-surface$="/headline_field/lib:dco-add-asset-button"]');
      h.setAttribute('aria-label', 'Add text option');
      window.__head = 0; h.addEventListener('click', () => window.__head++);
    }""")
    report: dict = {}
    assert md.add_second_primary_text(page, _body_text(page), TEXT2, _typed, report) is False
    assert report["second_text"]["why"] == "add_control_not_found"
    assert page.evaluate("window.__head") == 0


# ── каждая проверка кнопки нужна сама по себе (мутационная проверка) ──────

_GEN = "[...document.querySelectorAll('[role=list]')].find(l => /creative text generation/i.test(l.textContent))"


def test_without_roles_a_surface_button_in_a_variation_row_is_not_ours(page):
    """Раздел генерации без ролей списка: спасает только проверка ветки —
    в ветке кнопки лежат варианты Meta."""
    page.evaluate(_META_REACTION_JS)
    _gen_button(page, "row")
    page.evaluate("() => document.getElementById('add-bodies-option-button').remove()")
    page.evaluate(f"() => {{ const g = {_GEN};"
                  " for (const e of [g, ...g.querySelectorAll('[role=listitem],[role=region]')])"
                  " e.removeAttribute('role'); }")
    report: dict = {}
    assert md.add_second_primary_text(page, _body_text(page), TEXT2, _typed, report) is False
    assert page.evaluate("window.__trap") == 0, page.evaluate("window.__clicked")
    assert _generated(page) == [f"Generated variation placeholder {i}." for i in range(1, 6)]


def test_a_surface_button_on_top_of_the_generation_list_does_not_shrink_its_zone(page):
    """Варианты Meta вынесены из списка раздела, кнопка с нашим именем — в
    самом списке: ветка у неё чистая, отсекает её только роль списка. Иначе
    зона сжимается и варианты Meta попадают в наши поля."""
    _gen_button(page, "top")
    page.evaluate(f"() => {{ const g = {_GEN}; const r = g.querySelector('[role=region]');"
                  " g.parentElement.insertBefore(r, g.nextSibling); }")
    state = md._text_options(page, "scan")
    assert [b["body"] for b in state["boxes"]] == [True], state["boxes"]


def test_a_stale_our_mark_on_a_meta_variation_does_not_open_the_section(page):
    """Метка «наше поле» на варианте Meta (перерисовка) не делает раздел
    генерации нашим: его поля не попадают в список."""
    page.evaluate(f"() => {{ const g = {_GEN};"
                  " g.querySelector('[role=region] textarea').setAttribute('data-rpa-opt', 'ours'); }")
    state = md._text_options(page, "scan")
    assert [b["body"] for b in state["boxes"]] == [True], state["boxes"]


def test_the_button_in_metas_slot_wins_over_another_surface_button(page):
    page.evaluate(_META_REACTION_JS)
    page.evaluate("""() => {
      const body = document.querySelector('[data-rpa-field="body"]');
      const d = document.createElement('div');
      d.setAttribute('role', 'button'); d.tabIndex = 0; d.id = 'decoy';
      d.setAttribute('data-surface', '/am/editor_drawer/editor:ad/ad_creative_section/message_field/lib:dco-add-asset-button');
      d.style.cssText = 'display:inline-block;padding:6px'; d.textContent = 'Add text option';
      body.parentElement.appendChild(d);
      window.__decoy = 0; d.addEventListener('click', () => window.__decoy++);
    }""")
    report: dict = {}
    assert md.add_second_primary_text(page, _body_text(page), TEXT2, _typed, report) is True, report
    assert page.evaluate("window.__adds") == 1
    assert page.evaluate("window.__decoy") == 0


def test_a_surface_button_outside_the_field_is_pressed_neither_by_surface_nor_by_text(page):
    page.evaluate("() => document.getElementById('add-bodies-option-button').remove()")
    page.evaluate("""() => {
      const host = document.querySelector('[data-surface$="/message_field"]');
      const d = document.createElement('div');
      d.setAttribute('role', 'button'); d.tabIndex = 0; d.id = 'outside';
      d.setAttribute('data-surface', '/am/editor_drawer/editor:ad/ad_creative_section/message_field/lib:dco-add-asset-button');
      d.style.cssText = 'display:inline-block;padding:6px'; d.textContent = 'Add text option';
      host.parentElement.appendChild(d);
      window.__outside = 0; d.addEventListener('click', () => window.__outside++);
    }""")
    report: dict = {}
    assert md.add_second_primary_text(page, _body_text(page), TEXT2, _typed, report) is False
    assert report["second_text"]["why"] == "add_control_not_found"
    assert page.evaluate("window.__outside") == 0


# ── ревью 8d96212: заголовки генерации считаются только в нашем поле ──────

@pytest.mark.parametrize("where", ["body_start", "body_end", "headline_field", "liquidity_field"])
def test_a_generation_phrase_elsewhere_does_not_make_our_button_foreign(page, where):
    """Фраза «Text variations» вне поля основного текста (живой Doc19 дал
    zones 7 против 5 в блоке — совпадения вне блока есть): наша кнопка
    остаётся нашей, ветка у неё — только внутри поля."""
    page.evaluate(_META_REACTION_JS)
    page.evaluate("""(w) => {
      const d = document.createElement('div'); d.textContent = 'Text variations';
      if (w === 'body_end') document.body.appendChild(d);
      else if (w === 'body_start') document.body.insertBefore(d, document.body.firstChild);
      else if (w === 'headline_field') document.querySelector('[data-surface$="/headline_field"]').parentElement.appendChild(d);
      else document.querySelector('[data-surface$="/text_liquidity_toggle_field"]').parentElement.appendChild(d);
    }""", where)
    report: dict = {}
    assert md.add_second_primary_text(page, _body_text(page), TEXT2, _typed, report) is True, report
    assert report["second_text"]["add_how"] == "surface"
    assert page.evaluate("window.__adds") == 1
    assert page.evaluate("window.__trap") == 0


@pytest.mark.parametrize("roles", [True, False])
def test_generation_in_a_portal_keeps_our_button_and_its_own_button_untouched(page, roles):
    """Раздел генерации вынесен порталом в конец страницы, в нём кнопка с
    нашим именем: она вне поля — не наша; наша кнопка в поле — нажимается."""
    _gen_button(page, "end")
    page.evaluate(_META_REACTION_JS)
    page.evaluate(f"""(roles) => {{ const g = {_GEN};
      if (!roles) for (const e of [g, ...g.querySelectorAll('[role]')])
        if (['list', 'listitem', 'region'].includes(e.getAttribute('role'))) e.removeAttribute('role');
      const p = document.createElement('div'); p.id = 'portal'; p.appendChild(g); document.body.appendChild(p); }}""", roles)
    report: dict = {}
    assert md.add_second_primary_text(page, _body_text(page), TEXT2, _typed, report) is True, report
    assert page.evaluate("window.__adds") == 1
    assert page.evaluate("window.__trap") == 0, page.evaluate("window.__clicked")


_SPLIT_HEADINGS_JS = r"""
() => {
  const RE = /creative text generation|text variations|generated text|text suggestions/i;
  for (const l of [...document.querySelectorAll('span,div,label')]) {
    if (l.children.length || !RE.test(l.textContent)) continue;
    const t = l.textContent, i = t.search(/text/i);
    l.textContent = '';
    l.append(t.slice(0, i));
    const b = document.createElement('b'); b.textContent = t.slice(i, i + 4); l.append(b);
    l.append(t.slice(i + 4));
  }
}
"""


@pytest.mark.parametrize("real", [True, False])
def test_a_generation_heading_split_by_tags_is_still_recognised(page, real):
    """Фраза заголовка разбита тегом («creative <b>text</b> generation»):
    раздел всё равно узнаётся, его кнопка с нашим именем не нажимается."""
    _gen_button(page, "top")
    page.evaluate(_META_REACTION_JS)          # ловушка — по разделу, пока роли на месте
    page.evaluate(f"() => {{ const g = {_GEN}; for (const e of [g, ...g.querySelectorAll('[role]')])"
                  " if (['list', 'listitem', 'region'].includes(e.getAttribute('role'))) e.removeAttribute('role'); }")
    page.evaluate(_SPLIT_HEADINGS_JS)
    assert page.evaluate("window.__gen") is True
    if not real:
        page.evaluate("() => document.getElementById('add-bodies-option-button').remove()")
    state = md._text_options(page, "scan")
    assert [b["body"] for b in state["boxes"]] == [True], state["boxes"]
    report: dict = {}
    ok = md.add_second_primary_text(page, _body_text(page), TEXT2, _typed, report)
    assert page.evaluate("window.__trap") == 0, page.evaluate("window.__clicked")
    assert ok is real, report
