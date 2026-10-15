# -*- coding: utf-8 -*-
"""«Further limit the reach of your ads» уже выбран — клик, который не
прошёл, шаг не роняет (прогон 40b560c02036, 25.09).

Диалог «Reach your preferred audiences» открылся с уже выбранным вариантом
«Further limit…», клик по подписи бросил исключение (подпись была
перекрыта), исключение проглатывалось, и шаг `further_limit_reach_radio`
отказал жёстко. Теперь после неудачного клика проверяется сам
переключатель. Разметка синтетическая: три варианта с `role=radio`, как в
диалоге Meta, и прозрачный слой поверх — клик Playwright в него упирается.
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

_DIALOG = """
<div role="dialog" style="position:relative;width:600px">
  <div role="heading">Reach your preferred audiences</div>
  <div><div role="radio" aria-checked="%(stay)s"></div><div><span>Stay in current setup</span></div></div>
  <div><div role="radio" aria-checked="%(further)s"></div><div><span>Further limit the reach of your ads</span>
       <div>Set the controls to limit who can see your ads.</div></div></div>
  <div><div role="radio" aria-checked="false"></div><div><span>Switch to original audience options</span></div></div>
  <div role="button">Continue</div>
  <div style="position:absolute;inset:0;z-index:9"></div>
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
    rows = []
    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    monkeypatch.setattr(md._telemetry, "record", lambda step, ok=None, **k: rows.append((step, ok, k)))
    ctx = browser.new_context(viewport={"width": 1200, "height": 800})
    p = ctx.new_page()
    p.rows = rows
    try:
        yield p
    finally:
        ctx.close()


def test_an_already_selected_further_limit_radio_is_success(page):
    page.set_content(_DIALOG % {"stay": "false", "further": "true"})
    assert md._pick_further_limit_radio(page) is True
    step, ok, kw = page.rows[-1]
    assert step == "pick_further_limit_radio" and ok is True
    assert kw["note"] == "already_selected" and kw["diag"]["error"]


def test_a_failed_click_with_another_option_selected_is_a_failure(page):
    page.set_content(_DIALOG % {"stay": "true", "further": "false"})
    assert md._pick_further_limit_radio(page) is False
    assert page.rows[-1][2]["note"] == "click_failed"


def test_a_click_that_goes_through_is_success_without_a_note(page):
    page.set_content((_DIALOG % {"stay": "true", "further": "false"})
                     .replace('<div style="position:absolute;inset:0;z-index:9"></div>', ""))
    assert md._pick_further_limit_radio(page) is True
    assert page.rows == []
