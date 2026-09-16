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
