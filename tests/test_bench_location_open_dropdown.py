# -*- coding: utf-8 -*-
"""Карточка локаций 06.10: «Open Dropdown» не страна, а страна, не вставшая в карточку (ACL-1630).

06.10, Facebook_5265 (`simple_create`), три прогона SA_ARC (aa99ab40d397,
fab05371305f, da07f392ff1f): `audience_location` — `default_location_left`,
`diag.left == ["United States", "Open Dropdown"]`, второй проход — `not_set`.
Снимки `00_audience_location` и `09/10_audience_location_second_pass` всех трёх
прогонов одинаковы (`locations_open_dropdown_card_2026_10_06.html`): в карточке
одна группа «United States» (✕ выключен, страна единственная) и, под ней, её
строка с кнопкой-списком «Open Dropdown» (теперь у кнопки есть видимая
подпись) и выключенным «Remove». Saudi Arabia в карточке нет ни в одном
снимке; поле поиска пусто, `aria-activedescendant="SA"` (подсказка была
выбрана подсветкой), списка нет.

Две причины:
1. Имя строки читалось вверх от «Remove» и брало подпись соседней кнопки: в
   отказ попадала «страна» «Open Dropdown».
2. Шаг считал страну добавленной, потому что Enter без видимых вариантов
   (`outcome == "entered"`) «сработал», хотя в карточке её нет: дальше —
   `default_location_left` с названием не той страны. Теперь после Enter
   страна ищется в карточке; нет — нажимается подсвеченная подсказка
   (`aria-activedescendant`), нет и тогда — это неудавшаяся попытка лестницы.

Добавление страны после выбора подсказки здесь эмулируется (живой список
не снят), карточка и её кнопки — снимок.
"""
from __future__ import annotations

import re

import pytest

from tests.bench_support import FIXTURES, browser, fragment, offline_page  # noqa: F401

from src.fb_ads_rpa import foreign_windows as fw
from src.fb_ads_rpa import meta_dom as md

FIXTURE = FIXTURES / "locations_open_dropdown_card_2026_10_06.html"

#: Без CSS Meta кнопки растянулись бы на всю строку; живьём ✕ и «Remove» — значки.
_CSS = ("<style>button, [role=button] { display: inline-block; width: 24px; height: 24px; overflow: hidden }"
        " [aria-haspopup=menu] { width: auto; height: auto }</style>")

# Поведение Meta: группа страны снимается своим ✕; с одной страной ✕ и
# «Remove» выключены; выбор подсказки добавляет группу. `window.enter_adds` —
# добавляет ли Enter (живьём 06.10 — нет), `window.click_adds` — клик по
# подсвеченной подсказке.
_EMULATION = """
<script>
  window.enter_adds = false; window.click_adds = true; window.log = [];
  const card = () => document.querySelector('#LOCATION');
  const groups = () => [...card().querySelectorAll('ul > li')].filter(li =>
    li.parentElement.parentElement === card().querySelector('ul').parentElement
    && li.parentElement === card().querySelector('ul'));
  function sync() {
    const g = groups();
    for (const li of g) for (const b of li.querySelectorAll('button, [role=button]')) {
      if (b.matches('[aria-haspopup=menu]')) continue;
      const off = g.length < 2;
      if (b.tagName === 'BUTTON') b.disabled = off; else b.setAttribute('aria-disabled', off ? 'true' : 'false');
    }
  }
  function wire() {
    for (const li of groups()) for (const b of li.querySelectorAll('button, [role=button]')) {
      if (b.__w || b.matches('[aria-haspopup=menu]')) continue;
      b.__w = true;
      b.addEventListener('click', () => {
        if (b.disabled || b.getAttribute('aria-disabled') === 'true') return;
        window.log.push('close:' + (li.textContent.includes('United States') ? 'United States' : 'Saudi Arabia'));
        li.remove(); sync();
      });
    }
  }
  function addSaudi() {
    const first = groups()[0], c = first.cloneNode(true);
    const w = document.createTreeWalker(c, NodeFilter.SHOW_TEXT);
    for (let n = w.nextNode(); n; n = w.nextNode()) n.textContent = n.textContent.replace('United States', 'Saudi Arabia');
    for (const b of c.querySelectorAll('*')) b.__w = false;
    first.parentElement.appendChild(c); wire(); sync();
  }
  window.addSaudi = addSaudi;
  const q = document.querySelector('input[placeholder="Search locations"]');
  q.addEventListener('input', () => {
    if (!q.value) { q.setAttribute('aria-expanded', 'false'); return; }
    q.setAttribute('aria-expanded', 'true'); q.setAttribute('aria-activedescendant', 'SA');
    let o = document.getElementById('SA');
    if (!o) {
      o = document.createElement('div'); o.id = 'SA'; o.textContent = 'Saudi Arabia';
      o.style.cssText = 'padding:6px;border:1px solid #999;width:200px'; card().appendChild(o);
      o.addEventListener('click', () => {
        window.log.push('option_click'); if (window.click_adds) addSaudi();
        o.remove(); q.value = ''; q.setAttribute('aria-expanded', 'false');
      });
    }
  });
  q.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { window.log.push('enter'); if (window.enter_adds) addSaudi(); }
  });
  wire(); sync();
</script>
"""


