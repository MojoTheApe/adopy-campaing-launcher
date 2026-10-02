# -*- coding: utf-8 -*-
"""«Add text option» на настоящих страницах Meta (ACL-1344, стенд ACL-1433).

Две живые разметки блока `ad_creative_section` после набранного текста 1:

* классический редактор — `ad_copy_headline_missing_2026_10_01.html`
  (cc8fcfa17ae9/08_ad_copy, 01.10; первый снимок кнопки на стенде);
* одноэкранный редактор (`simple_create`) — `ad_copy_single_screen_2026_09_30.html`
  (3100d06ad51e/04_destination_sweep, 30.09).

В обоих кнопка одна и та же: `#add-bodies-option-button` > `div[role=button]`
с `data-surface` `…/message_field/lib:dco-add-asset-button` и текстом «Add text
option»; основной текст — Draft.js `contenteditable role=combobox`.

Почему живые прогоны 28.09–01.10 давали `add_control_not_found`: в тех
объявлениях Meta рисует раздел «Advantage+ creative text generation» внутри
того же `message_field`, рядом с кнопкой (живой Doc19 29.09,
`ad_copy_add_text_option_2026_09_29.html`). Старый поиск находил кнопку
только по тексту и отбрасывал всё внутри «зоны генерации», а зона от
заголовка раздела поднималась до общего с кнопкой предка. Телеметрия 01.10:
каждый отказ — `zones 3–7` и тут же `text_generation_off found: true`;
каждый успех — раздела нет. Поэтому каждая страница проверяется и как
снята, и с настоящим разделом генерации 29.09, вставленным туда же, где он
стоял на живой странице (соседом ветки кнопки).

Что Meta рисует после нажатия, не снято ни разу: тест кладёт рядом пустую
копию первого редактора (так выглядело поле в успешных прогонах 01.10:
`boxes 2`). Робот: находит кнопку, жмёт, набирает текст 2, сверяет оба поля.
"""
from __future__ import annotations

import pytest

from src.fb_ads_rpa import meta_dom as md
from src.fb_ads_rpa import page_capture, run_telemetry
from tests.bench_support import FIXTURES, browser, fragment  # noqa: F401

CLASSIC = FIXTURES / "ad_copy_headline_missing_2026_10_01.html"
SINGLE_SCREEN = FIXTURES / "ad_copy_single_screen_2026_09_30.html"
GENERATION = FIXTURES / "ad_copy_add_text_option_2026_09_29.html"
OUR_ADD = '[data-surface$="/message_field/lib:dco-add-asset-button"]'
TEXT2 = "Second primary text option.\nWith a second line."
PAGES = {
    "classic": (CLASSIC, "/am/editor_drawer/editor:ad/ad_creative_section"),
    "single_screen": (SINGLE_SCREEN, "/am/editor:ad/ad_creative_section"),
}

# Раздел генерации с живой страницы 29.09 — туда же, где он был: ветка
# раздела — сосед ветки кнопки под их общим предком внутри `message_field`.
_INSERT_GENERATION_JS = r"""
(html) => {
  const tpl = document.createElement('template');
  tpl.innerHTML = html;
  const src = tpl.content;
  const gen = [...src.querySelectorAll('[role=list]')]
    .find(l => /creative text generation/i.test(l.textContent));
  const slot = src.querySelector('#add-bodies-option-button');
  let up = 0, btnBranch = slot;
  while (!btnBranch.parentElement.contains(gen)) { btnBranch = btnBranch.parentElement; up++; }
  let genBranch = gen;
  while (genBranch.parentElement !== btnBranch.parentElement) genBranch = genBranch.parentElement;
  let here = document.querySelector('#add-bodies-option-button');
  for (let i = 0; i < up; i++) here = here.parentElement;
  here.after(document.importNode(genBranch, true));
  return up;
}
"""

