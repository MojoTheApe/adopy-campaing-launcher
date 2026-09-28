# -*- coding: utf-8 -*-
"""Сводка Locations нового редактора читается, и верная страна проходит (ACL-1327).

Первый живой прогон нового редактора (28.09, 97314905598e, Facebook_1443):
до шага в поле стояло «Inclusion: United States» (снимок 00), робот ввёл
Colombia, снял лишнюю страну — и шаг всё равно отказал с
`location_rows_not_read`: после ✕ Meta свернула поле в строку
`<span><strong>Inclusion:</strong> Colombia</span>` (снимок 01), строк со
✕ больше нет, а чтение сводки брало лист `<strong>Inclusion:</strong>` и
видело пустой список стран. Черновик остановился со словами «страна не
выставлена», хотя она стояла верно.

Фрагменты — `tests/fixtures/ads_manager/locations_simple_create_2026_09_28.html`.
Проверка обязана по-прежнему отказывать, когда страна не та.
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

FIX = (ROOT / "tests" / "fixtures" / "ads_manager"
       / "locations_simple_create_2026_09_28.html").read_text(encoding="utf-8")
FRAG = {m.group(1): m.group(2) for m in re.finditer(
    r"<!-- fragment: (\w+) \| source: [^>]*-->\n(.*?)(?=\n<!-- fragment: |\Z)", FIX, re.S)}


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
    monkeypatch.setattr(md, "_human_jitter", lambda base, spread: 0.0)
    monkeypatch.setattr(md._telemetry, "record", lambda *a, **k: None)
    import src.fb_ads_rpa.page_capture as pc
    monkeypatch.setattr(pc, "capture", lambda *a, **k: None)
    ctx = browser.new_context(viewport={"width": 1280, "height": 800})
    p = ctx.new_page()
    try:
        yield p
    finally:
        ctx.close()


def _wrap(fragment: str) -> str:
    return f"<!doctype html><html><body><div style='width:600px'>{fragment}</div></body></html>"


def test_the_live_collapsed_field_reads_its_country(page):
    page.set_content(_wrap(FRAG["field_target"]))
    assert md._location_summary_countries(page) == ["Colombia"]
    assert md._only_wanted_in_summary(page, "Colombia") is True


def test_the_live_default_country_is_not_taken_for_the_target(page):
    page.set_content(_wrap(FRAG["field_default"]))
    assert md._location_summary_countries(page) == ["United States"]
    assert md._only_wanted_in_summary(page, "Colombia") is False


def test_the_collapsed_audience_card_grid_reads_its_country(page):
    """Свёрнутая карточка Audience (снимок 02): «Locations | Colombia» в сетке."""
    page.set_content(_wrap(FRAG["card_summary"]))
    assert md._location_summary_countries(page) == ["Colombia"]
    assert md._only_wanted_in_summary(page, "Colombia") is True
    assert md._only_wanted_in_summary(page, "Colom") is False


# Раскрытое поле, как его видит шаг сразу после выбора страны: Colombia и
# United States со своими ✕. ✕ у United States сворачивает поле в сводку
# из живого снимка (как Meta 28.09).
_OPEN = """
<!doctype html><html><body><div style='width:600px' id='host'>
<div id="loc">
  <div>Locations</div>
  <div class="grp" data-c="Colombia"><div>Colombia <div role="button" style="display:inline-block;width:20px;height:20px;overflow:hidden"><span>Close</span></div></div></div>
  <div class="grp" data-c="United States"><div>United States <div role="button" style="display:inline-block;width:20px;height:20px;overflow:hidden" onclick="collapse()"><span>Close</span></div></div></div>
  <div><div>Include</div><div role="combobox"><input placeholder="Search locations"></div></div>
</div></div>
<script>
  window.collapse = () => { document.getElementById('host').innerHTML = window.SUMMARY; };
