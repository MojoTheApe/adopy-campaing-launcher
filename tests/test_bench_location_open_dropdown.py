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
снимке; поле поиска пусто, у него `aria-activedescendant="SA"`, но ни списка,
ни элемента с id="SA" в DOM нет — атрибут устаревший, и что подсказка была
«подсвечена, но не выбрана», снимки не доказывают (ревью PR #632, F4).

Две причины:
1. Имя строки читалось вверх от «Remove» и брало подпись соседней кнопки: в
   отказ попадала «страна» «Open Dropdown».
2. Шаг считал страну добавленной, потому что Enter без видимых вариантов
   (`outcome == "entered"`) «сработал», хотя в карточке её нет: дальше —
   `default_location_left` с названием не той страны. Теперь после Enter
   страна ищется в карточке; нет — попытка не засчитана (`not_in_card`), и
   лестница набирает заново. Запасной путь: если подсвеченная подсказка
   (`aria-activedescendant`) — сама страна, она нажимается; город или регион
   не нажимается никогда (ACL-1390, ревью PR #632 F1).

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
# подсвеченной подсказке; `window.active` — какая подсказка подсвечена
# (`kind`: country — страна, city/region — строка под заголовком страны).
_EMULATION = """
<script>
  window.enter_adds = false; window.click_adds = true; window.log = [];
  window.active = {id: 'SA', text: 'Saudi Arabia', kind: 'country', row: ''};
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
  // Город или регион: заголовок группы — «Saudi Arabia», строка — сам город.
  function addPlace(name) {
    const first = groups()[0], c = first.cloneNode(true);
    const w = document.createTreeWalker(c, NodeFilter.SHOW_TEXT);
    let seen = 0;
    for (let n = w.nextNode(); n; n = w.nextNode()) {
      if (!n.textContent.includes('United States')) continue;
      n.textContent = n.textContent.replace('United States', seen++ ? name : 'Saudi Arabia');
    }
    for (const b of c.querySelectorAll('*')) b.__w = false;
    first.parentElement.appendChild(c); wire(); sync();
  }
  window.addPlace = addPlace;
  const q = document.querySelector('input[placeholder="Search locations"]');
  q.addEventListener('input', () => {
    if (!q.value) { q.setAttribute('aria-expanded', 'false'); return; }
    const a = window.active;
    q.setAttribute('aria-expanded', 'true'); q.setAttribute('aria-activedescendant', a.id);
    let o = document.getElementById(a.id);
    if (!o) {
      o = document.createElement('div'); o.id = a.id; o.textContent = a.text;
      o.style.cssText = 'padding:6px;border:1px solid #999;width:200px'; card().appendChild(o);
      o.addEventListener('click', () => {
        window.log.push(a.kind === 'country' ? 'option_click' : a.kind + '_click');
        if (window.click_adds) { if (a.kind === 'country') addSaudi(); else addPlace(a.row); }
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
    # Ревью PR #632 (F7): итоговая строка — «страна не встала в карточку»,
    # а не «подсказок нет».
    final = [k for step, _ok, k in ladder if step == "audience_location"]
    assert len(final) == 1 and final[0].get("note") == "not_in_card"
    assert set(final[0]["diag"]["outcomes"]) == {"not_in_card"}


_CITY = {"id": "2145812", "text": "Riyadh, Saudi Arabia (City)", "kind": "city", "row": "Riyadh"}
_REGION = {"id": "3302", "text": "Riyadh Province, Saudi Arabia (Region)", "kind": "region",
           "row": "Riyadh Province"}


@pytest.mark.parametrize("active", [_CITY, _REGION], ids=["city", "region"])
def test_a_highlighted_city_or_region_is_never_pressed(offline_page, ladder, active):
    """Ревью PR #632 (F1): подсвечен город или регион страны — не нажимаем
    (ACL-1390), страна не засчитана, лестница идёт дальше и кончается отказом."""
    _card(offline_page)
    offline_page.evaluate("(a) => { window.active = a; }", active)
    _typed(offline_page)
    assert md._click_active_suggestion(offline_page, "Saudi Arabia") is False
    assert md._pick_country_suggestion(offline_page, offline_page, "Saudi Arabia") is False
    log = offline_page.evaluate("window.log")
    assert "city_click" not in log and "region_click" not in log, log
    assert _names(offline_page) == ["United States"]
    notes = [k.get("note") for step, _ok, k in ladder if step == "audience_location_attempt"]
    assert notes and set(notes) == {"not_in_card"}


def test_the_highlighted_country_is_pressed_and_counts(offline_page, ladder):
    """Подсвечена сама страна (id = ISO-код, текст — имя страны) — нажимаем, успех."""
    _card(offline_page)
    _typed(offline_page)
    assert md._pick_country_suggestion(offline_page, offline_page, "Saudi Arabia") is True
    assert offline_page.evaluate("window.log") == ["enter", "option_click"]


def test_a_country_option_without_text_is_recognised_by_its_iso_id():
    assert md._active_suggestion_is_country({"id": "SA", "text": ""}, "Saudi Arabia") is True
    assert md._active_suggestion_is_country({"id": "2145812", "text": ""}, "Saudi Arabia") is False
    assert md._active_suggestion_is_country({"id": "SA", "text": "Riyadh, Saudi Arabia (City)"},
                                            "Saudi Arabia") is False
    assert md._active_suggestion_is_country({"id": "x", "text": "Saudi Arabia (Country)"},
                                            "Saudi Arabia") is True


@pytest.mark.parametrize("place", ["Riyadh", "Riyadh, Saudi Arabia"])
def test_a_city_under_the_country_heading_is_not_the_country(offline_page, monkeypatch, place):
    """Ревью PR #632 (F1): город встал строкой под заголовком «Saudi Arabia» —
    заголовок группы страной не считается, нужна строка самой страны."""
    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    _card(offline_page)
    offline_page.evaluate("(n) => addPlace(n)", place)
    assert "Saudi Arabia" in _names(offline_page) and place in _names(offline_page)
    assert md._country_added_or_unread(offline_page, "Saudi Arabia") is False
    offline_page.evaluate("addSaudi()")
    assert md._country_added_or_unread(offline_page, "Saudi Arabia") is True


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
