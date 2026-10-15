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
    monkeypatch.setattr(md, '_wait_location_options', lambda root, **k: False)
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


def test_unrelated_summary_cannot_hide_an_extra_country(page, monkeypatch):
    page.set_content('<aside><div>Saved audience</div><div>Inclusion: Chile</div></aside>'
                     '<section><div>Locations</div><div>Inclusion: Chile, United States</div></section>')
    monkeypatch.setattr(md, '_pick_country_suggestion', lambda *a: True)
    monkeypatch.setattr(md._telemetry, 'record', lambda *a, **k: None)
    import src.fb_ads_rpa.page_capture as pc
    monkeypatch.setattr(pc, 'capture', lambda *a, **k: None)
    assert md._pick_country_and_clear_defaults(page, page, 'Chile') is False


def test_only_the_exact_country_in_a_unique_locations_field_is_clean(page):
    page.set_content('<section><div>Locations</div><div>Inclusion: Chile</div></section>')
    assert md._only_wanted_in_summary(page, 'Chile') is True
    assert md._only_wanted_in_summary(page, 'Chi') is False
    page.set_content('<section><div>Locations</div><div>Inclusion: Chile</div></section>' * 2)
    assert md._only_wanted_in_summary(page, 'Chile') is False


def test_opening_locations_does_not_click_adjacent_language_control(page):
    page.set_content('<section><div><div>Locations</div><div id="location-value" '
                     'onclick="window.clicked=\'locations\'">Inclusion: Chile</div></div>'
                     '<div><div>Languages</div><button aria-label="Edit" '
                     'onclick="window.clicked=\'languages\'">Edit</button></div></section>')
    assert md._click_locations_edit(page) is True
    assert page.evaluate('window.clicked') == 'locations'


def test_collapsing_locations_does_not_touch_another_expanded_field(page):
    page.set_content('<section><div><div>Locations</div><div>Inclusion: Chile</div></div>'
                     '<div><div>Languages</div><button aria-expanded="true" '
                     'onclick="window.clicked=true">Edit</button></div></section>')
    assert md._collapse_locations_card(page) is False
    assert page.evaluate('window.clicked === undefined')


def test_own_locations_toggle_can_be_collapsed(page):
    page.set_content('<section><div>Locations</div><div>Inclusion: Chile</div>'
                     '<button aria-label="Collapse locations" aria-expanded="true" '
                     'onclick="this.setAttribute(\'aria-expanded\', \'false\')">Collapse</button></section>')
    assert md._collapse_locations_card(page) is True
    assert page.locator('button').get_attribute('aria-expanded') == 'false'


def test_nested_locations_text_keeps_its_own_edit_and_collapse_control(page):
    page.set_content('<section style="display:flex"><div><div>Locations</div>'
                     '<div>Inclusion: Chile</div></div><button aria-label="Edit locations" '
                     'aria-expanded="false" onclick="this.setAttribute(\'aria-expanded\', '
                     'this.getAttribute(\'aria-expanded\')===\'true\'?\'false\':\'true\')">Edit</button></section>')
    assert md._click_locations_edit(page) is True
    assert page.locator('button').get_attribute('aria-expanded') == 'true'
    assert md._collapse_locations_card(page) is True
    assert page.locator('button').get_attribute('aria-expanded') == 'false'


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


def _record_clicks(page, monkeypatch):
    clicked = {}
    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    monkeypatch.setattr(md, "_cdp_mouse_click",
                        lambda pg, x, y: clicked.update(at=pg.evaluate(
                            "([x,y]) => { const e = document.elementFromPoint(x,y); "
                            "return e && (e.closest('[id]') || {}).id; }", [x, y])))
    return clicked


def test_a_whole_row_button_deep_above_the_label_is_opened_after_scrolling(page, monkeypatch):
    """20.09 00:16 SA_arc GMBL: вся строка — role=button aria-expanded=false
    на 10 уровней выше подписи, и строка ниже края окна."""
    deep = "<div>" * 9 + "<span>Locations</span><span>​</span>" + "</div>" * 9
    page.set_content(
        "<div style='height:2500px'></div>"
        "<div role='button' aria-expanded='false' id='row'>" + deep
        + "<div>Inclusion: Saudi Arabia, United States</div></div>"
        "<div style='height:2500px'></div>")
    clicked = _record_clicks(page, monkeypatch)
    assert md._click_locations_edit(page) is True
    assert clicked["at"] == "row", clicked


def test_a_row_hidden_under_the_editor_footer_is_never_clicked_through(page, monkeypatch):
    """20.09 00:16 SA_arc GMBL: «All languages» под нижней панелью с Close —
    клик по координатам закрыл редактор. Такую точку не жмём."""
    page.set_content(
        "<div style='position:fixed;left:0;right:0;top:0;bottom:0;z-index:5;background:#fff'>"
        "<button id='close'>Close</button></div>"
        "<div><div>Languages</div><div id='val'>All languages</div></div>")
    clicked = _record_clicks(page, monkeypatch)
    assert md._click_languages_edit(page) is False
    assert "at" not in clicked, clicked


