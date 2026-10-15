# -*- coding: utf-8 -*-
"""Every creative-wizard step the robot has met live, read by the robot's own readers.

Bench (ACL-1433). Fragments of `creative_wizard_steps_2026_10_01.html` are the
wizard blocks ACL-1426 phase A captured read-only on 2026-10-01, one per step
the walk saw, before its forward press. Five step rows were met that day:

- image upload / library pick (SA Doc19, 5edc0e7cca0a):
  Media · Creative setup · Crop · Text · Image generation · Enhancements · Translation
- video library pick (GT, 1125a87e6b51):
  Creative setup · Media · Trim · Crop · Text · Creative generation · Enhancements · Translation
- video library pick, no Creative setup (CO_bnc Trading, 4121938e6dc3):
  Media · Trim · Crop · Text · Creative generation · Enhancements · Translation
- video library pick, no Trim/Crop (CO_bnc Mix, b47bc815bb3c):
  Media · Text · Creative generation · Enhancements · Translation
- video library pick, Text before Crop (CO_bnc FX_Y, cc8fcfa17ae9):
  Creative setup · Media · Text · Crop · Image generation · Enhancements · Translation

The expected step row, current step and buttons are what the robot recorded
live in each capture's `context.json` (`diag.state`), not what the readers
return today. Without Meta's CSS the step list would lie in a column; the
bench lays it out as the row it is on screen.
"""
from __future__ import annotations

import pytest

from src.fb_ads_rpa import meta_dom as md
from tests.bench_support import FIXTURES, WIZARD_ROW_CSS, browser, fragment, offline_page  # noqa: F401

FIXTURE = FIXTURES / "creative_wizard_steps_2026_10_01.html"

IMAGE = ["Media", "Creative setup", "Crop", "Text", "Image generation", "Enhancements", "Translation"]
VIDEO = ["Creative setup", "Media", "Trim", "Crop", "Text", "Creative generation", "Enhancements",
         "Translation"]
PICK = ["Media", "Trim", "Crop", "Text", "Creative generation", "Enhancements", "Translation"]
SHORT = ["Media", "Text", "Creative generation", "Enhancements", "Translation"]
TEXT_FIRST = ["Creative setup", "Media", "Text", "Crop", "Image generation", "Enhancements",
              "Translation"]

#: fragment -> (step row, current step, the forward button the walk must press).
#: `None` = no forward button: the Media step waits for a pick/upload.
CASES = {
    "image_media": (IMAGE, "Media", "Next"),
    "image_creative_setup": (IMAGE, "Creative setup", "Next"),
    "image_crop": (IMAGE, "Crop", "Next"),
    "image_text": (IMAGE, "Text", "Next"),
    "image_image_generation": (IMAGE, "Image generation", "Next"),
    "image_enhancements": (IMAGE, "Enhancements", "Done"),
    "video_trim": (VIDEO, "Trim", "Skip and continue"),
    "video_crop": (VIDEO, "Crop", "Next"),
    "video_text": (VIDEO, "Text", "Next"),
    "video_creative_generation": (VIDEO, "Creative generation", "Next"),
    "video_enhancements": (VIDEO, "Enhancements", "Done"),
    "pick_crop": (PICK, "Crop", "Next"),
    "pick_media_library": (SHORT, "Media", None),
    "pick_enhancements": (SHORT, "Enhancements", "Done"),
    "text_first_text": (TEXT_FIRST, "Text", "Next"),
    "text_first_crop": (TEXT_FIRST, "Crop", "Next"),
}


def _load(page, name: str) -> None:
    page.set_content(WIZARD_ROW_CSS + fragment(FIXTURE, name))


@pytest.mark.parametrize("name", sorted(CASES))
def test_the_wizard_is_seen_open(offline_page, name):
    _load(offline_page, name)
    assert md._creative_wizard_open(offline_page) is True


@pytest.mark.parametrize("name", sorted(CASES))
def test_the_step_row_and_current_step_are_read(offline_page, name):
    steps, current, _ = CASES[name]
    _load(offline_page, name)
    got = md._wizard_media_step(offline_page)
    assert got and got["found"] is True, got
    assert got["steps"] == steps
    assert got["current"] == current
    assert got["disabled"] is False and got["covered"] is False
    # Media behind the open step: Next never leads back to it (ACL-1348).
    assert md._media_step_behind(got) is (steps.index("Media") < steps.index(current))


@pytest.mark.parametrize("name", sorted(CASES))
def test_the_forward_button_is_the_one_the_walk_must_press(offline_page, name):
    *_, forward = CASES[name]
    _load(offline_page, name)
    pt = offline_page.evaluate(md._WIZARD_FORWARD_JS, list(md._WIZARD_ADVANCE_LABELS))
    assert (pt or {}).get("label") == forward, pt


@pytest.mark.parametrize("name", sorted(CASES))
def test_only_trim_reads_as_the_post_media_step(offline_page, name):
    """`_creative_post_media_step` (ACL-1207) — Trim/Crop screen WITH Skip and continue."""
    _load(offline_page, name)
    assert md._creative_post_media_step(offline_page) is (name == "video_trim")


def test_a_forward_button_without_its_label_is_not_pressed(offline_page):
    """Negative control: the reader really keys on the label, not on position."""
    _load(offline_page, "video_trim")
    offline_page.evaluate("""() => {
      for (const b of document.querySelectorAll('[role=button]'))
        if (b.textContent.trim() === 'Skip and continue') b.querySelector('*').textContent = 'Later';
    }""")
    pt = offline_page.evaluate(md._WIZARD_FORWARD_JS, list(md._WIZARD_ADVANCE_LABELS))
    assert pt is None
