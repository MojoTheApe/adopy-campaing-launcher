# -*- coding: utf-8 -*-
"""Текст объявления: сломанный блок лечится перезагрузкой, окно Site links — Cancel (ACL-1313).

* d3e690189a6e/07_ad2_copy: «Error: component failed to load» на месте блока
  редактора — повтор текста шёл в тот же пустой блок. Теперь перед повтором
  перезагрузка (только при «All edits saved», через `recover_stuck_page`) и
  повторное открытие объявления (`_back_on_the_ad`).
* 21.09, ad2_copy: поверх объявления окно «Creative setup → Extensions / Site
  links» с Cancel и Save. `_close_creative_setup_modal` его не узнавал, а обход
  мастера нажал бы Save. Теперь — только Cancel. Разметки окна в снимках нет,
  она синтетическая по описанию: заголовок, разделы, поле, Cancel/Save.
* Шаг мастера «Image generation» теперь называется «Creative generation».
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
from src.fb_ads_rpa import run_telemetry  # noqa: E402

SITE_LINKS = """
<div id="sitelinks" role="dialog" style="position:fixed; top:80px; left:300px; width:700px; height:500px; background:#fff">
  <div><span>Creative setup</span></div>
  <div><div role="tab"><span>Extensions</span></div></div>
  <div><span>Site links</span></div>
  <div><span>Add links to your ad so people can visit specific pages of your website.</span></div>
  <input placeholder="Enter URL">
  <div style="position:absolute; bottom:10px; right:10px">
    <div role="button" tabindex="0" style="display:inline-block; width:70px; height:30px">Cancel</div>
    <div role="button" tabindex="0" style="display:inline-block; width:70px; height:30px">Save</div>
  </div>
</div>
"""

_SPY = """
() => {
  window.clicked = [];
  document.addEventListener('click', (e) => {
    const b = e.target.closest('[role=button], button');
    if (!b) return;
    window.clicked.push(b.textContent.trim());
    const d = b.closest('#sitelinks');
    if (d) d.remove();
  }, true);
}
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
def page(browser, monkeypatch, tmp_path):
    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    monkeypatch.setattr(run_telemetry, "TELEMETRY_PATH", tmp_path / "telemetry.jsonl")
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    p = ctx.new_page()
    p.route("**/*", lambda route: route.abort())
    try:
        yield p
    finally:
        ctx.close()


def test_the_site_links_modal_is_cancelled_never_saved(page):
    page.set_content("<div><span>Ad setup</span></div>" + SITE_LINKS)
    page.evaluate(_SPY)
    assert md._close_creative_setup_modal(page) is True
    assert page.locator("#sitelinks").count() == 0
    assert page.evaluate("window.clicked") == ["Cancel"]


def test_the_creative_wizard_is_not_mistaken_for_the_site_links_modal(page):
    """В мастере есть Next — это не окно Site links, Cancel в нём не жмём."""
    page.set_content(
        '<div id="wizard"><span>Creative setup</span> <span>Media</span> <span>Text</span>'
        '<span>Site links</span><div role="button">Cancel</div><div role="button">Save</div>'
        '<div role="button">Next</div></div>')
    page.evaluate(_SPY)
    assert md._cancel_site_links_modal(page) is False
    assert page.evaluate("window.clicked") == []


def test_creative_generation_is_a_wizard_step(page):
    page.set_content('<div><span>Creative setup</span> <span>Media</span> '
                     '<span>Creative generation</span><div role="button">Next</div></div>')
    assert md._creative_wizard_open(page) is True


# ── component failed to load ──────────────────────────────────────────────

@pytest.fixture
def copy(monkeypatch):
    from src.fb_ads_rpa import browser as br

    ev: list = []
    state = {"component": True, "fills": [False, True], "reload": True}
    monkeypatch.setattr(br, "ensure_editor_window", lambda p, section: True)
    monkeypatch.setattr(md, "_review_errors_visible", lambda p: False)
    monkeypatch.setattr(md, "_await_creative_settled", lambda p: True)
    monkeypatch.setattr(md, "_component_failed", lambda p: state["component"])

    def fill(p, t, b, report):
        ev.append("fill")
        ok = state["fills"].pop(0)
        if not ok:
            report["why"] = "typed_text_mismatch"
            state["component"] = True
        return ok

    def recover(p, where, **kw):
        ev.append(("recover", where, kw.get("editor")))
        if state["reload"]:
            state["component"] = False
        return state["reload"]

    monkeypatch.setattr(md, "fill_ad_copy", fill)
    monkeypatch.setattr(md, "recover_stuck_page", recover)
    monkeypatch.setattr(md, "_back_on_the_ad", lambda p, name, report: ev.append(("reopen", name)))
    monkeypatch.setattr(md._telemetry, "record", lambda *a, **k: None)
    return ev, state


def test_a_failed_component_is_reloaded_and_the_ad_reopened_before_the_retry(copy):
    ev, state = copy
    state["component"] = False  # до первой попытки блок цел
    report: dict = {}
    assert md.write_ad_copy(object(), "T", "B", ad_name="Ad 2", report=report) is True
    assert ev == ["fill", ("recover", "ad_copy_retry", True), ("reopen", "Ad 2"), "fill"]
    assert report["component_failed"] == {"where": "ad_copy_retry", "reloaded": True}


def test_a_failed_component_before_the_first_try_is_healed_first(copy):
    ev, state = copy
    state["fills"][:] = [True]
    assert md.write_ad_copy(object(), "T", "B", ad_name="Ad 2", report={}) is True
    assert ev[:2] == [("recover", "ad_copy_before", True), ("reopen", "Ad 2")]


def test_unsaved_edits_are_not_reloaded_and_the_ad_is_not_reopened(copy):
    ev, state = copy
    state["component"] = False
    state["reload"] = False
    md.write_ad_copy(object(), "T", "B", ad_name="Ad 2", report={})
    assert ("recover", "ad_copy_retry", True) in ev
    assert not any(e[0] == "reopen" for e in ev if isinstance(e, tuple))
