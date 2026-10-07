# -*- coding: utf-8 -*-
"""Website выбирается, даже когда меню Conversion location не открывается кликом (ACL-1572).

Сентри PYTHON-3F, 01-06.10: `conversion_website_pick: not_selected` на Doc21
(×5), Elf403, KLD_BFL_B91 и Facebook_5265. Во всех снимках отказа — закрытый
триггер «Website and instant forms» (`role=combobox`, `aria-haspopup=listbox`,
`aria-expanded=false`), а в телеметрии `menu_closed: 8`, `reopened: 0`:
меню не открывалось ни разу, и все восемь способов выбора не запускались.
На снимках 87d43d6202af / 162a95a64776 фокус стоит в поле имени ad set'а
(его список подсказок съедает первое нажатие мимо).

Стенд: вырезка Doc21 (`foreign_windows_doc21_2026_10_04.html`, снимок
254ee3b3e0c7/00_conversion_website: поле имени и секция Conversion с живым
триггером). Живого снимка ОТКРЫТОГО меню нет — оно эмулируется тестом; поведение
Meta (клик не доходит, фокус в поле имени) — гипотеза по снимкам и телеметрии.
"""
from __future__ import annotations

import pytest

from src.fb_ads_rpa import foreign_windows as fw
from src.fb_ads_rpa import meta_dom as md
from tests.bench_support import FIXTURES, browser, fragment  # noqa: F401

DOC21 = FIXTURES / "foreign_windows_doc21_2026_10_04.html"

#: Меню без заголовков «Single»/«Multiple» (разметка, которую старая проверка
#: «открыто ли меню» не узнавала) — открытость видна только по aria-expanded.
_MENU = """
<div id="menu" style="display:none">
  <div class="opt"><span>Website and instant forms</span></div>
  <div class="opt" role="radio" aria-checked="false"><span id="web">Website</span></div>
</div>
"""

_MENU_NO_WEBSITE = _MENU.replace(
    '<div class="opt" role="radio" aria-checked="false"><span id="web">Website</span></div>', "")

#: mode: dead_click — клик (мышь и скрипт) не открывает меню, Enter открывает;
#:       name_focus — клик не открывает, пока фокус в поле имени;
#:       dead — меню не открывается ничем; ok — обычный триггер.
_EMULATION = """
<script>
  const MODE = '%s';
  const menu = document.getElementById('menu');
  const trigger = [...document.querySelectorAll('[role=combobox]')]
    .find((e) => /Website and instant forms/.test(e.textContent));
  trigger.id = 'trigger';
  trigger.parentElement.appendChild(menu);
  const setOpen = (open) => {
    menu.style.display = open ? 'block' : 'none';
    trigger.setAttribute('aria-expanded', open ? 'true' : 'false');
  };
  const leaf = (() => {
    const w = document.createTreeWalker(trigger, NodeFilter.SHOW_TEXT);
    for (let t = w.nextNode(); t; t = w.nextNode())
      if (t.nodeValue.trim() === 'Website and instant forms') return t;
  })();
  const toggle = () => setOpen(menu.style.display === 'none');
  trigger.addEventListener('click', () => {
    if (MODE === 'dead_click' || MODE === 'dead') return;
    if (MODE === 'name_focus' && document.activeElement &&
        document.activeElement.tagName === 'INPUT') return;
    toggle();
  });
  trigger.addEventListener('keydown', (e) => {
    if (MODE === 'dead') return;
    if (e.key === 'Enter' && MODE === 'dead_click') toggle();
  });
  for (const o of document.querySelectorAll('.opt')) o.addEventListener('click', () => {
    leaf.nodeValue = o.textContent.trim();
    setOpen(false);
  });
</script>
"""


def _page_html(mode: str, menu: str = _MENU, *, with_section: bool = True) -> str:
    body = fragment(DOC21, "adset_name_section")
    if with_section:
        body += fragment(DOC21, "conversion_section")
    emu = _EMULATION % mode if with_section else ""
    return ("<!doctype html><html><head><style>body{font:14px sans-serif;margin:0}"
            "#editor{margin:20px 0 0 100px;width:520px}</style></head><body>"
            f"<div id='editor'>{body}</div>{menu if with_section else ''}{emu}</body></html>")


@pytest.fixture
def rows(monkeypatch):
    got = []
    monkeypatch.setattr(md._telemetry, "record", lambda step, ok=None, **k: got.append((step, ok, k)))
    return got


@pytest.fixture
def page(browser, monkeypatch, rows):  # noqa: F811
    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    monkeypatch.setattr(md, "WEBSITE_VALUE_WAIT_S", 0.3)
    monkeypatch.setattr(md, "dismiss_publish_draft_dialog", lambda *a, **k: False)
    fw.reset()
    ctx = browser.new_context(viewport={"width": 1500, "height": 950})
    ctx.route("**/*", lambda route: route.abort())
    p = ctx.new_page()
    try:
        yield p
    finally:
        ctx.close()
        fw.reset()