def test_the_pencil_that_only_exists_under_the_cursor_is_found(page, monkeypatch):
    """20.09 01:30 SA_arc GMBL: карандаш строки Meta рисует только под курсором."""
    page.set_content(
        "<style>#pen{display:none;width:24px;height:24px}"
        "#row:hover #pen{display:block}</style>"
        "<div id='row' style='width:600px'><div>Locations</div>"
        "<div>Inclusion: Saudi Arabia, United States</div>"
        "<div role='button' aria-label='Edit' id='pen'></div></div>")
    clicked = _record_clicks(page, monkeypatch)
    assert md._click_locations_edit(page) is True
    assert clicked["at"] == "pen", clicked


def test_a_row_far_below_the_window_is_scrolled_to_before_the_click(page, monkeypatch):
    """21.09 18:44 CR_ait IPO: строка стояла на y=3811 — клик по её координатам
    уходил в пустоту, и United States оставалась рядом с нужной страной.
    Имя при этом читалось как «United StatesClose»."""
    page.set_content(
        "<div style='height:3600px'></div>"
        "<div id='loc'><div>Locations</div>"
        "<div class='grp' data-c='United States'><div>United States"
        "<div role='button' style='display:inline-block;width:20px;height:20px'"
        " onclick=\"window.clicked.push('us')\"><span>Close</span></div></div></div>"
        "<div><div role='combobox'><input placeholder='Search locations'></div></div></div>"
        "<script>window.clicked = [];</script>"
    )
    rows = md._location_rows(page)
    assert [r["name"] for r in rows] == ["United States"], rows
    assert rows[0]["y"] > 900, "строка должна лежать за окном"

    fresh = md._scroll_location_row_into_view(page, "United States")
    assert fresh and 0 < fresh["y"] < 900, fresh


def test_the_card_is_found_by_its_rows_when_no_locations_heading_exists(page):
    """21.09 20:31 SA_arc GMBL: блока, чей текст начинается словом «Locations»,
    в разметке нет вовсе — поиск упирался в двенадцатый уровень, а строки стран
    оставались в стороне, и список выходил пустым."""
    page.set_content(
        "<div id='outer'>"
        "<div class='grp'><div>Saudi Arabia<div role='button'"
        " style='display:inline-block;width:20px;height:20px'><span>Close</span></div></div></div>"
        "<div class='grp'><div>United States<div role='button'"
        " style='display:inline-block;width:20px;height:20px'><span>Close</span></div></div></div>"
        "<div><div><div><div role='combobox'><input placeholder='Search locations'></div></div></div></div>"
        "</div>"
    )
    names = [r["name"] for r in md._location_rows(page)]
    assert names == ["Saudi Arabia", "United States"], names


# ── стрелка строки: только своя, не выключенная, не «Remove» (PR #314, M6) ──

def test_a_section_toggle_holding_other_fields_is_not_the_rows_expander(page, monkeypatch):
    """Свёрнутая кнопка-раздел над подписью, но в ней и чужое поле
    (Languages): это не строка Locations — жать её нельзя."""
    page.set_content(
        "<div role='button' aria-expanded='false' id='section' style='width:600px'>"
        "<div><span>Locations</span><span>Inclusion: Chile</span></div>"
        "<div><span>Languages</span><span>All languages</span></div></div>")
    clicked = _record_clicks(page, monkeypatch)
    assert md._click_row_expander(page, "Locations") is False
    assert "at" not in clicked, clicked


def test_a_disabled_whole_row_button_is_not_clicked(page, monkeypatch):
    page.set_content(
        "<div role='button' aria-expanded='false' aria-disabled='true' id='row' style='width:600px'>"
        "<div><span>Locations</span></div><div>Inclusion: Chile, United States</div></div>")
    clicked = _record_clicks(page, monkeypatch)
    assert md._click_row_expander(page, "Locations") is False
    assert "at" not in clicked, clicked


def test_the_expander_skips_remove_and_disabled_controls(page, monkeypatch):
    """Справа у строки сначала «Remove United States» и выключенная стрелка,
    потом настоящая стрелка: жмётся только она."""
    page.set_content(
        "<div style='position:absolute;left:0;top:0;width:600px'>"
        "<div><span>Locations</span>"
        "<div><span>Inclusion:</span><span>Chile, United States</span></div>"
        "<div role='button' aria-expanded='false' aria-label='Remove United States' id='rm'"
        " style='width:24px;height:24px'></div>"
        "<div role='button' aria-expanded='false' aria-disabled='true' id='off'"
        " style='width:24px;height:24px'></div>"
        "<div role='button' aria-expanded='false' id='arrow' style='width:24px;height:24px'></div>"
        "</div></div>")
    clicked = _record_clicks(page, monkeypatch)
    assert md._click_row_expander(page, "Locations") is True
    assert clicked["at"] == "arrow", clicked


