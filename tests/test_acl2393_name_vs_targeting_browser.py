"""ACL-2393: имя кампании называет один пресет таргетинга, выбран другой.

Мутация, на которой тест обязан падать: в `cbUpdateName` убрать вызов
`nameTargetingCheck` (строка не появится) или в `cbPublish` убрать блок
`nameDiffers` (подтверждение промолчит). Чистые правила — builder_layout_harness.cjs.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_builder_per_account import _render  # noqa: E402
from test_drawer_review_browser import add  # noqa: E402

LIB = [
    {"code": "Mix2", "label": "Mix 2", "fb_interest": "Forex"},
    {"code": "Green", "label": "Green", "fb_interest": "Green interest"},
    {"code": "Day", "label": "Day", "fb_interest": "Day trading"},
]
SLOT = ".cbd-host .cb-slot:not([hidden])"
OVERRIDE = SLOT + " .cb-name-override"
CHECK = SLOT + " [data-name-check]"


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=os.environ.get("ACL_BROWSER_EXECUTABLE") or None)
        yield b
        b.close()


@pytest.fixture
def page(browser):
    ctx = browser.new_context()
    p = ctx.new_page()
    p.set_default_timeout(5000)
    errors: list[str] = []
    p.on("pageerror", lambda e: errors.append(str(e)))
    html = _render({"Demo": []})
    assert "REF = {};" in html
    html = html.replace("REF = {};", "REF = " + json.dumps({"targetings": LIB}) + ";", 1)

    def route(r):
        if r.request.resource_type == "document":
            r.fulfill(content_type="text/html", body=html); return
        r.fulfill(json={"files": [], "texts": [], "rows": [], "profiles": [], "waiting": False})

    p.route("**/*", route)
    p.goto("http://acl.test/")
    p.evaluate("cbDrawer.open()")
    add(p)
    p.locator(".cbd-camp-row").first.click()
    yield p
    assert not errors, errors
    ctx.close()


def _pick(p, code):
    p.evaluate("(c) => cbTargetingPick(document.querySelector('.cbd-host .cb-slot:not([hidden]) .cb-targeting-wrap'), c, '')", code)


def _shown(p):
    return p.locator(CHECK).is_visible()


def test_the_line_follows_paste_pick_and_clear(page):
    _pick(page, "Mix2")
    assert not _shown(page), "авто-имя не может расходиться с таргетингом"
    page.fill(OVERRIDE, "KW_TRL_All_Image_Green_35_04.11")   # вставка = input
    assert _shown(page)
    assert page.locator(CHECK + " .cb-name-check-text").text_content() == \
        "⚠ Name says Green, targeting is Mix2 – fix one of them"
    _pick(page, "Green")
    assert not _shown(page)
    page.evaluate("""() => {
      const s = document.querySelector('.cbd-host .cb-slot:not([hidden])');
      s.querySelector('.cb-targeting-search').value = '';
      s.querySelector('.cb-targeting-hidden').value = '';
      cbUpdateName(s);
    }""")
    assert "no targeting is selected" in page.locator(CHECK + " .cb-name-check-text").text_content()


def test_use_button_picks_the_preset_and_keeps_the_name(page):
    _pick(page, "Mix2")
    page.fill(OVERRIDE, "KW_Green_35")
    btn = page.locator(CHECK + " .cb-name-fix")
    assert btn.is_visible() and btn.inner_text() == "Use Green"
    btn.click()
    assert page.eval_on_selector(SLOT + " .cb-targeting-hidden", "e => e.value") == "Green"
    assert page.eval_on_selector(SLOT + " .cb-targeting-search", "e => e.value") == "Green"
    assert page.input_value(OVERRIDE) == "KW_Green_35"
    assert not _shown(page)


def test_a_duplicate_carries_the_line_and_is_independent(page):
    _pick(page, "Mix2")
    page.fill(OVERRIDE, "KW_Green_35")
    page.locator(SLOT + " .cb-lvl-campaign .cb-dup-slot-btn").click()
    page.wait_for_function("document.querySelectorAll('.cbd-host .cb-slot').length >= 2")
    flags = page.evaluate("() => Array.from(document.querySelectorAll('.cbd-host .cb-slot')).map(s => !!(s._cbNameCheck && s._cbNameCheck.text))")
    assert flags.count(True) == 2, flags
    _pick(page, "Green")
    flags = page.evaluate("() => Array.from(document.querySelectorAll('.cbd-host .cb-slot')).map(s => !!(s._cbNameCheck && s._cbNameCheck.text))")
    assert sorted(flags) == [False, True], flags


def test_publish_confirm_warns_but_does_not_block(page):
    _pick(page, "Mix2")
    page.fill(OVERRIDE, "KW_Green_35")
    page.evaluate("""() => {
      window.__asked = null;
      window._safeConfirm = function (title, body, ok, opts) { window.__asked = { body: body, label: opts && opts.confirmLabel }; };
      window.cbPreflightFetch = undefined;
      cbPublish(document.querySelector('.cbd-host form.open-account-form') || document.querySelector('form.open-account-form'));
    }""")
    page.wait_for_function("window.__asked")
    asked = page.evaluate("window.__asked")
    assert "name says Green, targeting is Mix2".lower() in asked["body"].lower()
    assert "the campaign goes out with Mix2" in asked["body"]
    assert asked["label"] == "Build anyway"
