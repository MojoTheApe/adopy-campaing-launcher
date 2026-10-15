# -*- coding: utf-8 -*-
"""Возраст читается внутри своего поля — и на живой раскладке 24.09 (порт ACL-1284).

Порт живого робота на main (ACL-1284) принёс чтение возраста из ACL-1250:
число подтверждает возраст, только если оно стоит в поле Age, а не рядом
(«35» бюджета не должно подтверждать 35). Но в живой раскладке 24.09
(снимок d3e690189a6e/01_advantage_plus_control) подписей «Age» две —
заголовок строки-кнопки и <label> самого поля, — а между подписью и
значениями стоят <label> «Minimum age» / «Maximum age». Чтение ACL-1250 в
таком виде давало пусто, и после снятия живого обхода read_back_by_label
шаг возраста отказывал бы в каждом прогоне (живая телеметрия 19–25.09:
162 из 162 возрастов подтверждены только этим обходом).

Проверяется на вырезке живой страницы в офлайн-Chrome.
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

_AGE = (ROOT / "tests" / "fixtures" / "ads_manager" / "audience_age_open_2026_09_24.html").read_text(encoding="utf-8")
_BUDGET = "<section><div><span>Daily budget</span></div><div><span>35</span></div></section>"


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
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    ctx.route("**/*", lambda r: r.abort())
    p = ctx.new_page()
    try:
        yield p
    finally:
        ctx.close()


def test_the_live_age_field_is_read(page):
    page.set_content(_AGE)
    assert md._age_triggers_read(page) == ["18", "65+"]
    assert md._age_shows_value(page, "18") is True
    assert md._age_shows_value(page, "35") is False


def test_a_budget_next_to_the_live_age_field_does_not_confirm_the_age(page, monkeypatch):
    """На экране 18 – 65+, рядом бюджет 35: возраст 35 не подтверждён."""
    page.set_content(_AGE + _BUDGET)
    assert md._age_triggers_read(page) == ["18", "65+"]
    monkeypatch.setattr(md, "_ensure_manual_audience", lambda p: False)
    monkeypatch.setattr(md, "_cancel_dynamic_creative_dialog", lambda p: False)
    monkeypatch.setattr(md, "_fill_audience_age_controls", lambda *a: True)
    assert md.fill_audience_age(page, 35, 65) is False


def test_the_wanted_age_on_the_live_field_is_confirmed(page, monkeypatch):
    page.set_content(_AGE.replace(">18<", ">35<") + _BUDGET)
    monkeypatch.setattr(md, "_ensure_manual_audience", lambda p: False)
    monkeypatch.setattr(md, "_cancel_dynamic_creative_dialog", lambda p: False)
    monkeypatch.setattr(md, "_fill_audience_age_controls", lambda *a: True)
    assert md.fill_audience_age(page, 35, 65) is True


def test_two_age_fields_with_numbers_give_no_answer(page):
    """Два поля Age с числами — неизвестно, какое из них наше."""
    page.set_content(_AGE + "<section><div><span>Age</span></div><div><span>25</span><span>40</span></div></section>")
    assert md._age_triggers_read(page) == []
