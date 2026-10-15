# -*- coding: utf-8 -*-
"""Languages field with the language already chosen, run through `fill_audience_languages`.

Bench (ACL-1433). `languages_chips_2026_10_01.html`, both captures taken by the
step itself (`languages_no_suggestion`: after typing, no suggestion list):

- `sa_arabic_typed` — 0afc79a410c6/02 (SA PF, 29.09): chip "Arabic" (Meta's
  country default) and "Arabic" still typed in the search box.
- `co_spanish_two_chips` — 243f23a1bc1b/01 (CO_bnc Mix, 01.10): two chips,
  "Spanish" and "Spanish (All)", empty search box.

The same capture was taken in 8ce1b9985046, 9993b097fb70, b3a333ff43ab,
eee3be108750, 03086e22f5d2, 68cc447bd867, df5344da3a17, 2ce7fd3be6f1,
4121938e6dc3, b47bc815bb3c (29.09–01.10, all "Spanish"): a chosen language is
not offered again as a suggestion, so "no suggestion" is expected there. On
this markup the step must see the chip and succeed without typing.
"""
from __future__ import annotations

import time

import pytest

from src.fb_ads_rpa import meta_dom as md
from tests.bench_support import FIXTURES, browser, fragment, offline_page  # noqa: F401

FIXTURE = FIXTURES / "languages_chips_2026_10_01.html"

CASES = {
    "sa_arabic_typed": ("Arabic", ["Arabic"]),
    "co_spanish_two_chips": ("Spanish", ["Spanish", "Spanish (All)"]),
}


@pytest.fixture
def page(offline_page, monkeypatch):
    monkeypatch.setattr(md, "_sleep_h", lambda base, spread=0.3: time.sleep(min(base, 0.02)))
    return offline_page


@pytest.mark.parametrize("name", sorted(CASES))
def test_the_chosen_language_is_read(page, name):
    lang, chips = CASES[name]
    page.set_content(fragment(FIXTURE, name))
    assert md._languages_selected(page, [lang]) == chips
    assert md._languages_read(page, [lang]) == chips


@pytest.mark.parametrize("name", sorted(CASES))
def test_the_step_succeeds_without_typing(page, name):
    lang, _ = CASES[name]
    page.set_content(fragment(FIXTURE, name))
    before = page.input_value("input[role=combobox]")
    typed: list[str] = []
    orig_type = page.keyboard.type
    page.keyboard.type = lambda text, **kw: (typed.append(text), orig_type(text, **kw))[1]  # type: ignore[method-assign]
    report: dict = {}
    assert md.fill_audience_languages(page, [lang], report) is True, report
    assert typed == []
    assert page.input_value("input[role=combobox]") == before


def test_suggestion_state_is_what_the_robot_recorded(page):
    """`diag` of 243f23a1bc1b: expanded "false", listbox false, no options."""
    page.set_content(fragment(FIXTURE, "co_spanish_two_chips"))
    got = md._language_suggestions_state(page.locator("input[role=combobox]").first)
    assert got.get("expanded") == "false" and got.get("listbox") is False
    assert got.get("option_count") == 0
