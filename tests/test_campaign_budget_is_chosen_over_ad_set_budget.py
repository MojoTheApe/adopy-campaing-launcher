# -*- coding: utf-8 -*-
"""Бюджет кампанийный, даже если Meta открыла черновик с «Ad set budget» (17.09).

Прогон 71f6bddbcf7c (S15, SV_ait Day): радио стояло на «Ad set budget», поля
бюджета кампании не было, `daily_budget` упал через 133 с.
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

_PAGE = """
<!doctype html><html><body>
<div>Budget strategy</div>
<label><input type="radio" name="b" id="camp" %(camp)s onchange="show()"><div><span>Campaign budget</span></div></label>
<label><input type="radio" name="b" id="adset" %(adset)s onchange="show()"><div><span>Ad set budget</span></div></label>
<div id="amount" style="display:%(amount)s"><input placeholder="Please enter amount"></div>
<script>function show(){document.getElementById('amount').style.display=document.getElementById('camp').checked?'block':'none';}</script>
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
    monkeypatch.setattr(md._telemetry, "record", lambda *a, **k: None)
    ctx = browser.new_context()
    p = ctx.new_page()
    try:
        yield p
    finally:
        ctx.close()


def test_ad_set_budget_is_switched_to_campaign_budget(page):
    page.set_content(_PAGE % {"camp": "", "adset": "checked", "amount": "none"})
    assert md._ensure_campaign_budget(page) is True
    assert page.is_checked("#camp")
    assert page.is_visible("input[placeholder='Please enter amount']")


def test_campaign_budget_already_chosen_is_left_alone(page):
    page.set_content(_PAGE % {"camp": "checked", "adset": "", "amount": "block"})
    assert md._ensure_campaign_budget(page) is False
    assert page.is_checked("#camp")


def test_a_page_without_the_strategy_block_is_untouched(page):
    page.set_content("<input placeholder='Please enter amount'>")
    assert md._ensure_campaign_budget(page) is False


# ── лишний «New Leads ad» после Create ad (Doc20, 17.09 00:02) ──────────────

def test_new_ad_shells_are_counted_only_in_the_left_tree(page):
    page.set_viewport_size({"width": 1500, "height": 800})
    page.set_content(
        "<div style='position:absolute;left:10px;top:10px'><div>New Leads ad</div><div>2221 - AR Trolley partner</div>"
        "<div>New Leads ad</div></div><div style='position:absolute;left:900px;top:10px'><span>New Leads ad</span></div>"
    )
    assert md._tree_new_ad_shells(page) == 2


# ── недоступный подпункт площадок: состояние по aria-checked (Doc20, 17.09) ──

def test_a_locked_subitem_is_read_by_aria_checked(page):
    page.set_content(
        "<div data-rpa-placements-marker='1'><ul>"
        "<li><span>Ads on Facebook Reels</span><input type='checkbox' aria-checked='false' aria-disabled='true' checked disabled></li>"
        "</ul></div>"
    )
    res = page.evaluate(md.PLACEMENTS_SUBITEM_JS, ["Ads on Facebook Reels", False])
    assert res["ok"] is True and res["checked"] is False


# ── заготовка объявления ищется без учёта регистра (Doc20: «New Leads ad») ──

def test_the_new_ad_shell_is_found_whatever_the_case(page, monkeypatch):
    clicks = []
    monkeypatch.setattr(md, "_cdp_mouse_click", lambda p, x, y: clicks.append((x, y)))
    monkeypatch.setattr(md, "wait_editor_saved", lambda *a, **k: None)
    page.set_viewport_size({"width": 1400, "height": 800})
    page.set_content("<div style='position:absolute;left:20px;top:40px'><span>New Leads ad</span></div>")
    assert md._open_tree_ad(page, "New Leads Ad") is True
    assert len(clicks) == 1


# ── «Further limit» применился без окна Continue (S15/Doc21, 17.09) ─────────

def test_manual_audience_without_the_continue_dialog_is_recognised(page):
    page.set_content("<div>Age</div><div role='button'>Switch to recommended setup</div>")
    assert md._audience_already_manual(page) is True
    page.set_content("<div role='dialog'>Further limit the reach of your ads <div role='button'>Continue</div></div>"
                     "<div role='button'>Switch to recommended setup</div>")
    assert md._audience_already_manual(page) is False
    page.set_content("<div>Use recommended setup</div>")
    assert md._audience_already_manual(page) is False


# ── язык, который уже стоит, — успех (S15, 17.09 06:56) ─────────────────────

def test_a_language_already_selected_counts(page):
    page.set_content(
        "<div><div>Languages</div><div>Only enter a language if you need to limit your audience.</div>"
        "<div><span>Spanish (All)</span><div role='button'>x</div></div>"
        "<input placeholder='Search languages'></div>"
    )
    assert "Spanish (All)" in md._languages_selected(page)
    assert md.fill_audience_languages(page, ["Spanish"]) is True


def test_the_language_step_never_presses_escape(page, monkeypatch):
    """Escape в редакторе Ads Manager закрывает редактор (17.09 13:19)."""
    pressed = []
    orig = page.keyboard.press
    monkeypatch.setattr(page.keyboard, "press", lambda k, *a, **kw: (pressed.append(k), orig(k, *a, **kw))[1])
    page.set_content("<div><div>Languages</div><span>Spanish (All)</span><input placeholder='Search languages'></div>")
    assert md.fill_audience_languages(page, ["Spanish"]) is True
    assert "Escape" not in pressed


