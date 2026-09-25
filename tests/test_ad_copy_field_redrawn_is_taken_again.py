# -*- coding: utf-8 -*-
"""Поле текста, перерисованное Meta между пометкой и захватом, берётся заново;
страница ошибки Facebook после перезагрузки не остаётся под шагом текста.

25.09, второе объявление: SA Investing (3199b2aa80b8, 12:40) и SA Mix
(f7d1cc74e17c, 20:19). Основной текст вставал, а заголовок — нет: поле
находилось (`count` 1), но узел пропадал до захвата (`click_via_cdp_failed`
в обеих попытках) — Meta перерисовывает блок текста после ввода основного
текста, и помеченный узел заменяется новым без метки. Вторая попытка шла
через перезагрузку, а она у SA Mix открыла «Sorry, something went wrong»
(title «Facebook | Error»), и шаг кончился `copy_fields_not_revealed`.

Разметка поля — как в fixtures/ads_manager/ad_copy_2026_09_09.html: блоки
`data-surface=".../message_field"` и `".../headline_field"`, поле — голая
`<textarea>`. Перерисовка смоделирована: первые две пометки заголовка Meta
«съедает», заменяя узел свежим.
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
<!doctype html><html><body style="width:900px">
<div id="creativeEditorComponent">
  <div data-surface="/am/editor_drawer/ad/message_field"><div>Tell people what your ad is about</div>
    <textarea dir="auto" id="body" style="width:500px;height:60px"></textarea></div>
  <div data-surface="/am/editor_drawer/ad/headline_field"><div>Write a short headline</div>
    <textarea dir="auto" id="title" style="width:500px;height:30px"></textarea></div>
</div>
<script>
  window.redraws = 0;
  new MutationObserver((ms) => {
    for (const m of ms) {
      const el = m.target;
      if (m.attributeName !== 'data-rpa-field' || el.getAttribute('data-rpa-field') !== 'title') continue;
      if (window.redraws >= window.redrawLimit) continue;
      window.redraws += 1;
      const fresh = document.createElement('textarea');
      fresh.dir = 'auto'; fresh.id = 'title'; fresh.style.cssText = el.style.cssText;
      el.replaceWith(fresh);
    }
  }).observe(document.body, {attributes: true, subtree: true});
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
    rows = []
    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    monkeypatch.setattr(md._telemetry, "record", lambda step, ok=None, **k: rows.append((step, ok, k)))
    monkeypatch.setattr(md, "_reveal_ad_copy_fields", lambda p: True)
    monkeypatch.setattr(md, "_await_creative_settled", lambda p: True)
    monkeypatch.setattr(md, "disable_text_variations", lambda p: None)
    monkeypatch.setattr(md, "disable_text_generation", lambda p: None)
    monkeypatch.setattr(md, "_cdp_mouse_click", lambda pg, x, y: pg.mouse.click(x, y))
    monkeypatch.setattr(md, "_type_human", lambda p, t: p.keyboard.insert_text(t))
    ctx = browser.new_context(viewport={"width": 1280, "height": 800})
    p = ctx.new_page()
    p.route("**/*", lambda route: route.abort())
    p.rows = rows
    p.set_content(_PAGE)
    try:
        yield p
    finally:
        ctx.close()


def test_a_headline_redrawn_twice_is_still_filled(page):
    page.evaluate("window.redrawLimit = 2")
    report: dict = {}
    assert md.fill_ad_copy(page, "Short headline", "Body of the ad", report) is True, report
    assert page.evaluate("window.redraws") == 2
    assert page.locator("#title").input_value() == "Short headline"
    assert page.locator("#body").input_value() == "Body of the ad"


def test_a_headline_that_never_settles_is_reported_with_its_tries(page):
    page.evaluate("window.redrawLimit = 1000")
    report: dict = {}
    assert md.fill_ad_copy(page, "Short headline", "Body of the ad", report) is False
    stages = [a for a in report["attempts"] if a.get("field") == "headline"]
    assert stages and all(a["tries"] == 3 for a in stages), stages


# ── страница ошибки Facebook после перезагрузки ───────────────────────────

_FB_ERROR = """<!doctype html><html><head><title>Facebook | Error</title></head><body>
<div><h2>Sorry, something went wrong.</h2><p>We're working on it and we'll get it fixed as soon as we can.</p>
<a href="#">Go Back</a></div></body></html>"""


def test_the_error_page_after_the_reveal_reload_is_left(page, monkeypatch):
    page.set_content(_FB_ERROR)
    assert md._facebook_error_page(page) is True
    moves = []

    def goto(url, **kw):
        moves.append(("goto", url))
        page.set_content(_PAGE)

    monkeypatch.setattr(page, "goto", goto)
    monkeypatch.setattr(type(page), "url", property(lambda self: "https://adsmanager.example/ads/edit?x=1&y=true#"))
    assert md._recover_facebook_error_page(page, "test") is True
    assert moves == [("goto", "https://adsmanager.example/ads/edit?x=1&y=true")]
    assert ("facebook_error_page", True) in [r[:2] for r in page.rows]


def test_a_normal_editor_is_not_an_error_page(page):
    assert md._facebook_error_page(page) is False
    assert md._recover_facebook_error_page(page, "test") is False
    assert not [r for r in page.rows if r[0] == "facebook_error_page"]


def test_the_reveal_reload_is_followed_by_the_error_page_check():
    import inspect
    src = inspect.getsource(md._reveal_ad_copy_fields)
    assert src.index("_recover_facebook_error_page(page") > src.index("page.reload(")
