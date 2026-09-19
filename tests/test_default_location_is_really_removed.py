# -*- coding: utf-8 -*-
"""Страна по умолчанию действительно снимается, а не «снята» (Doc21, 16.09).

GT_Ait_All_Video_PF_35_16.09: в локациях остались Guatemala **и** United
States, а шаг записал `locations_remove_default_removedTrue` — старый код
нажал какой-то «Close» (на странице есть ✕ у баннера WhatsApp Status в
площадках) и счёл дело сделанным. Раскладка 2026-09 группирует локации по
странам: заголовок страны с ✕ рядом.

Проверяется в офлайн-Chrome на карточке той же формы: снимается именно
лишняя страна, нужная остаётся, ✕ вне карточки не трогается, безымянные
иконки (сворачивание карточки) не трогаются, а неснятая страна — отказ шага.
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

_CARD = """
<!doctype html><html><body>
<div id="banner">WhatsApp Status included <div role="button" style="display:inline-block;width:20px;height:20px;overflow:hidden" onclick="window.clicked.push('banner')"><span>Close</span></div></div>
<div id="loc">
  <div>Locations <div role="button" style="display:inline-block;width:20px;height:20px;overflow:hidden" id="collapse" onclick="window.clicked.push('collapse')"><svg width="12" height="12"></svg></div></div>
  <div class="grp" data-c="Guatemala"><div>Guatemala <div role="button" style="display:inline-block;width:20px;height:20px;overflow:hidden" onclick="drop('Guatemala')"><span>Close</span></div></div><div>Guatemala</div></div>
  <div class="grp" data-c="United States"><div>United States <div role="button" style="display:inline-block;width:20px;height:20px;overflow:hidden" %(us_click)s><span>Close</span></div></div><div>United States</div></div>
  <div><div>Include</div><div role="combobox"><input placeholder="Search locations"></div><div role="button">Browse</div></div>
</div>
<script>
  window.clicked = [];
  function drop(c) { window.clicked.push(c); const g = document.querySelector('.grp[data-c="' + c + '"]'); if (g) g.remove(); }
</script>
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
    monkeypatch.setattr(md, "_human_jitter", lambda base, spread: 0.0)
    ctx = browser.new_context(viewport={"width": 1280, "height": 800})
    p = ctx.new_page()
    try:
        yield p
    finally:
        ctx.close()


def test_the_stray_default_country_is_removed_and_the_target_stays(page):
    page.set_content(_CARD % {"us_click": "onclick=\"drop('United States')\""})
    assert md._clear_other_locations(page, "Guatemala") == []
    assert page.evaluate("window.clicked") == ["United States"]
    assert page.evaluate("!!document.querySelector('.grp[data-c=Guatemala]')")


def test_a_close_outside_the_locations_card_is_never_clicked(page):
    page.set_content(_CARD % {"us_click": "onclick=\"drop('United States')\""})
    md._clear_other_locations(page, "Guatemala")
    clicked = page.evaluate("window.clicked")
    assert "banner" not in clicked and "collapse" not in clicked


def test_a_default_that_will_not_go_is_reported_by_name(page):
    page.set_content(_CARD % {"us_click": "onclick=\"window.clicked.push('noop')\""})
    left = md._clear_other_locations(page, "Guatemala")
    assert left and "United States" in left[0]


def test_nothing_to_clear_is_clean(page):
    page.set_content(_CARD % {"us_click": "onclick=\"drop('United States')\""})
    page.evaluate("document.querySelector('.grp[data-c=\"United States\"]').remove()")
    assert md._clear_other_locations(page, "Guatemala") == []
    assert page.evaluate("window.clicked") == []


# ── площадки: карточка, на которой Meta убрала ручной выбор (16.09) ─────────

def test_the_card_without_manual_placements_is_recognised(page):
    page.set_content(
        "<div role=heading>Placements</div><div>Advantage+ on</div>"
        "<div role=heading>Changes to placement settings are coming soon</div>"
        "Excluding placements or platforms will\n no longer be available for your ad sets."
        "<div>Account controls</div><div>Excluded placements: None</div>"
    )
    assert md._placements_exclusion_removed(page) is True
    page.set_content("<div>Placements</div><div>Feeds</div><div>Stories, Status, Reels</div>")
    assert md._placements_exclusion_removed(page) is False