def test_an_age_boundary_already_at_its_value_is_read_from_the_closed_dropdown(page):
    page.set_content("<div><div>Age</div><div><div>35</div><div>65+</div></div>"
                     "<div role='listbox'><div>64</div></div></div>")
    assert md._age_boundary_shows(page, "65+") is True
    assert md._age_boundary_shows(page, "64") is False


def test_the_start_time_hour_18_is_not_taken_for_the_minimum_age(page):
    page.set_content(
        "<div>Start date</div><div role='group'><span>18</span>:<span>20</span> GMT+3</div>"
        "<div style='height:300px'></div>"
        "<div>Age</div><div><div id='min'>18<i></i></div><div id='max'>65+</div></div>"
    )
    assert md._mark_age_trigger(page, "18") is True
    assert page.locator("[data-acl-age-trigger]").get_attribute("id") == "min"
    assert md._mark_age_trigger(page, "65+") is True
    assert page.locator("[data-acl-age-trigger]").count() == 1
    assert page.locator("[data-acl-age-trigger]").get_attribute("id") == "max"


def test_no_age_field_marks_nothing(page):
    page.set_content("<div>Start time</div><span>18</span>")
    assert md._mark_age_trigger(page, "18") is False


def test_an_age_label_inside_a_closing_dialog_is_ignored(page):
    page.set_content(
        "<div>Age</div><div><div id='min'>18</div><div id='max'>65+</div></div>"
        "<div role='dialog' style='position:fixed;top:300px'>Further limit the reach <span>Age</span></div>"
    )
    assert md._mark_age_trigger(page, "18") is True
    assert page.locator("[data-acl-age-trigger]").get_attribute("id") == "min"


def test_an_unchecked_subitem_with_a_stale_checked_property_is_not_clicked_again(page):
    """17.09 20:00 CR_ait FX_Y: aria-checked="false", а свойство checked ещё true."""
    page.set_content(
        "<div data-rpa-placements-marker='1'><ul>"
        "<li><span>Messenger Stories</span><input id='cb' type='checkbox' aria-checked='false' checked></li>"
        "</ul></div>"
        "<script>window.clicks=0; document.getElementById('cb').addEventListener('click',()=>window.clicks++)</script>"
    )
    res = page.evaluate(md.PLACEMENTS_SUBITEM_JS, ["Messenger Stories", False])
    assert res["ok"] is True and res["checked"] is False
    assert page.evaluate("window.clicks") == 0


def test_a_subitem_state_is_read_without_clicking(page):
    page.set_content(
        "<div data-rpa-placements-marker='1'><ul>"
        "<li><span>Threads feed</span><input id='cb' type='checkbox' aria-checked='false' checked disabled></li>"
        "</ul></div>"
        "<script>window.clicks=0; document.getElementById('cb').addEventListener('click',()=>window.clicks++)</script>"
    )
    assert page.evaluate(md.PLACEMENTS_SUBITEM_READ_JS, "Threads feed") == {"found": True, "checked": False}
    assert page.evaluate("window.clicks") == 0
    assert page.evaluate(md.PLACEMENTS_SUBITEM_READ_JS, "Facebook Feed")["found"] is False


def test_a_subitem_is_read_whatever_the_case(page):
    """17.09 CO_bnc FX: в списке «Facebook Notifications», на странице «Facebook notifications»."""
    page.set_content(
        "<div data-rpa-placements-marker='1'><ul>"
        "<li><span>Facebook notifications</span><input type='checkbox' aria-checked='false'></li>"
        "</ul></div>"
    )
    assert page.evaluate(md.PLACEMENTS_SUBITEM_READ_JS, "Facebook Notifications") == {
        "found": True, "checked": False}


def test_a_collapsed_languages_row_is_opened_by_its_pencil(page, monkeypatch):
    """17.09 23:04 CR_ait Day: блок аудитории сводкой, поля языков нет — жмём карандаш."""
    page.set_content(
        "<div style='position:absolute;left:0;top:0'>"
        "<div><span>Languages</span><span>All languages</span>"
        "<div role='button' aria-label='Edit' id='pen' style='width:24px;height:24px'"
        " onclick=\"window.opened=true\"></div></div></div>"
    )
    clicks = []
    monkeypatch.setattr(md, "_cdp_mouse_click",
                        lambda pg, x, y: clicks.append((round(x), round(y))) or
                        pg.eval_on_selector("#pen", "el => el.click()"))
    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    assert md._click_languages_edit(page) is True
    assert page.evaluate("window.opened") is True
    assert clicks, "клик должен был уйти по координатам карандаша"


def test_without_a_pencil_the_languages_row_is_opened_by_its_value(page, monkeypatch):
    """18.09 08:23: в этой отрисовке кнопок у строки нет — жмём сам текст."""
    page.set_content(
        "<div style='position:absolute;left:0;top:0'>"
        "<div><div>Languages</div><div id='val'>All languages</div></div></div>"
    )
    clicked = {}
    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    monkeypatch.setattr(md, "_cdp_mouse_click",
                        lambda pg, x, y: clicked.update(at=pg.evaluate(
                            "([x,y]) => (document.elementFromPoint(x,y)||{}).id", [x, y])))
    assert md._click_languages_edit(page) is True
    assert clicked["at"] == "val", clicked


def test_no_languages_row_no_click(page, monkeypatch):
    page.set_content("<div>Budget</div>")
    monkeypatch.setattr(md, "_cdp_mouse_click", lambda *a: pytest.fail("лишний клик"))
    assert md._click_languages_edit(page) is False