def _fake_methods(monkeypatch, *impls):
    names = ["_click_website_via_keyboard_focus", "_click_website_via_cdp",
             "_pick_website_via_recorded_recipe", "_click_website_via_mouse",
             "_click_website_via_force_locator", "_click_website_via_js_row",
             "_click_website_via_keyboard", "_pick_website_from_aria_controls"]
    calls = []
    for i, name in enumerate(names):
        impl = impls[i] if i < len(impls) else (lambda p: False)

        def wrapped(p, _impl=impl, _name=name):
            calls.append(_name)
            return _impl(p)
        monkeypatch.setattr(md, name, wrapped)
    return calls


def _clicks_website(p) -> bool:
    if not p.is_visible("#web"):
        return False
    p.click("#web")
    return True


def _trigger_text(p) -> str:
    return p.inner_text("#trigger").replace(chr(0x200b), "").strip()


# ── чтение состояния по снимку ───────────────────────────────────────────────

def test_the_captured_closed_trigger_reads_as_closed(page):
    """Снимок 254ee3b3e0c7: триггер найден, aria-expanded=false, меню не открыто."""
    page.set_content(_page_html("ok"))
    st = md._conversion_trigger_state(page)
    assert st["label"] is True and st["trigger"] is True and st["expanded"] is False
    assert md._conversion_location_menu_open(page) is False


def test_an_open_menu_without_group_headings_is_seen_by_the_trigger(page):
    page.set_content(_page_html("ok"))
    page.evaluate("document.getElementById('trigger').click()")
    assert md._conversion_location_menu_open(page) is True


# ── открытие ─────────────────────────────────────────────────────────────────

def test_a_click_that_never_arrives_is_followed_by_the_keyboard(page, monkeypatch, rows):
    """8 из 8 отказов: клик по триггеру не открывал меню. Enter с фокусом — открывает."""
    page.set_content(_page_html("dead_click"))
    _fake_methods(monkeypatch, _clicks_website)
    assert md._pick_website_from_open_menu(page) is True
    assert _trigger_text(page) == "Website"
    step, ok, kw = rows[-1]
    assert step == "conversion_website_pick" and ok is True
    assert kw["diag"]["reopened"] >= 1 and kw["diag"]["menu_closed"] >= 1


def test_the_focus_in_the_name_field_is_released_before_the_trigger_is_pressed(page, monkeypatch, rows):
    """162a95a64776 / 87d43d6202af: фокус в поле имени — первое нажатие по меню пропадало."""
    page.set_content(_page_html("name_focus"))
    page.focus("input[placeholder*='ad set name' i]")
    assert md._conversion_trigger_state(page)["name_focus"] is True
    _fake_methods(monkeypatch, _clicks_website)
    assert md._pick_website_from_open_menu(page) is True
    assert _trigger_text(page) == "Website"


def test_a_menu_closed_by_a_method_is_reopened_before_the_next_one(page, monkeypatch, rows):
    page.set_content(_page_html("ok"))
    page.evaluate("document.getElementById('trigger').click()")

    def _closes(p) -> bool:
        p.evaluate("document.getElementById('trigger').click()")
        return False

    calls = _fake_methods(monkeypatch, _closes, _clicks_website)
    assert md._pick_website_from_open_menu(page) is True
    assert calls[:2] == ["_click_website_via_keyboard_focus", "_click_website_via_cdp"]
    assert rows[-1][2]["diag"]["reopened"] == 1


# ── ясные причины ────────────────────────────────────────────────────────────

def test_a_menu_that_never_opens_stops_early_and_says_so(page, monkeypatch, rows):
    page.set_content(_page_html("dead"))
    _fake_methods(monkeypatch, _clicks_website)
    assert md._pick_website_from_open_menu(page) is False
    step, ok, kw = rows[-1]
    assert ok is False
    assert kw["note"].startswith("not_selected: The Conversion location menu would not stay open")
    assert len(kw["diag"]["tried"]) <= 3
    assert kw["diag"]["reason"] == kw["note"].split(": ", 1)[1]


def test_website_not_offered_is_named(page, monkeypatch, rows):
    page.set_content(_page_html("ok", _MENU_NO_WEBSITE))
    page.evaluate("document.getElementById('trigger').click()")
    _fake_methods(monkeypatch)
    assert md._pick_website_from_open_menu(page) is False
    assert rows[-1][2]["note"] == "not_selected: Website is not offered in the open Conversion location menu"


def test_a_screen_without_conversion_location_is_named(page, monkeypatch, rows):
    """5265 (e2df3122c39f): на экране редактор объявления, поля Conversion location нет."""
    page.set_content(_page_html("ok", with_section=False))
    _fake_methods(monkeypatch)
    assert md._pick_website_from_open_menu(page) is False
    note = rows[-1][2]["note"]
    assert note.startswith("not_selected: Conversion location is not on this screen")