def test_the_country_is_typed_again_when_no_suggestions_open(page, monkeypatch):
    """17.09 CO_bnc FX: «Colombia» в поле, подсказок нет — набираем заново."""
    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    page.set_content(
        "<input id='q' placeholder='Search locations'><ul id='list'></ul>"
        "<script>let cleared=false; const q=document.getElementById('q');"
        "q.addEventListener('input',()=>{ if(!q.value) cleared=true;"
        " if(cleared && q.value==='Colombia'){ document.getElementById('list').innerHTML="
        "\"<li role='option' onclick=\\\"window.picked=this.textContent\\\">Colombia (Country)</li>\"; } });"
        "</script>"
    )
    page.click("#q")
    page.keyboard.type("Colombia")
    monkeypatch.setattr(md, "_wait_location_options",
                        lambda root, timeout_s=6.0, _orig=md._wait_location_options: _orig(root, 1.0))
    assert md._pick_country_suggestion(page, page, "Colombia") is True
    assert page.evaluate("window.picked") == "Colombia (Country)"


def test_an_expanded_location_search_counts_as_open_suggestions(page):
    page.set_content("<input placeholder='Search locations' aria-expanded='true' value='Chile'>")
    assert md._wait_location_options(page, 0.5) is True
    page.set_content("<input placeholder='Search locations' aria-expanded='false' value='Chile'>")
    assert md._wait_location_options(page, 0.5) is False


def test_no_suggestions_after_retry_fails_without_blind_enter(page, monkeypatch):
    page.set_content("<input placeholder='Search locations' id='country'>")
    page.locator('#country').fill('Colombia')
    page.locator('#country').focus()
    monkeypatch.setattr(md, '_wait_location_options', lambda root: False)
    monkeypatch.setattr(md._telemetry, 'record', lambda *a, **k: None)
    page.evaluate("window.entered=false; document.addEventListener('keydown', e => {if(e.key==='Enter') window.entered=true;})")
    assert md._pick_country_suggestion(page, page, 'Colombia') is False
    assert page.evaluate('window.entered') is False


def test_unreadable_country_rows_cannot_confirm_success(page, monkeypatch):
    page.set_content("<input placeholder='Search locations'>")
    monkeypatch.setattr(md, '_pick_country_suggestion', lambda *a: True)
    monkeypatch.setattr(md._telemetry, 'record', lambda *a, **k: None)
    import src.fb_ads_rpa.page_capture as pc
    monkeypatch.setattr(pc, 'capture', lambda *a, **k: None)
    assert md._pick_country_and_clear_defaults(page, page, 'Colombia') is False


def test_missing_target_does_not_remove_the_only_remaining_country(page):
    page.set_content(_CARD % {'us_click': 'onclick="drop(\'United States\')"'})
    page.evaluate("document.querySelector('.grp[data-c=Guatemala]').remove()")
    assert md._clear_other_locations(page, 'Guatemala')
    assert page.evaluate('window.clicked') == []


def test_a_collapsed_locations_card_is_opened_by_its_pencil(page, monkeypatch):
    """18.09 07:07 CL_ml Day: «Inclusion: Chile, United States», снять лишнюю нечем."""
    page.set_content(
        "<div style='position:absolute;left:0;top:0'>"
        "<div><span>Locations</span><span>Inclusion: Chile, United States</span>"
        "<div role='button' aria-label='Edit' id='pen' style='width:24px;height:24px'"
        " onclick=\"window.opened=true\"></div></div></div>"
    )
    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    monkeypatch.setattr(md, "_cdp_mouse_click",
                        lambda pg, x, y: pg.eval_on_selector("#pen", "el => el.click()"))
    assert md._click_locations_edit(page) is True
    assert page.evaluate("window.opened") is True


