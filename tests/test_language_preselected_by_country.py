# -*- coding: utf-8 -*-
"""Язык, который Meta сама поставила по стране, — успех шага (ACL-1313, 25.09).

20/20 прогонов SA (Saudi) падали на `languages`: Meta ставит «Arabic» по
стране, у чипа нет «(All)», а `_languages_selected` узнавал только формы
«X (All)» / «X (…)» — уже выбранный язык не виделся, шаг отказывал при
верном поле (снимок 79d4d978f8c7 fail_languages: чип «Arabic» стоит).

Второе: запасной клик по первой подсказке мог взять региональный вариант
(«Arabic (Egypt)») — теперь только «<язык> (All)» или ровно «<язык>».

Фикстура languages_preselected_2026_09_25.html — блок Languages из
rpa_pages/79d4d978f8c7/02_languages (классы, svg и служебные атрибуты
вырезаны; имён и id аккаунта в блоке нет). Подсказки — синтетика.
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

FIX = Path(__file__).parent / "fixtures" / "ads_manager"
ARABIC_SET = (FIX / "languages_preselected_2026_09_25.html").read_text(encoding="utf-8")


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
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    p = ctx.new_page()
    p.route("**/*", lambda route: route.abort())
    try:
        yield p
    finally:
        ctx.close()


def _typed(page) -> list[str]:
    typed: list[str] = []
    orig = page.keyboard.type
    page.keyboard.type = lambda text, *a, **k: (typed.append(text), orig(text, *a, **k))[1]
    return typed


# ── чип без «(All)» из живого снимка ──────────────────────────────────────

def test_the_live_arabic_chip_is_read_as_selected(page):
    page.set_content(ARABIC_SET)
    assert "Arabic" in md._languages_selected(page, ["Arabic"])


def test_the_language_meta_preselected_passes_without_typing(page, rows):
    page.set_content(ARABIC_SET)
    typed = _typed(page)
    assert md.fill_audience_languages(page, ["Arabic"]) is True
    assert typed == []
    step, ok, kw = rows[-1]
    assert step == "languages" and kw["note"] == "already_selected"
    assert kw["diag"]["selected"] == ["Arabic"]


@pytest.mark.parametrize("wanted", ["arabic", "  ARABIC ", "Arabic (All)"])
def test_the_match_ignores_case_spaces_and_the_all_suffix(page, wanted):
    page.set_content(ARABIC_SET)
    assert md.fill_audience_languages(page, [wanted]) is True


def test_a_different_language_is_not_taken_for_the_chip(page):
    """«Arab» — не «Arabic», а «English» чипом «Arabic» не закрыт."""
    assert md._language_chip_matches("Arabic", "Arab") is False
    assert md._language_chip_matches("Spanish (Mexico)", "Spanish") is True
    page.set_content(ARABIC_SET)
    assert md._languages_selected(page, ["English"]) == []
    assert md.fill_audience_languages(page, ["English"]) is False


def test_the_close_button_label_is_not_a_language(page):
    page.set_content(ARABIC_SET)
    assert "Close" not in md._languages_selected(page, ["Arabic"])


# ── выбор подсказки: только «(All)» или ровно имя ─────────────────────────

_PICKER = """
<div><span>Languages</span>
  <ul id="chips"></ul>
  <input placeholder="Search languages" id="q">
  <div role="listbox" id="lb">%s</div>
</div>
<script>
  for (const o of document.querySelectorAll('[role=option]'))
    o.addEventListener('click', () => {
      const li = document.createElement('li');
      li.innerHTML = '<span>' + o.firstElementChild.textContent + '</span>';
      document.getElementById('chips').appendChild(li);
      window.__picked = o.firstElementChild.textContent;
    });
</script>
"""


def _options(*names: str) -> str:
    return "".join(f"<div role='option'><div>{n}</div><div>Language</div></div>" for n in names)


def test_a_regional_variant_is_never_clicked(page):
    page.set_content(_PICKER % _options("Arabic (Egypt)", "Arabic (Gulf)"))
    assert md.fill_audience_languages(page, ["Arabic"]) is False
    assert page.evaluate("window.__picked || null") is None
    # Набранное не остаётся в поиске.
    assert page.input_value("#q") == ""


def test_the_exact_language_is_picked_even_when_it_is_not_first(page):
    page.set_content(_PICKER % _options("Arabic (Egypt)", "Arabic"))
    assert md.fill_audience_languages(page, ["Arabic"]) is True
    assert page.evaluate("window.__picked") == "Arabic"


def test_the_all_variant_wins_over_the_bare_and_regional_ones(page):
    page.set_content(_PICKER % _options("Spanish (Spain)", "Spanish", "Spanish (All)"))
    assert md.fill_audience_languages(page, ["Spanish"]) is True
    assert page.evaluate("window.__picked") == "Spanish (All)"