</script>
</body></html>
"""


def _open_card_collapsing_into(page, summary: str) -> None:
    page.set_content(_OPEN)
    page.evaluate("(s) => { window.SUMMARY = s; }", summary)


def test_the_country_set_by_the_robot_verifies_after_meta_collapses_the_field(page):
    """Ровно путь 97314905598e: строки прочитаны, лишняя снята, поле свернулось."""
    _open_card_collapsing_into(page, FRAG["field_target"])
    assert md._clear_other_locations(page, "Colombia") == []


def test_a_wrong_country_left_in_the_collapsed_field_still_fails(page):
    _open_card_collapsing_into(page, FRAG["field_default"])
    assert md._clear_other_locations(page, "Colombia") is None


def test_the_whole_location_check_passes_on_the_live_collapse(page, monkeypatch):
    monkeypatch.setattr(md, "_pick_country_suggestion", lambda *a: True)
    _open_card_collapsing_into(page, FRAG["field_target"])
    assert md._pick_country_and_clear_defaults(page, page, "Colombia") is True
    _open_card_collapsing_into(page, FRAG["field_default"])
    assert md._pick_country_and_clear_defaults(page, page, "Colombia") is False


# ── Охраны от ложного «да» (ревью PR #340 v4, мутации M1–M4 и `<ul>`) ──────

def _field_with_list(items: str) -> str:
    """Живое поле 01 с непустым `<ul>` рядом со строкой «Inclusion: Colombia»."""
    frag = FRAG["field_target"]
    assert frag.count("<ul></ul>") == 1
    return frag.replace("<ul></ul>", f"<ul>{items}</ul>")


def test_an_extra_country_listed_beside_the_summary_fails(page):
    page.set_content(_wrap(_field_with_list("<li>United States</li>")))
    assert md._location_summary_countries(page) == ["Colombia", "United States"]
    assert md._only_wanted_in_summary(page, "Colombia") is False


def test_a_second_inclusion_line_in_the_list_is_never_clean(page):
    """Вторая строка «Inclusion:» — сводка неоднозначна, чисто не бывает."""
    page.set_content(_wrap(_field_with_list("<li>Inclusion: Mexico, Peru</li>")))
    assert md._only_wanted_in_summary(page, "Colombia") is False


def test_a_country_list_without_items_is_read_whole(page):
    page.set_content(_wrap(FRAG["field_target"].replace("<ul></ul>", "<ul>Mexico, Peru</ul>")))
    assert md._location_summary_countries(page) == ["Colombia", "Mexico", "Peru"]


def test_an_exclusion_line_in_the_list_adds_no_market(page):
    page.set_content(_wrap(_field_with_list("<li>Exclusion: Mexico</li>")))
    assert md._only_wanted_in_summary(page, "Colombia") is True


def test_the_climb_does_not_leave_the_locations_field(page):
    """M1: подъём от «Inclusion:» не выходит за коробки поля — страна в чужом
    заголовке рядом не становится значением Locations."""
    page.set_content(_wrap(
        "<section><div id='field'><div>Locations</div><div><strong>Inclusion:</strong></div></div>"
        "<div role='heading'>Colombia</div></section>"))
    assert md._only_wanted_in_summary(page, "Colombia") is False


def _card(value_cells: str, *, bar: str = "Audience", bar_id: str = "syn_1") -> str:
    return (f"<div id='{bar_id}' role='toolbar'><div role='heading' aria-level='3'>{bar}</div></div>"
            f"<div aria-labelledby='{bar_id}' aria-pressed='false' role='button'><div "
            "style='display:grid; grid-template-columns: 180px 1fr;'>"
            f"{value_cells}</div></div>")


_LOC_CELL = "<div style='grid-area: auto;'><div><div role='heading' aria-level='4'>Locations</div></div></div>"


def test_a_heading_next_to_locations_is_not_its_value(page):
    """M2: у Locations нет значения, следом — ячейка-заголовок «Colombia»."""
    page.set_content(_wrap(_card(
        _LOC_CELL + "<div style='grid-area: auto;'><div><div role='heading' aria-level='4'>Colombia</div></div></div>"
        "<span>x</span>")))
    assert md._location_summary_countries(page) is None


def test_two_audience_grids_are_not_read_as_one(page):
    """M3: два заголовка Locations — какой из них наш, не сказать."""
    page.set_content(_wrap(
        _card(_LOC_CELL + "<span>Colombia</span>", bar_id="syn_1")
        + _card(_LOC_CELL + "<span>United States</span>", bar_id="syn_2")))
    assert md._location_summary_countries(page) is None
    assert md._only_wanted_in_summary(page, "Colombia") is False


def test_a_locations_grid_outside_the_audience_card_is_ignored(page):
    """M4 и привязка к карточке: плитка с «Locations» вне кнопки-сводки или
    под чужой шапкой (сохранённая аудитория) — не сводка карточки Audience."""
    page.set_content(_wrap(
        "<div style='display:grid'>" + _LOC_CELL + "<span>Colombia</span></div>"))
    assert md._location_summary_countries(page) is None
    page.set_content(_wrap(_card(_LOC_CELL + "<span>Colombia</span>", bar="Saved audience")))
    assert md._location_summary_countries(page) is None
    page.set_content(_wrap(
        "<div aria-pressed='false' role='button'>" + _LOC_CELL + "<span>Colombia</span></div>"))
    assert md._location_summary_countries(page) is None
