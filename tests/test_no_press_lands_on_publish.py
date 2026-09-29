# -*- coding: utf-8 -*-
"""Ни одно нажатие робота не попадает в Publish (ACL-1327, ревью PR #340 v4).

Живой прогон 97314905598e (28.09, новый редактор simple_create): кнопка
«Set up creative» стояла под футером редактора, `_cdp_mouse_click` записал
`click_target_covered` — и всё равно нажал: точка x=1606, y=913 лежала в
полосе футера, где Publish. На этот раз мимо кнопки, но ничто этого не
гарантировало.

Охрана `_press_allowed` — непосредственно перед нажатием, после пути курсора:

* в любом редакторе: в точке Publish / «Review and publish» (кнопка или
  что-то внутри неё), элемент `syd_simple_create_publish` или футер нового
  редактора — нажатия нет;
* в новом редакторе: цель, помеченная `_click_xpath` / `_click_trusted`,
  накрыта чем-то другим — нажатия нет; в прежнем — как раньше.

Футер — живой фрагмент `tests/fixtures/ads_manager/editor_footer_simple_create_2026_09_28.html`.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import meta_dom as md  # noqa: E402

FOOTER = (ROOT / "tests" / "fixtures" / "ads_manager"
          / "editor_footer_simple_create_2026_09_28.html").read_text(encoding="utf-8")

SIMPLE_URL = ("https://adsmanager.facebook.com/adsmanager/manage/simple_create"
              "?act=1&selected_campaign_ids=2&selected_adset_ids=3&selected_ad_ids=4")
CLASSIC_URL = "https://adsmanager.facebook.com/adsmanager/manage/ads/edit/standalone?act=1"

TRIGGER = ("//*[@role='button' or self::button]"
           "[contains(normalize-space(.), 'Set up creative')]")
ITEM = "//*[@role='menuitem' or @role='option'][contains(normalize-space(.), 'Video ad')]"

# Цель внизу окна; поверх неё — полоса во всю ширину (COVER). Любое
# нажатие в полосу считается: window.coverClicks, а по Publish — publishClicks.
_PAGE = """
<!doctype html><html><body style="margin:0">
<div style="height:700px">Ad</div>
<div id="trigger" role="button" tabindex="0" aria-haspopup="menu"
     style="position:absolute; left:500px; top:740px; width:200px; height:30px">Set up creative</div>
<div id="cover" style="position:fixed; left:0; right:0; bottom:0; height:100px; background:#eee">COVER</div>
<script>
  window.coverClicks = 0; window.publishClicks = 0; window.triggerClicks = 0;
  document.getElementById('cover').addEventListener('click', (e) => {
    window.coverClicks++;
    if (e.target.closest('[data-surface*="syd_simple_create_publish"], .pub')) window.publishClicks++;
  });
  document.getElementById('trigger').addEventListener('click', () => window.triggerClicks++);
