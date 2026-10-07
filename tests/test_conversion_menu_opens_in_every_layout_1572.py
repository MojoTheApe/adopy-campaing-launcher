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

import time

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
#:       name_focus — клик не открывает, пока фокус в поле имени; триггер не
#:         фокусируется, а mousedown не трогает фокус (preventDefault), так что
#:         одно нажатие мыши фокус из поля не уберёт — только явный blur;
#:       focus_stuck — поле имени возвращает себе фокус, у него активная
#:         подсказка, Enter в нём принимает её (имя меняется); считаются Enter;
#:       renames_on_open — открытие меню заодно меняет имя (подсказка принята);
#:       dead — меню не открывается ничем; ok — обычный триггер.
_EMULATION = """
<script>
  const MODE = '%s';
  const menu = document.getElementById('menu');
  const trigger = [...document.querySelectorAll('[role=combobox]')]
    .find((e) => /Website and instant forms/.test(e.textContent));
  trigger.id = 'trigger';
  const nameInput = document.querySelector("input[placeholder*='ad set name' i]");
  window.__enters = 0;
  document.addEventListener('keydown', (e) => { if (e.key === 'Enter') window.__enters += 1; }, true);
  if (MODE === 'name_focus') {
    trigger.removeAttribute('tabindex');
    trigger.addEventListener('mousedown', (e) => e.preventDefault());
  }
  if (MODE === 'focus_stuck') {
    nameInput.setAttribute('aria-activedescendant', 'sugg1');
    nameInput.addEventListener('blur', () => setTimeout(() => nameInput.focus(), 0));
    nameInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') nameInput.value = 'SUGGESTION'; });
  }
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
  const toggle = () => {
    setOpen(menu.style.display === 'none');
    if (MODE === 'renames_on_open') nameInput.value = 'SUGGESTION';
  };
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
    """162a95a64776 / 87d43d6202af: фокус в поле имени — первое нажатие по меню пропадало.

    Триггер не фокусируется и не отнимает фокус при mousedown: нажатие мыши само
    фокус из поля не уберёт. Остальные способы открытия отключены — открыть меню
    может только клик после явного blur (без blur тест падает)."""
    page.set_content(_page_html("name_focus"))
    page.focus("input[placeholder*='ad set name' i]")
    assert md._conversion_trigger_state(page)["name_focus"] is True
    monkeypatch.setattr(md, "_open_conversion_menu_via_keyboard", lambda p: False)
    monkeypatch.setattr(md, "_open_conversion_menu_via_js", lambda p: False)
    _fake_methods(monkeypatch, _clicks_website)
    assert md._pick_website_from_open_menu(page) is True
    assert _trigger_text(page) == "Website"
    assert page.evaluate("document.activeElement.tagName") != "INPUT"


def test_without_the_blur_the_name_focus_swallows_the_click(page, monkeypatch):
    """Контроль стенда: без blur клик по триггеру в режиме name_focus не открывает меню."""
    page.set_content(_page_html("name_focus"))
    page.focus("input[placeholder*='ad set name' i]")
    monkeypatch.setattr(md, "_release_name_focus", lambda p: True)
    monkeypatch.setattr(md, "_open_conversion_menu_via_keyboard", lambda p: False)
    monkeypatch.setattr(md, "_open_conversion_menu_via_js", lambda p: False)
    assert md._ensure_conversion_menu_open(page) is False


def test_only_the_script_press_opens_the_menu_with_one_click(page, monkeypatch):
    """Клик и клавиатура отключены; триггер — переключатель: два click закрыли бы меню."""
    page.set_content(_page_html("ok"))
    monkeypatch.setattr(md, "click_conversion_location_dropdown", lambda p: False)
    monkeypatch.setattr(md, "_open_conversion_menu_via_keyboard", lambda p: False)
    page.evaluate("() => { window.__clicks = 0; document.getElementById('trigger')"
                  ".addEventListener('click', () => window.__clicks++); }")
    assert md._ensure_conversion_menu_open(page) is True
    assert page.evaluate("window.__clicks") == 1


def test_a_failing_first_opener_does_not_stop_the_next_ones(page, monkeypatch):
    """`continue`, а не `return False`: первый способ не нашёл триггер — идёт второй."""
    page.set_content(_page_html("ok"))
    monkeypatch.setattr(md, "click_conversion_location_dropdown", lambda p: False)
    monkeypatch.setattr(md, "_open_conversion_menu_via_keyboard", lambda p: False)
    assert md._ensure_conversion_menu_open(page) is True


def test_enter_is_not_pressed_while_the_focus_stays_in_the_name_field(page, monkeypatch):
    """Фокус возвращается в поле имени, подсказка активна: Enter принял бы её и сменил имя."""
    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: time.sleep(0.1))
    page.set_content(_page_html("focus_stuck"))
    page.focus("input[placeholder*='ad set name' i]")
    page.fill("input[placeholder*='ad set name' i]", "Ad set one")
    page.focus("input[placeholder*='ad set name' i]")
    assert md._open_conversion_menu_via_keyboard(page) is False
    assert page.evaluate("window.__enters") == 0
    assert page.input_value("input[placeholder*='ad set name' i]") == "Ad set one"


def test_enter_is_not_pressed_when_the_trigger_is_missing(page):
    page.set_content(_page_html("ok", with_section=False))
    assert md._open_conversion_menu_via_keyboard(page) is False


def test_a_changed_ad_set_name_blocks_the_step_with_a_reason(page, monkeypatch, rows):
    page.set_content(_page_html("renames_on_open"))
    page.fill("input[placeholder*='ad set name' i]", "Ad set one")
    _fake_methods(monkeypatch, _clicks_website)
    assert md._pick_website_from_open_menu(page) is False
    note = rows[-1][2]["note"]
    assert note.startswith("not_selected: The ad set name changed")
    assert md.last_conversion_failure_reason() == note.split(": ", 1)[1]


def test_a_page_script_that_fails_everywhere_is_a_read_error_not_an_absent_field(monkeypatch):
    class _Boom:
        def evaluate(self, *a, **k):
            raise RuntimeError("navigating")

    monkeypatch.setattr(md, "_roots", lambda p: [_Boom()])
    assert md._conversion_trigger_state(object()) == {"read_error": True}
    reason = md._conversion_failure_reason(object())
    assert reason.startswith("Could not read the Conversion location field")
    assert "not on this screen" not in reason


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
    assert md.last_conversion_failure_reason() == kw["diag"]["reason"]


def test_a_menu_that_could_not_be_pressed_is_not_called_unstable(page, monkeypatch, rows):
    """«Would not stay open» — только когда нажаты все три способа."""
    page.set_content(_page_html("dead"))
    monkeypatch.setattr(md, "_open_conversion_menu_via_keyboard", lambda p: False)
    _fake_methods(monkeypatch, _clicks_website)
    assert md._pick_website_from_open_menu(page) is False
    note = rows[-1][2]["note"]
    assert "would not stay open" not in note
    assert note.startswith("not_selected: The Conversion location menu did not open")


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
