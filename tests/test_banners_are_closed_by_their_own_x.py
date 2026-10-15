# -*- coding: utf-8 -*-
"""Баннеры Meta закрываются своим крестиком, а не крестиком всего редактора (ACL-1313).

`_dismiss_floating_banners` и `_dismiss_advantage_banner` искали
`[aria-label="Close"]`. У крестиков баннеров Meta такого атрибута нет: это
`div[role=button]`, внутри которого спрятанный `<div data-sscoverage-ignore=
"true">Close</div>`. А единственный `aria-label="Close"` в редакторе — это
крестик ВСЕГО редактора под `data-surface="/am/editor_drawer"`: нажатие
закрывает черновик и выбрасывает прогон на список кампаний. Старый поиск,
таким образом, баннеров не закрывал никогда, а если чем и щёлкал — то тем,
что щёлкать нельзя.

Разметка — из живых снимков (fixtures/ads_manager/banners_2026_09_25.html):

* синяя полоса «Add your contact information» (279ead80503a) и «Add your phone
  number» (8814247ec92e) — `span[data-surface*="AYMTAMGlobalBannerContainer"]`;
* карточки с крестиком: «WhatsApp Status included», «Set up the Conversions
  API…» (5b223701b271), «Use filters to verify Instant form leads»
  (90a8987edad9), «Add products or site links» (393bfda8b0db);
* карточка «Show your offerings…» без крестика (её «Remind me later» открывает
  новую вкладку) и шапка редактора с его крестиком.

«Updated business terms» и «Act now — Verification may be required soon»
в снимках не попались ни разу; для них разметка синтетическая, по образцу
настоящей полосы AYMT и настоящих карточек.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import meta_dom as md  # noqa: E402
from src.fb_ads_rpa import run_telemetry  # noqa: E402

FIX = (Path(__file__).parent / "fixtures" / "ads_manager" / "banners_2026_09_25.html").read_text(encoding="utf-8")
FRAG = {m.group(1): m.group(2).strip() for m in re.finditer(
    r"<!-- fragment: (\w+) \| source: [^>]*-->\n(.*?)(?=\n<!-- fragment: |\Z)", FIX, re.S)}

_CLOSE_BTN = ('<div aria-busy="false" role="button" tabindex="0"><span><div>'
              '<div data-sscoverage-ignore="true">Close</div><div><div role="presentation">'
              '<svg width="16" height="16"></svg></div>\u200b</div></div></span></div>')

# Синтетика по образцу настоящей полосы AYMT: в снимках этих сообщений нет.
BUSINESS_TERMS = (
    '<span data-surface="/am/msg:AYMTAMGlobalBannerContainer" style="display: contents;">'
    '<span data-surface="/am/msg:AYMTAMGlobalBannerContainer/info:msg:1" style="display: contents;">'
    '<div><div><div><span>Updated business terms</span></div><div><span>We have updated our terms.</span></div></div>'
    '<div role="none"><div role="button" tabindex="0"><span>Learn more</span></div></div>'
    + _CLOSE_BTN + '</div></span></span>')
VERIFICATION = (
    '<div><div><div><div><div aria-level="4" role="heading">Act now \u2014 Verification may be required soon</div></div>'
    + _CLOSE_BTN + '</div><div><span>Verify your business to keep advertising.</span>'
    '<div role="button" tabindex="0"><span>Start verification</span></div>'
    '<div role="button" tabindex="0"><span>Remind me later</span></div></div></div></div>')


def _editor(*cards: str) -> str:
    """Карточки редактора живут внутри data-surface="/am/editor_drawer"."""
    header = FRAG["editor_drawer_header"]
    assert header.endswith("</div></div></span>")
    inner = header[: -len("</span>")]
    return inner + "<div>" + "".join(f"<div>{c}</div>" for c in cards) + "</div></span>"


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


_SPY = """
() => {
  window.clicked = [];
  document.addEventListener('click', (e) => {
    const b = e.target.closest('[role=button], button, a');
    if (!b) return;
    const label = b.getAttribute('aria-label') || (b.textContent || '').replace(/[\\u200b]/g, '').trim();
    window.clicked.push(label);
    if (b.getAttribute('aria-label') === 'Close') { window.drawerClosed = true; return; }
    if (label === 'Close') {
      // Как у Meta: крестик убирает свой баннер.
      let n = b;
      for (let i = 0; i < 9 && n.parentElement; i++) {
        n = n.parentElement;
        if (n.getAttribute('data-banner') !== null) { n.remove(); return; }
      }
    }
  }, true);
}
"""


def _page(page, *blocks: str) -> None:
    page.set_content("".join(f'<div data-banner="{i}">{b}</div>' for i, b in enumerate(blocks)))
    page.evaluate(_SPY)


def _clicked(page) -> list[str]:
    return page.evaluate("window.clicked")


def _visible(page, text: str) -> bool:
    return page.get_by_text(text).count() > 0


# ── синяя полоса AYMT ─────────────────────────────────────────────────────

@pytest.mark.parametrize("frag,title,cta", [
    ("aymt_contact", "Add your contact information", "Add contact information"),
    ("aymt_phone", "Add your phone number", "Add mobile number"),
])
def test_the_blue_account_bar_is_closed_by_its_x(page, frag, title, cta):
    _page(page, FRAG[frag])
    closed = md._dismiss_floating_banners(page)
    assert not _visible(page, title)
    assert _clicked(page) == ["Close"], "нажато что-то кроме крестика"
    assert cta not in _clicked(page)
    assert closed and closed[0].startswith(("aymt:", title))


def test_updated_business_terms_are_closed_without_learn_more(page):
    _page(page, BUSINESS_TERMS)
    md._dismiss_floating_banners(page)
    assert not _visible(page, "Updated business terms")
    assert _clicked(page) == ["Close"]


# ── карточки редактора ────────────────────────────────────────────────────

@pytest.mark.parametrize("frag,title", [
    ("card_whatsapp_status", "WhatsApp Status included"),
    ("card_conversions_api", "Set up the Conversions API"),
    ("card_instant_form_filters", "Use filters to verify Instant form leads"),
    ("card_site_links", "Add products or site links"),
])
def test_an_editor_card_is_closed_and_the_editor_is_not(page, frag, title):
    page.set_content(_editor(f'<div data-banner="0">{FRAG[frag]}</div>'))
    page.evaluate(_SPY)
    closed = md._dismiss_floating_banners(page)
    assert not _visible(page, title)
    assert _clicked(page) == ["Close"]
    assert not page.evaluate("window.drawerClosed || false"), "закрыт весь редактор"
    assert closed == [next(t for t in md._BANNER_TITLES if title.startswith(t) or t.startswith(title))]


def test_the_verification_card_never_starts_verification(page):
    page.set_content(_editor(f'<div data-banner="0">{VERIFICATION}</div>'))
    page.evaluate(_SPY)
    md._dismiss_floating_banners(page)
    assert not _visible(page, "Verification may be required soon")
    assert _clicked(page) == ["Close"]


def test_several_cards_each_lose_only_their_own_card(page):
    page.set_content(_editor(
        f'<div data-banner="0">{FRAG["card_whatsapp_status"]}</div>',
        f'<div data-banner="1">{FRAG["card_conversions_api"]}</div>',
        f'<div data-banner="2">{FRAG["card_show_offerings_no_close"]}</div>',
    ))
    page.evaluate(_SPY)
    md._dismiss_floating_banners(page)
    assert not _visible(page, "WhatsApp Status included")
    assert not _visible(page, "Set up the Conversions API")
    assert _visible(page, "Show your offerings in more engaging ads"), "карточку без крестика не трогаем"
    assert _clicked(page) == ["Close", "Close"]


# ── чего нажимать нельзя ──────────────────────────────────────────────────

def test_the_editor_close_is_never_pressed(page):
    """На странице только шапка редактора: старый код жал её крестик."""
    page.set_content(_editor(f'<div>{FRAG["card_show_offerings_no_close"]}</div>'))
    page.evaluate(_SPY)
    md._dismiss_floating_banners(page)
    md._dismiss_advantage_banner(page)
    assert _clicked(page) == []
    assert not page.evaluate("window.drawerClosed || false")


def test_a_card_without_its_own_x_is_left_alone(page):
    """«Show your offerings»: «Remind me later» открывает новую вкладку."""
    _page(page, FRAG["card_show_offerings_no_close"])
    assert md._dismiss_floating_banners(page) == []
    assert _clicked(page) == []


def test_the_editor_close_is_never_a_closer_even_when_it_is_the_only_one(page):
    """PR #314, M15: крестик редактора не становится «своим крестиком»
    сообщения ни при каком раскладе — даже если он единственный внутри
    сообщения полосы AYMT (там нет проверки контейнера, как у карточек)."""
    page.set_content(
        '<span data-surface="/am/msg:AYMTAMGlobalBannerContainer/info:msg:9" style="display: contents;">'
        '<div><span>Something about your account</span>'
        '<span data-surface="/am/editor_drawer"><div aria-label="Close" role="button" tabindex="0"'
        ' style="width:20px;height:20px"><svg width="16" height="16"></svg></div></span></div></span>')
    page.evaluate(_SPY)
    assert md._dismiss_floating_banners(page) == []
    assert _clicked(page) == []
    assert not page.evaluate("window.drawerClosed || false")
