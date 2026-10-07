# -*- coding: utf-8 -*-
"""ACL-1669: B97, 07.10 - the library pick does not stick, Crop has no Replace.

Two live runs of the same build on KLD_BFL_B97:

- 3edaf71b0613: every ad ended `selection_state_unavailable` 0.1 s after the
  Media step opened (`ad_creative_reuse`), so reuse refused and nothing was
  uploaded; `url_field_missing` followed on every ad.
- bc4302862ee7: the counter was readable (`before 0, after 1`, the pick took)
  but the Crop step was read while Meta still drew it (loading placeholders,
  no Square/Vertical cards) and answered `crop_replace_button_missing`.

Common cause: B97's Meta draws step bodies late, and the robot read them once,
at once. The fix waits for the counter / the cards, and when the counter never
shows, steps back without clicking so that ad 1 uploads the file once.

The browser tests (bench) run in CI only; the rest need no browser.
"""
from __future__ import annotations

import re

import pytest

from src.fb_ads_rpa import crop_step
from src.fb_ads_rpa import meta_dom as md
from tests.bench_support import FIXTURES, WIZARD_ROW_CSS, browser, fragment, offline_page  # noqa: F401

PICKER = FIXTURES / "wizard_media_picker_2026_10_04.html"
CROP = FIXTURES / "crop_cards_loading_2026_10_07.html"


# ── no browser ────────────────────────────────────────────────────────────

@pytest.fixture
def said(monkeypatch):
    out: list[tuple] = []
    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    monkeypatch.setattr(md._telemetry, "record",
                        lambda step, ok=None, **k: out.append((step, ok, k.get("note"))))
    md._reuse_state["counter_missing"] = False
    return out


def test_counter_that_shows_up_late_is_waited_for(said, monkeypatch):
    reads = iter([None, None, 0])
    monkeypatch.setattr(md, "_library_selected_count", lambda page: next(reads))
    assert md._library_selected_count_waiting(object()) == 0


def test_counter_missing_and_no_tile_marked_steps_back_without_clicks(said, monkeypatch):
    monkeypatch.setattr(md, "_library_selected_count", lambda page: None)
    monkeypatch.setattr(md, "_library_any_tile_marked", lambda page: False)
    monkeypatch.setattr(md, "_library_search", lambda *a: pytest.fail("must not search or click"))
    assert md._try_reuse_library_media(object(), "a.mp4") == md.REUSE_ABSENT
    assert ("ad_creative_reuse", None, "selection_counter_missing") in said
    assert md._reuse_state["counter_missing"] is True
    # the caller then uploads: reuse_before_upload says "carry on"
    monkeypatch.setattr(md, "get_reuse_library_media", lambda: True)
    assert md.reuse_before_upload(object(), "C:/x/a.mp4") is None


@pytest.mark.parametrize("marked", [True, None])
def test_counter_missing_with_a_marked_tile_or_no_grid_is_still_refused(said, monkeypatch, marked):
    monkeypatch.setattr(md, "_library_selected_count", lambda page: None)
    monkeypatch.setattr(md, "_library_any_tile_marked", lambda page: marked)
    assert md._try_reuse_library_media(object(), "a.mp4") == md.REUSE_DIRTY
    assert ("ad_creative_reuse", False, "selection_state_unavailable") in said


def test_later_ads_do_not_ask_the_library_again_when_it_has_no_counter(said, monkeypatch):
    md._reuse_state["counter_missing"] = True
    monkeypatch.setattr(md, "_reuse_upload_decision", lambda *a: pytest.fail("asked again"))
    assert md._reuse_known_copy(object(), "C:/x/a.mp4", True) is None


def test_url_field_after_missing_media_is_named_as_a_consequence():
    md._last_destination.update(why="url_field_missing", diag={})
    line = md.destination_soft_fail("ad2", "Ad 2", no_media=True)
    assert line.startswith("ad2_destination:url_field_missing_no_media - Ad 2:")
    assert "no media" in line and "consequence" in line
    # with media it is a destination problem as before
    assert md.destination_soft_fail("ad2", "Ad 2") == "ad2_destination"
    md._last_destination.update(why="url_field_not_focused", diag={})
    assert md.destination_soft_fail("ad2", "Ad 2", no_media=True) == "ad2_destination"


def test_selection_unreadable_is_a_named_creative_reason(monkeypatch):
    monkeypatch.setattr(md, "last_upload_why", lambda: md.LIBRARY_COUNTER_MISSING)
    line = md.creative_soft_fail("ad", "Ad 1")
    assert line.startswith("ad_creative_upload:library_selection_unreadable - Ad 1:")


def test_crop_waits_for_the_cards_instead_of_naming_a_missing_button(monkeypatch):
    full = {"found": True, "shared": [], "cards": {
        n: {"original": True, "expand": [], "other": [], "radios": [], "replace": True,
            "media": "", "thumbs": []} for n in crop_step.CARDS}}
    empty = {"found": True, "shared": [], "cards": {n: None for n in crop_step.CARDS}}
    states = iter([empty, empty, empty, full])
    last = {"s": empty}

    def read(page, mark=None):
        last["s"] = next(states, last["s"])
        return last["s"]

    monkeypatch.setattr(crop_step, "read", read)
    monkeypatch.setattr(crop_step.time, "sleep", lambda s: None)
    assert crop_step.apply(object(), None) == ""


def test_crop_that_never_draws_its_cards_is_named_by_the_card(monkeypatch):
    empty = {"found": True, "shared": [], "cards": {n: None for n in crop_step.CARDS}}
    monkeypatch.setattr(crop_step, "read", lambda page, mark=None: empty)
    monkeypatch.setattr(crop_step, "CARDS_RENDER_WAIT_S", 0.05)
    monkeypatch.setattr(crop_step.time, "sleep", lambda s: None)
    assert crop_step.apply(object(), None) == "crop_square_card_missing"


# ── bench (offline Chromium, CI) ──────────────────────────────────────────

def _no_footer_counters(html: str) -> str:
    """The 04.10 loaded picker without its 'N selected' footer (what 07.10 showed)."""
    return re.sub(r"<span>\d+ selected</span>", "<span></span>", html)


def test_picker_without_footer_counter_reads_as_unavailable_and_steps_back(offline_page, monkeypatch):
    html = _no_footer_counters(fragment(PICKER, "loaded"))
    assert "selected</span>" not in html.replace('aria-selected', '')
    offline_page.set_content(WIZARD_ROW_CSS + html)
    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    monkeypatch.setattr(md._telemetry, "record", lambda *a, **k: None)
    md._reuse_state["counter_missing"] = False
    assert md._library_selected_count(offline_page) is None
    assert md._library_any_tile_marked(offline_page) is False
    assert md._try_reuse_library_media(offline_page, "sample_media_01.mp4") == md.REUSE_ABSENT


def test_picker_with_its_footer_counter_is_read(offline_page):
    offline_page.set_content(WIZARD_ROW_CSS + fragment(PICKER, "loaded"))
    assert md._library_selected_count(offline_page) == 1


def test_crop_opened_before_its_cards_are_drawn_reads_as_empty(offline_page):
    offline_page.set_content(WIZARD_ROW_CSS + fragment(CROP, "cards_loading"))
    state = crop_step.read(offline_page)
    assert state["found"] is True
    assert not any(crop_step._card(state, n) for n in crop_step.CARDS)