# Реакция Meta на нажатие: пустая копия первого редактора под ним. Нажатие
# внутри раздела генерации — ловушка.
_META_REACTION_JS = r"""
() => {
  const btn = document.querySelector('[data-surface$="/message_field/lib:dco-add-asset-button"]');
  const body = document.querySelector('[data-rpa-field="body"]');
  window.__adds = 0; window.__trap = 0;
  btn.addEventListener('click', () => {
    window.__adds++;
    const row = body.closest('[role=presentation]');
    const copy = row.cloneNode(true);
    const ed = copy.querySelector('[contenteditable=true]');
    for (const a of [...ed.attributes]) if (a.name.startsWith('data-rpa')) ed.removeAttribute(a.name);
    ed.innerHTML = '<div><div><span><br></span></div></div>';
    ed.id = 'option-2';
    row.after(copy);
  });
  const gen = [...document.querySelectorAll('[role=list]')]
    .find(l => /creative text generation/i.test(l.textContent));
  document.addEventListener('click', (e) => { if (gen && gen.contains(e.target)) window.__trap++; }, true);
}
"""

# Meta перерисовывает новое поле сразу после того, как робот его пометил:
# метка остаётся на снятом узле (гипотеза живых `option_box_not_focused`).
_REMOUNT_ONCE_JS = r"""
() => {
  window.__remounts = 0;
  new MutationObserver((list, obs) => {
    for (const m of list) {
      const el = m.target;
      if (el.getAttribute('data-rpa-field') !== 'body2' || window.__remounts) continue;
      obs.disconnect();
      window.__remounts++;
      const fresh = el.cloneNode(true);
      for (const a of [...fresh.attributes]) if (a.name.startsWith('data-rpa')) fresh.removeAttribute(a.name);
      el.replaceWith(fresh);
    }
  }).observe(document.body, {attributes: true, subtree: true, attributeFilter: ['data-rpa-field']});
}
"""


@pytest.fixture
def captures(monkeypatch):
    seen: list[dict] = []
    monkeypatch.setattr(page_capture, "capture", lambda page, **kw: seen.append(kw) or "captured")
    return seen


@pytest.fixture
def page(browser, monkeypatch, tmp_path, captures):  # noqa: F811
    monkeypatch.setattr(run_telemetry, "TELEMETRY_PATH", tmp_path / "telemetry.jsonl")
    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    monkeypatch.setattr(md, "_focus_via_cdp_click", lambda p, loc: (loc.focus(), loc.element_handle())[1])
    monkeypatch.setattr(md, "_cdp_mouse_click", lambda pg, x, y: pg.evaluate(
        "([x, y]) => { const e = document.elementFromPoint(x, y); if (e) e.click(); }", [x, y]))
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    ctx.route("**/*", lambda route: route.abort())
    p = ctx.new_page()
    try:
        yield p
    finally:
        ctx.close()


def _load(page, which: str, generation: bool) -> str:
    path, surface = PAGES[which]
    page.set_content("<!doctype html><html><body style='width:640px'>"
                     + fragment(path, "creative") + "</body></html>")
    assert page.locator(f'[data-surface="{surface}"]').count() == 1
    # Текст 1 помечает настоящий шаг робота.
    assert md.mark_ad_copy_field(page, "message_field", md._AD_COPY_FIELDS["message_field"], "body")
    if generation:
        assert page.evaluate(_INSERT_GENERATION_JS, GENERATION.read_text(encoding="utf-8")) > 0
        assert page.evaluate("() => /creative text generation/i.test("
                             "document.querySelector('[data-surface$=\"/message_field\"]').textContent)")
    return md._text_options(page, "scan")["boxes"][0]["text"]


def _typed(handle, text: str) -> bool:
    handle.evaluate("(e, t) => { e.innerText = t; }", text)
    return True


def _generated(page) -> list[str]:
    return page.evaluate("() => [...document.querySelectorAll('[role=region] textarea')].map(t => t.value)")