def _card(page, *, saudi: bool = False) -> None:
    page.set_content(_CSS + "<div style='width:700px'>" + fragment(FIXTURE, "card") + "</div>" + _EMULATION)
    if saudi:
        page.evaluate("addSaudi()")


def _names(page) -> list[str]:
    return [r["name"] for r in md._location_rows(page)]


def test_the_fixture_is_the_live_card_with_one_country_locked(offline_page):
    _card(offline_page)
    text = offline_page.inner_text("#LOCATION")
    assert "Open Dropdown" in text and "Saudi" not in text
    assert offline_page.input_value("input[placeholder='Search locations']") == ""


def test_open_dropdown_is_never_read_as_a_location(offline_page):
    _card(offline_page)
    rows = md._location_rows(offline_page)
    assert [(r["name"], bool(r.get("disabled"))) for r in rows] == [("United States", True)]


def test_the_default_country_is_removed_next_to_the_target(offline_page, monkeypatch):
    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    _card(offline_page, saudi=True)
    assert _names(offline_page) == ["United States", "Saudi Arabia"]
    assert md._clear_other_locations(offline_page, "Saudi Arabia") == []
    assert _names(offline_page) == ["Saudi Arabia"]
    assert offline_page.evaluate("window.log") == ["close:United States"]


def test_a_missing_target_leaves_the_default_and_names_it(offline_page, monkeypatch):
    """Страна не добавилась (06.10): единственную страну не трогаем, отказ — с её именем."""
    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    _card(offline_page)
    assert md._clear_other_locations(offline_page, "Saudi Arabia") == ["United States"]
    assert offline_page.evaluate("window.log") == []


@pytest.fixture
def ladder(monkeypatch):
    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    monkeypatch.setattr(md, "_LOCATION_ATTEMPTS", tuple((w, 0, 0.5) for w, _d, _t in md._LOCATION_ATTEMPTS))
    rows: list[tuple] = []
    monkeypatch.setattr(md._telemetry, "record", lambda step, ok=None, **k: rows.append((step, ok, k)))
    return rows


def _typed(page) -> None:
    page.click("input[placeholder='Search locations']")
    page.keyboard.type("Saudi Arabia")


def test_enter_that_adds_nothing_is_followed_by_the_highlighted_option(offline_page, ladder):
    """06.10: Enter ничего не добавил, подсказка подсвечена — её и нажимаем."""
    _card(offline_page)
    _typed(offline_page)
    assert md._pick_country_suggestion(offline_page, offline_page, "Saudi Arabia") is True
    assert _names(offline_page) == ["United States", "Saudi Arabia"]
    assert offline_page.evaluate("window.log") == ["enter", "option_click"]


def test_enter_that_adds_the_country_is_enough(offline_page, ladder):
    """Раскладка, где Enter работает (17.09): подсказку не нажимаем."""
    _card(offline_page)
    offline_page.evaluate("() => { window.enter_adds = true; }")
    _typed(offline_page)
    assert md._pick_country_suggestion(offline_page, offline_page, "Saudi Arabia") is True
    assert offline_page.evaluate("window.log") == ["enter"]


def test_a_country_that_never_lands_in_the_card_is_not_a_success(offline_page, ladder):
    """Ни Enter, ни клик не добавили страну: попытка не засчитывается, лестница идёт дальше."""
    _card(offline_page)
    offline_page.evaluate("() => { window.click_adds = false; }")
    _typed(offline_page)
    assert md._pick_country_suggestion(offline_page, offline_page, "Saudi Arabia") is False
    assert _names(offline_page) == ["United States"]
    notes = [k.get("note") for step, _ok, k in ladder if step == "audience_location_attempt"]
    assert notes and set(notes) == {"not_in_card"}
    assert any(step == "audience_location" and k.get("note") == "no_location_suggestions"
               for step, _ok, k in ladder)


def test_the_whole_step_ends_with_only_the_target_country(offline_page, ladder):
    _card(offline_page)
    _typed(offline_page)
    assert md._pick_country_and_clear_defaults(offline_page, offline_page, "Saudi Arabia") is True
    assert _names(offline_page) == ["Saudi Arabia"]


def test_the_onboarding_modal_is_a_known_window_closed_before_the_step(offline_page):
    """GeoIllustrationModal «A streamlined way to create campaigns» (09:02:49,
    `not_closed`, затем `own_close` в 09:03:12) к шагу локаций закрыт: на
    снимках 00_audience_location нет ни одного диалога, кроме скрытой панели
    Notifications. Окно — известный вид реестра, а не незнакомое."""
    kinds = {k.name: k for k in fw.KNOWN}
    assert "onboarding" in kinds
    assert re.search(kinds["onboarding"].text, "A streamlined way to create campaigns", re.I)
    _card(offline_page)
    assert offline_page.locator("[role=dialog]").count() == 0
