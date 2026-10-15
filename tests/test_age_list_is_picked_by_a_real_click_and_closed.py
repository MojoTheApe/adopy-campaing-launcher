# -*- coding: utf-8 -*-
"""Возраст выбирается настоящим кликом, и список после него закрыт
(прогон 25c557ae0ca7, SA_sa_All_Image_Investing_35, 25.09 17:54).

Что было: шаг возраста отчитался успехом за 22,7 с, но список минимального
возраста остался открытым поверх строки Locations (скриншот
rpa_screenshots/25c557ae0ca7/20260925_175438_recorded_fail_audience_location.png:
выпадашка показывает 30, открыт список 30…37). Шаг локаций не нашёл, по
чему кликнуть (`locations_open nothing_to_click`, `location_rows_not_read`),
а возраст в итоге стоял 30 вместо 35. Причина: пункт «35» выбирался
`el.click()` из скрипта — это не нажатие мыши; Meta показала значение, но
список не закрыла, и следующий ввод (Enter/клик) выбрал в нём другой пункт.

Снимок rpa_pages обрезан на 4 МБ, портала списка в нём нет — список
синтетический, по скриншоту: пункты-радио 13…65+ в прокручиваемом
`[role=listbox]`. Поле Age — настоящее, из fixtures/ads_manager/
audience_age_collapsed_2026_09_25.html (open_card). Модель Meta: доверенный
клик по пункту выбирает его и закрывает список; клик из скрипта
(`isTrusted=false`) только подсвечивает и пишет значение, список остаётся;
Enter в открытом списке выбирает подсвеченный верхний пункт; повторный клик
по раскрытой выпадашке сворачивает список, ничего не выбирая.
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
from tests.test_collapsed_age_row_is_opened import FRAG  # noqa: E402

_META_LIST = r"""
() => {
  const ages = []; for (let a = 13; a <= 64; a++) ages.push(String(a)); ages.push('65+');
  const triggers = [...document.querySelectorAll('[role=combobox]')];
  const valueOf = (t) => [...t.querySelectorAll('div')].find(d => d.children.length === 0 && d.textContent.trim());
  let open = null;
  const close = () => {
    if (!open) return;
    open.list.remove(); open.trigger.setAttribute('aria-expanded', 'false'); open = null;
  };
  const pick = (v) => {
    // misroute: значение уходит в соседнюю выпадашку (проверка «своей» кнопки).
    const t = window.misroute ? triggers.find(x => x !== open.trigger) : open.trigger;
    valueOf(t).textContent = v;
  };
  const cover = (el, name) => {
    const r = el.getBoundingClientRect();
    const c = document.createElement('div');
    c.style.cssText = `position:fixed;left:${r.x - 2}px;top:${r.y - 2}px;width:${r.width + 4}px;`
      + `height:${r.height + 4}px;z-index:99;background:rgba(0,0,0,.1)`;
    c.addEventListener('click', () => { (window.coverClicks = window.coverClicks || []).push(name); });
    document.body.appendChild(c);
  };
  window.__cover = cover;
  const show = (t) => {
    const list = document.createElement('div');
    list.setAttribute('role', 'listbox');
    list.style.cssText = 'position:fixed;left:20px;top:40px;width:180px;height:240px;overflow:auto;background:#fff;z-index:9';
    for (const a of ages) {
      const row = document.createElement('div');
      row.setAttribute('role', 'option');
      row.style.cssText = 'height:30px';
      row.innerHTML = '<div role="radio"></div><div>' + a + '</div>';
      row.addEventListener('click', (e) => {
        pick(a);
        window.highlighted = a;
        if (e.isTrusted && !window.stubborn) close();   // скриптовый клик список не закрывает
      });
      list.appendChild(row);
    }
    document.body.appendChild(list);
    t.setAttribute('aria-expanded', 'true');
    open = { list, trigger: t };
    if (window.coverTriggerOnOpen) cover(t, 'trigger');
    // coverOption: поверх пунктов списка встаёт чужой слой (подсказка Meta).
    if (window.coverOption) cover(list, 'option');
    window.highlighted = null;
    list.addEventListener('scroll', () => {
      const first = [...list.children].find(r => r.offsetTop >= list.scrollTop);
      window.highlighted = first ? first.textContent.trim() : null;
    });
  };
  for (const t of triggers) {
    t.addEventListener('click', (e) => {
      window.clicks = (window.clicks || 0) + 1;
      if (open && open.trigger === t) close(); else { close(); show(t); }
    });
  }
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && open && window.highlighted) { pick(window.highlighted); close(); }
  });
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
def page(browser, monkeypatch):
    rows = []
    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    monkeypatch.setattr(md._telemetry, "record", lambda step, ok=None, **k: rows.append((step, ok, k)))
    monkeypatch.setattr(md, "_cdp_mouse_click", lambda pg, x, y: pg.mouse.click(x, y))
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    p = ctx.new_page()
    p.route("**/*", lambda route: route.abort())
    p.rows = rows
    p.set_content(f"<div style='margin-top:420px;width:600px'>{FRAG['open_card']}</div>")
    p.evaluate(_META_LIST)
    try:
        yield p
    finally:
        ctx.close()


