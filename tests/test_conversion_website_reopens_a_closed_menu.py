# -*- coding: utf-8 -*-
"""Website выбирается и тогда, когда меню Conversion location успело закрыться (ACL-1313, 25.09).

19 полных отказов `conversion_website` с 19.09 (сегодня 90a8987edad9, SA
Investing): все восемь способов `_pick_website_from_open_menu` работали по
уже закрытому меню — на снимке закрытый список «Website and instant forms»,
в page.html ни одного role=radio. Теперь перед каждым способом меню
проверяется и открывается заново, а успех засчитывается только по
показанному в триггере значению «Website».

Разметка синтетическая: живого снимка ОТКРЫТОГО меню в rpa_pages нет.
Закрытое состояние повторяет снимок 90a8987edad9 (подпись «Conversion
location», триггер «Website and instant forms», ниже «Conversion event»),
открытое — запись рецепта config/rpa_recipes/conversion_location_website.json
(заголовки «Multiple»/«Single» с role=heading, SPAN «Website»).
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
<!doctype html><html><body style="font:14px sans-serif">
<div style="width:480px;margin:40px">
  <div>Conversion</div>
  <div><span>Conversion location</span></div>
  <div>Choose where you want to generate leads.</div>
  <div role="button" aria-haspopup="listbox" id="trigger"
       style="border:1px solid #999;padding:6px">Website and instant forms</div>
  <div id="menu" style="display:none;border:1px solid #ccc">
    <div role="heading">Multiple</div>
    <div class="opt"><span>Website and instant forms</span></div>
    <div class="opt"><span>Website and calls</span></div>
    <div role="heading">Single</div>
    <div>Send people to one location where they're most likely to convert.</div>
    <div class="opt" role="radio" aria-checked="false"><span id="web">Website</span></div>
    <div class="opt"><span>Instant forms</span></div>
  </div>
  <div style="height:30px"></div>
  <div><span>Conversion event</span></div>
  <div role="button">Lead</div>
</div>
<script>
  const menu = document.getElementById('menu');
  const trigger = document.getElementById('trigger');
  window.__opens = 0;
  trigger.addEventListener('click', () => {
    const open = menu.style.display !== 'none';
    menu.style.display = open ? 'none' : 'block';
    if (!open) window.__opens++;
  });
  for (const o of document.querySelectorAll('.opt')) o.addEventListener('click', () => {
    trigger.textContent = o.textContent.trim();
    menu.style.display = 'none';
  });
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
def rows(monkeypatch):
    got = []
    monkeypatch.setattr(md._telemetry, "record", lambda step, ok, **k: got.append((step, ok, k)))
    return got


@pytest.fixture
def page(browser, monkeypatch, rows):
    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    monkeypatch.setattr(md, "WEBSITE_VALUE_WAIT_S", 0.3)
    monkeypatch.setattr(md, "dismiss_publish_draft_dialog", lambda *a, **k: False)
    ctx = browser.new_context(viewport={"width": 1000, "height": 700})
    p = ctx.new_page()
    p.route("**/*", lambda route: route.abort())
    p.set_content(_PAGE)
    try:
        yield p
    finally:
        ctx.close()


def _open(page):
    page.click("#trigger")


def _is_open(page) -> bool:
    return page.evaluate("document.getElementById('menu').style.display !== 'none'")


def _fake_methods(monkeypatch, *impls):
    """Подменить восемь способов выбора: первые — `impls`, остальные — «не вышло»."""
    names = ["_click_website_via_keyboard_focus", "_click_website_via_cdp",
             "_pick_website_via_recorded_recipe", "_click_website_via_mouse",
             "_click_website_via_force_locator", "_click_website_via_js_row",
             "_click_website_via_keyboard", "_pick_website_from_aria_controls"]
    calls: list[tuple[str, bool]] = []
    for i, name in enumerate(names):
        impl = impls[i] if i < len(impls) else (lambda p: False)

        def wrapped(p, _impl=impl, _name=name):
            calls.append((_name, _is_open(p)))
            return _impl(p)
        monkeypatch.setattr(md, name, wrapped)
    return calls


def _closes_menu(p) -> bool:
    """Способ, который промахнулся и закрыл меню (клик мимо / лишняя клавиша)."""
    p.evaluate("document.getElementById('menu').style.display = 'none'")
    return False


def _clicks_website_if_visible(p) -> bool:
    if not p.is_visible("#web"):
        return False
    p.click("#web")
    return True


# ── чтение состояния ──────────────────────────────────────────────────────

def test_the_closed_trigger_text_is_not_an_open_menu(page):
    """Снимок 90a8987edad9: «Website and instant forms» — значение закрытого триггера."""
    assert md._conversion_location_menu_open(page) is False
    _open(page)
    assert md._conversion_location_menu_open(page) is True


# ── механизм ──────────────────────────────────────────────────────────────

def test_a_menu_closed_by_one_method_is_reopened_for_the_next(page, monkeypatch, rows):
    _open(page)
    calls = _fake_methods(monkeypatch, _closes_menu, _clicks_website_if_visible)
    assert md._pick_website_from_open_menu(page) is True
    assert page.inner_text("#trigger") == "Website"
    # Второй способ получил ОТКРЫТОЕ меню.
    assert calls[1] == ("_click_website_via_cdp", True)
    step, ok, kw = rows[-1]
    assert step == "conversion_website_pick" and ok is True and kw["note"] == "cdp"
    assert kw["diag"]["reopened"] == 1


def test_a_menu_that_never_opened_is_opened_before_the_first_method(page, monkeypatch):
    """Прогон 90a898: «after_conversion_menu_open» — а меню на снимке закрыто."""
    calls = _fake_methods(monkeypatch, _clicks_website_if_visible)
    assert md._pick_website_from_open_menu(page) is True
    assert calls[0] == ("_click_website_via_keyboard_focus", True)
    assert page.inner_text("#trigger") == "Website"


def test_an_open_menu_is_not_clicked_shut(page, monkeypatch):
    """Открытое меню повторно не «открывают»: клик по триггеру его бы закрыл."""
    _open(page)
    _fake_methods(monkeypatch, _clicks_website_if_visible)
    assert md._pick_website_from_open_menu(page) is True
    assert page.evaluate("window.__opens") == 1


def test_a_method_that_says_yes_without_website_in_the_trigger_is_not_a_success(page, monkeypatch, rows):
    """Способ ответил True, а в триггере по-прежнему «Website and instant forms»."""
    _open(page)

    def _lies(p) -> bool:
        return True

    _fake_methods(monkeypatch, *([_lies] * 8))
    assert md._pick_website_from_open_menu(page) is False
    step, ok, kw = rows[-1]
    assert ok is False and kw["note"] == "not_selected"


def test_website_and_instant_forms_is_not_website(page, monkeypatch):
    """Выбор соседнего «Website and instant forms» проверкой не засчитывается."""
    _open(page)

    def _picks_the_wrong_one(p) -> bool:
        p.get_by_text("Website and calls", exact=True).click()
        return True

    _fake_methods(monkeypatch, _picks_the_wrong_one)
    assert md._pick_website_from_open_menu(page) is False
    assert page.inner_text("#trigger") == "Website and calls"
