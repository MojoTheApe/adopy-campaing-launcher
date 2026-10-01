# -*- coding: utf-8 -*-
"""Placements layouts met live since 29.09, run through the robot's `fill_placements`.

Bench (ACL-1433).

1. `placements_doc19_2026_10_01.html` — SA Doc19 (run 5edc0e7cca0a,
   01_placements, 2026-10-01 08:00 UTC; capture truncated, the card is whole).
   The classic ad set editor with SIX categories (a new "Marketing messages"
   between "Search results" and "Apps and sites") and a "Stories, Status,
   Reels" list WITHOUT "Messenger Stories". The step failed with
   `subitems_missing: ["Messenger Stories"]` — the only miss. The same code
   passed that morning on other accounts: Meta's variation, not a regression.
   The captured card is the robot's own marked block after the failure; the
   Feeds and Stories categories are collapsed in it, so their rows are built
   here by the accordion emulation of `test_rpa_placements_category_recollapse`
   from a real In-stream row, with the labels the robot saw live
   (`diag.subitems_seen`).

2. `placements_simple_create_2026_09_30.html` — CO_bnc Mix (run cb854b07857b,
   02_placements, 2026-09-30, not truncated): the single-screen editor's
   Placements card, open (`aria-pressed=true`), shows only "Advantage+ on" and
   "About placements" — no categories, no "Account controls", no "Show more
   settings". The step failed with `placements_card_not_found`, stage `tag`.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from src.fb_ads_rpa import meta_dom as md
from tests.bench_support import FIXTURES, browser, fragment  # noqa: F401
from tests.test_rpa_placements_category_recollapse import _SIM_JS

DOC19 = FIXTURES / "placements_doc19_2026_10_01.html"
SIMPLE = FIXTURES / "placements_simple_create_2026_09_30.html"

#: `diag.subitems_seen` of 5edc0e7cca0a/01_placements, by category. No
#: "Messenger Stories" anywhere on the page (one match in the whole 4 MB
#: capture, and it is in the robot's own report text, not in the editor).
DOC19_ROWS = {
    "Feeds": [
        ("Facebook Feed", True, False),
        ("Facebook profile feed", True, False),
        ("Instagram feed", True, False),
        ("Instagram profile feed", True, False),
        ("Facebook Marketplace", True, False),
        ("Facebook right column", True, False),
        ("Instagram Explore home", True, False),
        ("Facebook Business Explore", True, False),
        ("Threads feed", True, False),
        ("Facebook Notifications", True, False),
    ],
    "Stories, Status, Reels": [
        ("Instagram Stories", True, False),
        ("Facebook Stories", True, False),
        ("Instagram Reels", True, False),
        ("Facebook Reels", True, False),
        ("WhatsApp Status", True, False),
    ],
    "In-stream ads for reels": [
        ("Facebook in-stream reels", True, False),
        ("Ads on Facebook Reels", True, False),
    ],
}


@pytest.fixture
def page(browser, monkeypatch, tmp_path):
    monkeypatch.setattr(md, "_sleep_h", lambda base, spread=0.3: time.sleep(min(base, 0.05)))
    monkeypatch.setattr(md, "ensure_account_placement_exclusions", lambda pg, report=None: False)
    monkeypatch.chdir(tmp_path)          # output/debug_placements_fill.txt stays out of the repo
    (tmp_path / "output").mkdir()
    ctx = browser.new_context(viewport={"width": 1000, "height": 800})
    ctx.route("**/*", lambda route: route.abort())
    p = ctx.new_page()
    try:
        yield p
    finally:
        ctx.close()


def _load_doc19(page) -> None:
    page.set_content("<html><body style='margin:0;padding-top:120px'>"
                     f"{fragment(DOC19, 'card')}</body></html>")
    page.evaluate(_SIM_JS, {"rows": {k: [list(r) for r in v] for k, v in DOC19_ROWS.items()},
                            "collapse": "never"})


def _state(page) -> dict[str, bool]:
    return {k: v["checked"] for k, v in page.evaluate("() => window.__sim.state").items()}


def test_doc19_layout_is_the_failing_one(page):
    _load_doc19(page)
    cats = page.evaluate(r"""() => [...document.querySelectorAll('[role=button][aria-expanded]')]
      .map(b => (b.textContent || '').replace(/\s+/g, ' ').trim())""")
    assert "Marketing messagesMarketing messages" in cats
    assert "Messenger Stories" not in page.content()


def test_doc19_every_offered_placement_is_set(page):
    """Everything Meta offers on Doc19 ends in the wanted state; the only miss
    is the placement Meta does not offer — the live report, reproduced."""
    _load_doc19(page)
    report: dict = {}
    md.fill_placements(page, report)
    state = _state(page)
    for label in md._SUBITEMS_ON:
        if label in state:
            assert state[label] is True, label
    for label in md._SUBITEMS_OFF:
        if label in state:
            assert state[label] is False, label
    assert report["subitems_missing"] == ["Messenger Stories"]


@pytest.mark.xfail(strict=True, reason=(
    "Live bug 2026-10-01 (Doc19, 5edc0e7cca0a): Meta no longer offers 'Messenger Stories' on "
    "this account; it is in _SUBITEMS_OFF, so its absence fails the step although an absent "
    "placement cannot be delivered to. Fixing needs a rule that tells 'not offered' from "
    "'row not rendered yet' (virtualised lists) — RPA track."))
def test_doc19_a_placement_meta_does_not_offer_is_not_a_failure(page):
    _load_doc19(page)
    report: dict = {}
    assert md.fill_placements(page, report) is True, report.get("subitems_missing")


def _load_simple(page) -> None:
    page.set_content("<html><body style='margin:0;padding-top:120px'>"
                     f"{fragment(SIMPLE, 'card')}</body></html>")


def test_simple_create_card_failure_is_named(page):
    """What the robot reports today on the single-screen Placements card."""
    _load_simple(page)
    report: dict = {}
    assert md.fill_placements(page, report) is False
    assert report["why"] == "placements_card_not_found"
    card = report["placements_card"]
    assert card["stage"] == "tag" and card["tagged"] is False and card["headings"] == 1
    assert "Placements" in card["headings_seen"]


@pytest.mark.xfail(strict=True, reason=(
    "Live bug 2026-09-30 (cb854b07857b, simple_create): the Placements card is on screen and "
    "open, but the robot anchors the card on 'Account controls', which this card does not "
    "render (only 'Advantage+ on' + 'About placements'), so it never reaches the manual "
    "controls. How Meta reveals them here was not captured — RPA track."))
def test_simple_create_card_is_found(page):
    _load_simple(page)
    report: dict = {}
    md.fill_placements(page, report)
    assert report.get("why") != "placements_card_not_found", report.get("placements_card")


def test_debug_file_is_written_to_the_temp_dir(page):
    _load_simple(page)
    md.fill_placements(page, {})
    assert Path("output/debug_placements_fill.txt").exists()