</script>
</body></html>
"""

# Полоса — живой футер нового редактора: строка «By clicking Publish…»
# во всю ширину над целью, Close и Publish справа.
LIVE_FOOTER = ("<div style='position:absolute; left:0; right:0; top:0; height:60px'>" + FOOTER + "</div>")
# Publish прежнего мастера (без surface нового редактора) — во всю полосу.
CLASSIC_PUBLISH = "<div class='pub' role='button' style='width:100%; height:100px'><span>Publish</span></div>"
REVIEW_PUBLISH = "<div class='pub' role='button' style='width:100%; height:100px'><span>Review and publish (2)</span></div>"
# Элемент Publish нового редактора, чья надпись — не слово «Publish», в
# длинной обёртке: ловит только правило по `data-surface`.
SURFACE_ONLY = ("<div style='height:100px'>" + ("Long editor text. " * 40)
                + "<div class='pub' role='button' data-surface='/am/lib:syd_simple_create_publish'"
                " style='position:absolute; left:0; right:0; top:0; height:100px'><span>Go live</span></div></div>")
# Обычная накладка (подсказка, тост) — не Publish.
PLAIN = "<div style='width:100%; height:100px'>tooltip</div>"


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
    from src.fb_ads_rpa import page_capture

    monkeypatch.setattr(md, "_sleep_h", lambda s, *a, **k: time.sleep(min(float(s), 0.2)))
    monkeypatch.setattr(md, "_human_jitter", lambda base, spread: 0.0)
    monkeypatch.setattr(md._telemetry, "record", lambda *a, **k: None)
    monkeypatch.setattr(page_capture, "capture", lambda *a, **k: None)
    ctx = browser.new_context(viewport={"width": 1280, "height": 800})
    p = ctx.new_page()
    try:
        yield p
    finally:
        ctx.close()


def _open(page, url: str, cover: str) -> None:
    html = _PAGE.replace("COVER", cover)
    page.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html", body=html)
               if route.request.url.startswith("https://adsmanager.facebook.com/") else route.abort())
    page.goto(url)


def _centre(page) -> tuple[float, float]:
    b = page.locator("#trigger").bounding_box()
    return b["x"] + b["width"] / 2, b["y"] + b["height"] / 2


def _clicks(page) -> dict:
    return page.evaluate("({cover: window.coverClicks, publish: window.publishClicks,"
                         " trigger: window.triggerClicks})")


@pytest.mark.parametrize("url", [SIMPLE_URL, CLASSIC_URL])
def test_the_live_simple_create_footer_is_never_pressed(page, url):
    _open(page, url, LIVE_FOOTER)
    assert md._cdp_mouse_click(page, *_centre(page)) is False
    assert _clicks(page)["cover"] == 0


@pytest.mark.parametrize("cover", [CLASSIC_PUBLISH, REVIEW_PUBLISH])
@pytest.mark.parametrize("url", [SIMPLE_URL, CLASSIC_URL])
def test_a_publish_button_under_the_point_is_never_pressed(page, url, cover):
    _open(page, url, cover)
    assert md._cdp_mouse_click(page, *_centre(page)) is False
    assert _clicks(page)["publish"] == 0


def test_the_publish_surface_is_never_pressed_whatever_its_label(page):
    _open(page, CLASSIC_URL, SURFACE_ONLY)
    assert md._cdp_mouse_click(page, *_centre(page)) is False
    assert _clicks(page)["publish"] == 0


def test_the_playwright_fallback_is_guarded_too(page, monkeypatch):
    """CDP-сессия не открылась — нажатие через Playwright, та же охрана."""
    def no_session(p):
        raise RuntimeError("no cdp")

    monkeypatch.setattr(md, "_cdp_input_session", no_session)
    _open(page, CLASSIC_URL, CLASSIC_PUBLISH)
    assert md._cdp_mouse_click(page, *_centre(page)) is False
    assert _clicks(page)["publish"] == 0


def test_a_covered_target_is_not_pressed_in_the_single_screen_editor(page):
    _open(page, SIMPLE_URL, PLAIN)
    assert md._click_xpath(page, "//*[@id='trigger']", scroll=False) is False
    assert _clicks(page)["cover"] == 0


def test_a_covered_handle_is_not_pressed_in_the_single_screen_editor(page):
    """Запасной путь `cdp_handle` (`_click_trusted`) — та же охрана."""
    _open(page, SIMPLE_URL, PLAIN)
    assert md._click_trusted(page, page.query_selector("#trigger")) is False
    assert _clicks(page)["cover"] == 0


def test_the_classic_editor_still_presses_a_plainly_covered_target(page):
    """Прежний мастер не меняется: обычная накладка — нажатие как раньше."""
    _open(page, CLASSIC_URL, PLAIN)
    assert md._click_xpath(page, "//*[@id='trigger']", scroll=False) is True
    assert _clicks(page)["cover"] == 1


def test_an_uncovered_target_is_pressed(page):
    _open(page, SIMPLE_URL, "")
    page.evaluate("document.getElementById('cover').style.display = 'none'")
    assert md._click_xpath(page, "//*[@id='trigger']", scroll=False) is True
    assert _clicks(page)["trigger"] == 1


def test_the_wizard_opener_never_reaches_publish_even_if_the_check_was_stale(page, monkeypatch):
    """Промежуток между проверкой `hit` и нажатием: проверка сказала «видно»,
    к нажатию кнопку накрыл футер. Ни xpath-клик, ни `cdp_handle` не жмут."""
    _open(page, SIMPLE_URL, LIVE_FOOTER)
    monkeypatch.setattr(md, "_scroll_xpath_into_view",
                        lambda p, xp: {"enabled": True, "in_view": True, "hit": True})
    opened, tries = md._open_creative_wizard(page, TRIGGER, ITEM, attempts=2)
    assert opened is False
    assert _clicks(page)["cover"] == 0 and _clicks(page)["trigger"] == 0
    assert all(t.get("xpath") is False and t.get("cdp_handle") is False for t in tries), tries