# ── последняя страна: Meta выключает и прячет её крестик (63710223de4c) ─────

_DISABLE_LAST = """() => {
  const orig = window.drop;
  window.drop = (c) => {
    orig(c);
    const left = document.querySelectorAll('.grp');
    if (left.length === 1) {
      const x = left[0].querySelector('[role=button]');
      x.outerHTML = '<button disabled style="display:none"><span>Close</span></button>';
    }
  };
}"""


def test_the_last_country_with_a_disabled_hidden_x_is_clean(page):
    """25.09 12:02 GT FX: United States снята, у Guatemala крестик стал
    `<button disabled>` нулевого размера — строки «не читались», и шаг
    отказал при одной нужной стране."""
    page.set_content(_CARD % {"us_click": "onclick=\"drop('United States')\""})
    page.evaluate(_DISABLE_LAST)
    assert md._clear_other_locations(page, "Guatemala") == []
    assert page.evaluate("window.clicked") == ["United States"]


def test_a_disabled_x_is_read_but_never_clicked(page):
    page.set_content(_CARD % {"us_click": "onclick=\"drop('United States')\""})
    page.evaluate("""() => {
      const x = document.querySelector('.grp[data-c="United States"] [role=button]');
      x.setAttribute('aria-disabled', 'true');
      x.setAttribute('onclick', "window.clicked.push('disabled-us')");
    }""")
    rows = md._location_rows(page)
    assert {r["name"]: bool(r.get("disabled")) for r in rows} == {"Guatemala": False, "United States": True}
    left = md._clear_other_locations(page, "Guatemala")
    assert left == ["United States"]
    assert page.evaluate("window.clicked") == []


def test_the_expander_is_taken_after_the_label_not_before_it(page, monkeypatch):
    """PR #314, повторное ревью (A4): стрелка строки — после своей подписи;
    свёрнутый контрол ПЕРЕД подписью принадлежит чему-то выше."""
    page.set_content(
        "<div style='position:absolute;left:0;top:0;width:600px'>"
        "<div><div role='button' aria-expanded='false' id='before' style='width:24px;height:24px'></div>"
        "<span>Locations</span>"
        "<div><span>Inclusion:</span><span>Chile, United States</span></div>"
        "<div role='button' aria-expanded='false' id='after' style='width:24px;height:24px'></div>"
        "</div></div>")
    clicked = _record_clicks(page, monkeypatch)
    assert md._click_row_expander(page, "Locations") is True
    assert clicked["at"] == "after", clicked


# ── карточка на миг пуста после ✕ (GT FX 63710223de4c, GT Day 503b0c077db6) ──

_SINGLE = (Path(__file__).parent / "fixtures" / "ads_manager"
           / "locations_single_country_2026_09_25.html").read_text(encoding="utf-8")


def test_the_live_card_with_one_country_and_a_disabled_close_is_clean(page):
    """Живая карточка 503b0c/01: одна Guatemala, Close выключен, Remove aria-disabled."""
    page.set_content(f"<div style='width:600px'>{_SINGLE}</div>")
    rows = md._location_rows(page)
    assert [r["name"] for r in rows] == ["Guatemala"] and rows[0].get("disabled") is True
    assert md._clear_other_locations(page, "Guatemala") == []


def test_a_card_blank_for_a_moment_after_the_x_is_read_again(page, monkeypatch):
    """После ✕ у United States карточка на полторы секунды пуста (Meta её
    перерисовывает), потом в ней одна Guatemala с выключенным Close.
    Одно чтение в пустой миг давало «строки не прочитаны» (None)."""
    import time as _t

    monkeypatch.setattr(md, "_sleep_h", lambda s, *a, **k: _t.sleep(s))
    page.set_content(_CARD % {"us_click": "onclick=\"redraw()\""})
    page.evaluate("""() => {
      window.redraw = () => {
        window.clicked.push('United States');
        const loc = document.getElementById('loc'); const html = loc.innerHTML;
        loc.innerHTML = '';
        setTimeout(() => {
          loc.innerHTML = html;
          loc.querySelector('.grp[data-c="United States"]').remove();
          loc.querySelector('.grp[data-c="Guatemala"] [role=button]').outerHTML =
            '<button disabled style="display:none"><span>Close</span></button>';
        }, 1250);
      };
    }""")
    assert md._clear_other_locations(page, "Guatemala") == []
    assert page.evaluate("window.clicked") == ["United States"]