def _listbox_open(page) -> bool:
    return page.locator("[role=listbox]").count() > 0


def test_the_age_is_picked_by_a_real_click_and_the_list_closes(page):
    assert md._age_triggers_read(page) == ["18", "65+"]
    assert md._age_boundaries_pass(page, "35", "65+") is True
    assert not _listbox_open(page), "список минимального возраста остался открытым"
    assert md._age_triggers_read(page) == ["35", "65+"]
    assert not [r for r in page.rows if r[0] == "listbox_left_open"], "список закрыл не сам выбор"
    # Следующий ввод (Enter шага локаций) уже ничего не перевыбирает.
    page.keyboard.press("Enter")
    assert md._age_triggers_read(page) == ["35", "65+"]


def test_a_list_left_open_is_closed_by_its_own_trigger_without_a_pick(page):
    page.locator("[role=combobox]").first.click()
    page.evaluate("document.querySelector('[role=listbox]').scrollTop = 510")
    assert _listbox_open(page)
    assert md._close_open_listboxes(page, "test") is True
    assert not _listbox_open(page)
    assert md._age_triggers_read(page) == ["18", "65+"], "закрытие не должно ничего выбирать"
    assert page.rows[-1][:2] == ("listbox_left_open", True)


def test_nothing_open_means_nothing_clicked(page):
    assert md._close_open_listboxes(page, "test") is True
    assert page.evaluate("window.clicks || 0") == 0
    assert not page.rows


def test_a_list_meta_leaves_open_after_the_pick_is_closed_before_success(page):
    """Даже доверенный клик Meta может не закрыть список — граница засчитывается
    только при закрытом списке и значении в своей выпадашке."""
    page.evaluate("window.stubborn = true")
    assert md._age_boundaries_pass(page, "35", "65+") is True
    assert not _listbox_open(page)
    assert md._age_triggers_read(page) == ["35", "65+"]


# ── шаг возраста целиком и начало шага локаций ─────────────────────────────

def _no_manual_audience(monkeypatch):
    monkeypatch.setattr(md, "_ensure_manual_audience", lambda p: False)
    monkeypatch.setattr(md, "_cancel_dynamic_creative_dialog", lambda p: None)


def test_the_age_step_closes_a_list_left_open_before_reporting_success(page, monkeypatch):
    _no_manual_audience(monkeypatch)
    page.evaluate("window.stubborn = true")
    # Список открыт и после шага — как в 25c557ae0ca7: закрываем, потом успех.
    monkeypatch.setattr(md, "_uncheck_age_suggestion",
                        lambda p: p.locator("[role=combobox]").first.click())
    assert md.fill_audience_age(page, 35, 65) is True
    assert not _listbox_open(page)
    assert md._age_triggers_read(page) == ["35", "65+"]