CASES = [(w, g) for w in PAGES for g in (False, True)]
IDS = [f"{w}-{'with_generation' if g else 'as_captured'}" for w, g in CASES]


@pytest.mark.parametrize("which,generation", CASES, ids=IDS)
def test_the_real_add_text_option_is_found_by_its_surface(page, which, generation):
    _load(page, which, generation)
    got = md._text_options(page, "add_point")
    assert got["add_found"] is True and got["add_how"] == "surface" and got["add_count"] == 1, got
    hit = page.evaluate(
        "([x, y, sel]) => { const e = document.elementFromPoint(x, y); return !!(e && e.closest(sel)"
        " && e.closest('#add-bodies-option-button')); }",
        [got["point"]["x"], got["point"]["y"], OUR_ADD])
    assert hit, "точка нажатия — не кнопка «Add text option» нашего поля"
    # Варианты Meta в наши поля не попадают: поле одно — наше.
    assert [b["body"] for b in got["boxes"]] == [True], got["boxes"]


@pytest.mark.parametrize("which,generation", CASES, ids=IDS)
def test_second_text_is_added_typed_and_read_back(page, captures, which, generation):
    body = _load(page, which, generation)
    page.evaluate(_META_REACTION_JS)
    report: dict = {}
    ok = md.add_second_primary_text(page, body, TEXT2, _typed, report)
    assert ok is True, report
    st = report["second_text"]
    assert st["add_how"] == "surface" and st["boxes"] == 2, st
    assert page.evaluate("window.__adds") == 1
    assert page.evaluate("window.__trap") == 0
    # Сверка по экрану: поле 1 — текст 1, новое поле — текст 2.
    after = md._text_options(page, "scan")["boxes"]
    assert [b["body"] for b in after] == [True, False], after
    assert md._copy_matches(after[0]["text"], body)
    assert md._copy_matches(after[1]["text"], TEXT2)
    assert md._copy_matches(page.inner_text("#option-2"), TEXT2)
    # «Optimize text per person» только записан, не тронут.
    assert st["optimize_text_per_person"] == ("Disabled" if which == "single_screen" else None)
    if generation:
        assert _generated(page) == [f"Generated variation placeholder {i}." for i in range(1, 6)]
    assert captures == []


@pytest.mark.parametrize("which", list(PAGES))
def test_a_redrawn_option_box_is_taken_again(page, which):
    """Meta перерисовала новое поле после пометки: робот берёт его заново,
    а не сдаётся с `option_box_not_focused`."""
    body = _load(page, which, True)
    page.evaluate(_META_REACTION_JS)
    page.evaluate(_REMOUNT_ONCE_JS)
    report: dict = {}
    ok = md.add_second_primary_text(page, body, TEXT2, _typed, report)
    assert page.evaluate("window.__remounts") == 1
    assert ok is True, report
    assert report["second_text"]["reclaims"] == 1
    assert page.evaluate("window.__adds") == 1
    assert md._copy_matches(page.inner_text("#option-2"), TEXT2)


@pytest.mark.parametrize("which", list(PAGES))
def test_without_our_button_nothing_is_pressed_and_the_block_is_captured(page, captures, which):
    """Шаг остаётся предупреждением: кнопки нет — мягкий отказ, раздел
    генерации не тронут, блок креатива снят один раз."""
    body = _load(page, which, True)
    page.evaluate(_META_REACTION_JS)
    page.evaluate("() => document.getElementById('add-bodies-option-button').remove()")
    report: dict = {}
    assert md.add_second_primary_text(page, body, TEXT2, _typed, report) is False
    assert report["second_text"]["why"] == "add_control_not_found"
    assert page.evaluate("window.__trap") == 0
    assert [c["step"] for c in captures] == ["ad_second_text_add_not_found"]
    fail = md.second_text_soft_fail("ad", "ad 1", TEXT2, report)
    assert fail == f"ad_second_text:ad 1 {md.SECOND_TEXT_NOT_ADDED} (add_control_not_found)"