def test_the_pencil_is_used_before_giving_up_on_unreadable_rows(page, monkeypatch):
    """Строки читаются только после раскрытия карточки."""
    opened = {"yes": False}
    rows = [{"name": "United States", "x": 1.0, "y": 2.0}, {"name": "Chile", "x": 3.0, "y": 4.0}]
    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    monkeypatch.setattr(md, "_click_locations_edit",
                        lambda pg: opened.update(yes=True) or True)
    monkeypatch.setattr(md, "_location_rows", lambda pg: rows if opened["yes"] else [])
    monkeypatch.setattr(md, "_cdp_mouse_click", lambda pg, x, y: rows.pop(0))
    monkeypatch.setattr(md._telemetry, "record", lambda *a, **k: None)
    page.set_content("<div>Locations</div>")
    assert md._clear_other_locations(page, "Chile") == []
    assert opened["yes"] is True


def test_a_row_without_a_pencil_is_opened_by_clicking_its_value(page, monkeypatch):
    """18.09 08:23 CL_ml Trading: у свёрнутой строки ни одной кнопки, только текст."""
    page.set_content(
        "<div style='position:absolute;left:0;top:0'>"
        "<div><div>Locations​</div><div id='val'>Inclusion: Chile, United States</div></div></div>"
    )
    clicked = {}
    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    monkeypatch.setattr(md, "_cdp_mouse_click",
                        lambda pg, x, y: clicked.update(at=pg.evaluate(
                            "([x,y]) => (document.elementFromPoint(x,y)||{}).id", [x, y])))
    assert md._click_locations_edit(page) is True
    assert clicked["at"] == "val", clicked


def test_the_value_click_is_not_used_when_a_pencil_exists(page, monkeypatch):
    page.set_content(
        "<div style='position:absolute;left:0;top:0'>"
        "<div><div>Locations</div><div>Inclusion: Chile</div>"
        "<div role='button' aria-label='Edit' id='pen' style='width:24px;height:24px'></div></div></div>"
    )
    clicked = {}
    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    monkeypatch.setattr(md, "_cdp_mouse_click",
                        lambda pg, x, y: clicked.update(at=pg.evaluate(
                            "([x,y]) => (document.elementFromPoint(x,y)||{}).id", [x, y])))
    assert md._click_locations_edit(page) is True
    assert clicked["at"] == "pen", clicked


def test_a_summary_row_with_only_an_expander_is_opened_by_it(page, monkeypatch):
    """19.09 15:00 SA_arc GMBL: «Inclusion: Saudi Arabia, United States», ни
    карандаша, клик по значению не раскрывает — справа безымянная стрелка."""
    page.set_content(
        "<div style='position:absolute;left:0;top:0;width:600px'>"
        "<div><span>Locations</span><span>​</span>"
        "<div><span>Inclusion:</span><span id='val'>Saudi Arabia, United States</span></div>"
        "<div role='button' aria-expanded='false' id='arrow' style='width:24px;height:24px'></div></div></div>")
    clicked = {}
    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    monkeypatch.setattr(md, "_cdp_mouse_click",
                        lambda pg, x, y: clicked.update(at=pg.evaluate(
                            "([x,y]) => (document.elementFromPoint(x,y)||{}).id", [x, y])))
    assert md._click_locations_edit(page) is True
    assert clicked["at"] == "arrow", clicked


def test_a_whole_row_button_far_below_is_scrolled_to_and_opened(page, monkeypatch):
    """19.09 18:25 SA_arc Trading: вся строка — role=button aria-expanded=false,
    подпись внутри неё, и строка на y≈3500 — за краем окна."""
    page.set_content(
        "<div style='height:3400px'></div>"
        "<div role='listitem'><div role='button' aria-expanded='false' id='row' "
        "onclick=\"this.setAttribute('aria-expanded','true')\">"
        "<span>Locations</span><div><span>Inclusion:</span><span>United States</span></div></div></div>"
        "<div style='height:2000px'></div>")
    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    monkeypatch.setattr(md, "_cdp_mouse_click", lambda pg, x, y: pg.evaluate(
        "([x, y]) => { const e = document.elementFromPoint(x, y); if (e) e.click(); }", [x, y]))
    assert md._click_locations_edit(page) is True
    assert page.eval_on_selector("#row", "el => el.getAttribute('aria-expanded')") == "true"