def test_the_age_step_fails_while_a_list_stays_open(page, monkeypatch):
    _no_manual_audience(monkeypatch)
    monkeypatch.setattr(md, "_fill_audience_age_controls", lambda p, lo, hi: True)
    page.evaluate("""() => { document.querySelectorAll('[role=combobox] div').forEach(d => {
        if (d.children.length === 0 && d.textContent.trim() === '18') d.textContent = '35'; }); }""")
    page.locator("[role=combobox]").first.click()
    # Кнопку списка перекрыли — закрыть нечем: шаг не отчитывается успехом.
    page.evaluate("""() => { const c = document.createElement('div');
        c.style.cssText = 'position:fixed;inset:300px 0 0 0;z-index:99'; document.body.appendChild(c); }""")
    assert md.fill_audience_age(page, 35, 65) is False
    assert ("audience_age", False) in [r[:2] for r in page.rows]
    assert any(r[2].get("note") == "listbox_left_open" for r in page.rows if r[0] == "audience_age")


def test_the_locations_step_starts_with_no_list_open(monkeypatch):
    """В потоке: перед локациями открытые списки закрываются."""
    import ast
    src = (ROOT / "src/fb_ads_rpa/meta_dom.py").read_text(encoding="utf-8")
    flow = next(n for n in ast.parse(src).body
                if isinstance(n, ast.FunctionDef) and n.name == "run_recorded_campaign_flow")
    body = ast.get_source_segment(src, flow)
    i = body.index('_close_open_listboxes(page, "before_audience_location")')
    assert i < body.index("fill_audience_location(page, country)")


# ── повторное ревью v3 PR #314 (находка 6): защитные условия закрытия ──────

def test_a_covered_trigger_is_not_clicked_and_the_list_stays_reported(page):
    """L3: кнопку списка перекрыли — не жмём по перекрытию, отвечаем «открыт»."""
    page.evaluate("window.coverTriggerOnOpen = true")
    page.locator("[role=combobox]").first.click()
    assert _listbox_open(page)
    assert md._close_open_listboxes(page, "test") is False
    assert page.evaluate("window.coverClicks || []") == []


def test_a_search_input_combobox_is_never_clicked(page):
    """L4: у поиска локаций свой список подсказок — поле ввода не трогаем."""
    page.evaluate("""() => { const i = document.createElement('input');
      i.setAttribute('role', 'combobox'); i.setAttribute('aria-expanded', 'true');
      i.setAttribute('aria-haspopup', 'listbox'); i.placeholder = 'Search locations';
      i.style.cssText = 'position:fixed;left:700px;top:40px;width:200px;height:30px';
      i.addEventListener('click', () => { window.inputClicked = true; });
      document.body.appendChild(i); }""")
    assert md._close_open_listboxes(page, "test") is True
    assert page.evaluate("window.inputClicked || false") is False


def test_a_covered_option_is_not_picked(page):
    """L5: пункты списка перекрыты — по перекрытию не жмём, граница не засчитана."""
    page.evaluate("window.coverOption = '35'; window.coverTriggerOnOpen = false")
    assert md._age_boundaries_pass(page, "35", "65+") is False
    assert "option" not in page.evaluate("window.coverClicks || []")
    assert md._age_triggers_read(page)[0] != "35"


def test_a_boundary_with_its_list_left_open_does_not_count(page):
    """L7: выбор стал, но список не закрылся и закрыть его нечем (кнопка
    перекрыта) — ни граница, ни итоговый вид поля не засчитываются."""
    page.evaluate("window.stubborn = true; window.coverTriggerOnOpen = true")
    assert md._age_boundaries_pass(page, "35", "65+") is False
    assert _listbox_open(page)


def test_a_boundary_is_read_from_its_own_trigger(page):
    """L8: значение попало в соседнюю выпадашку — граница «от» не засчитана."""
    page.evaluate("window.misroute = true")
    assert md._age_boundaries_pass(page, "35", "35") is False


def test_a_visible_list_without_an_expanded_trigger_counts_as_open(page):
    """L10: видимый список без раскрытой кнопки — всё равно открытый список."""
    page.evaluate("""() => { const l = document.createElement('div'); l.setAttribute('role', 'listbox');
      l.style.cssText = 'position:fixed;left:20px;top:40px;width:180px;height:100px';
      l.textContent = 'something'; document.body.appendChild(l); }""")
    assert md._close_open_listboxes(page, "test") is False
