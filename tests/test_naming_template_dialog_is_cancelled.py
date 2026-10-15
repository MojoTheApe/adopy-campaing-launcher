# -*- coding: utf-8 -*-
"""Диалог шаблона имени ad set закрывается кнопкой Cancel (прогон 8dbafcecb31f, 25.09).

GT_ait_All_Video_Day: после имени ad set поток кликает по подписи «Ad set
name», чтобы снять фокус с поля, — клик открыл диалог «Ad set name» с
шаблоном имени (Add component / Choose existing template, Cancel / Save).
Диалог остался поверх редактора, и все восемь способов выбрать Website
видели `menu_closed`. Разметки диалога в снимке нет (rpa_pages обрезан на
4 МБ, портал диалога в конце body) — она синтетическая, по скриншоту
rpa_screenshots/8dbafcecb31f/20260925_123055_recorded_fail_conversion_website.png.
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
from tests.test_create_quick_retry_returns_to_list import flow  # noqa: E402,F401

_EDITOR = """
<div data-surface="/am/editor_drawer"><div aria-label="Close" role="button" style="width:20px;height:20px"
  onclick="window.clicked.push('editor-close')"></div>
  <div><span>Ad set name</span><input value="GT_Example_Day"></div>
  <div><span>Conversion location</span><div role="button" onclick="window.clicked.push('conversion')">Website and instant forms</div></div>
</div>
"""

_NAMING = """
<div role="dialog" id="naming" style="position:fixed;top:30px;left:450px;width:420px;background:#fff">
  <div><span>Ad set name</span><div aria-label="Close" role="button" style="width:16px;height:16px"
    onclick="window.clicked.push('x')"></div></div>
  <div>Name your ad sets consistently by creating a template based on your naming convention.
    Ad set names will update automatically to match your current ad set settings.</div>
  <div>Template</div>
  <div><div role="button" onclick="window.clicked.push('add')">Add component</div>
    <a onclick="window.clicked.push('choose')">Choose existing template</a></div>
  <div><div role="button" onclick="window.clicked.push('cancel'); document.getElementById('naming').remove()">Cancel</div>
    <div role="button" aria-disabled="true" onclick="window.clicked.push('save')">Save</div></div>
</div>
"""

_OTHER = """
<div role="dialog" style="position:fixed;top:30px;left:450px;width:420px;background:#fff">
  <div><span>Discard drafts?</span></div><div>Any changes will be discarded.</div>
  <div><div role="button" onclick="window.clicked.push('cancel')">Cancel</div>
    <div role="button" onclick="window.clicked.push('discard')">Save</div></div>
</div>
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
    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    monkeypatch.setattr(md._telemetry, "record", lambda *a, **k: None)
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    p = ctx.new_page()
    p.route("**/*", lambda route: route.abort())
    try:
        yield p
    finally:
        ctx.close()


def _set(page, html):
    page.set_content(html + "<script>window.clicked = [];</script>")


def test_the_naming_template_dialog_is_cancelled_never_saved(page):
    _set(page, _EDITOR + _NAMING)
    assert md._cancel_naming_template_dialog(page) is True
    assert page.evaluate("window.clicked") == ["cancel"]
    assert page.locator("#naming").count() == 0


def test_another_dialog_with_cancel_is_left_alone(page):
    _set(page, _EDITOR + _OTHER)
    assert md._cancel_naming_template_dialog(page) is False
    assert page.evaluate("window.clicked") == []


def test_overlays_in_the_editor_clear_the_naming_dialog(page):
    _set(page, _EDITOR + _NAMING)
    closed = md._clear_blocking_overlays(page, "adset")
    assert "naming_template_dialog" in closed
    assert page.evaluate("window.clicked") == ["cancel"]


def test_the_flow_cancels_it_before_opening_the_conversion_menu(flow, monkeypatch):
    order: list[str] = []
    monkeypatch.setattr(md, "_cancel_naming_template_dialog", lambda p: order.append("cancel") or False)
    monkeypatch.setattr(md, "click_conversion_location_dropdown",
                        lambda p: order.append("conversion_menu") or True)
    flow["run"]()
    assert "conversion_menu" in order
    assert order.index("cancel") < order.index("conversion_menu")
