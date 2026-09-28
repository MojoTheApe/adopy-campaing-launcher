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
