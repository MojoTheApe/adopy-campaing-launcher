# -*- coding: utf-8 -*-
"""Crop -> Media loop of Meta's media wizard (ACL-1736).

Run 445fa5460ce6 (Facebook_5265, ad 2, library pick): Next on Crop led to Media
(the picked tile was still selected: "1 selected"), then Creative setup -> Media
-> Creative setup -> Crop. The walk then took this Crop for the one where Next
had hung (ACL-1551) and closed the wizard with Close without pressing Next.

Fakes only: no browser, no page.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import creative_pairing as cp  # noqa: E402
from src.fb_ads_rpa import crop_step  # noqa: E402
from src.fb_ads_rpa import meta_dom as md  # noqa: E402

SINGLE = SimpleNamespace(replace_vertical=False, rule="no_marker")


class FakeTrail:
    def __init__(self):
        self.current = ""
        self.looped_back_to = ""
        self.seen = []
        self.exit = None
        self.closed_by = None

    def see(self, page):
        self.current = page.screen()

    def pressed(self, label):
        self.seen.append(label)

    def busy(self, s):
        pass

    def loop_back(self, step):
        if not self.looped_back_to:
            self.looped_back_to = step

    def exited(self, out):
        self.exit = out


class FakePage:
    """Screens in order; Next moves to the next one, Crop's Next 'advances'."""

    def __init__(self, screens):
        self.screens = list(screens)
        self.i = 0
        self.nexts = []
        self.open = True

    def screen(self):
        return self.screens[self.i] if self.i < len(self.screens) else "Enhancements"


def _wire(monkeypatch, page, plan=SINGLE):
    crop_step.set_plan(plan)
    applies = []
    monkeypatch.setattr(crop_step, "apply", lambda p, pl: (applies.append(1), crop_step._tls.__setattr__("done", True), "")[2])
    monkeypatch.setattr(md, "_creative_wizard_open", lambda p: p.open)
    monkeypatch.setattr(md, "_on_crop_step_now", lambda p, name: name == "Crop")

    def forward(p):
        p.nexts.append(p.screen())
        p.i += 1
        if p.i >= len(p.screens):
            p.open = False
        return "Next"

    monkeypatch.setattr(md, "_press_wizard_forward", forward)
    monkeypatch.setattr(md, "_await_crop_advance", lambda p: "advanced")
    monkeypatch.setattr(md, "_WIZARD_STEP_SETTLE_S", 0.0, raising=False)
    left = []
    monkeypatch.setattr(md, "_leave_wizard", lambda p, **kw: (left.append(1), {"closed_by": "Close", "closed": True,
                                                                              "dialog_left": False, "tried": ["Close"]})[1])
    return applies, left


def test_loop_back_through_media_gets_one_more_next(monkeypatch):
    # the live order of 445fa5460ce6, then the wizard goes on after the second Crop
    page = FakePage(["Creative setup", "Crop", "Media", "Creative setup", "Media", "Creative setup", "Crop", "Text"])
    applies, left = _wire(monkeypatch, page)
    trail = FakeTrail()
    assert md._walk_creative_wizard_rounds(page, 16, trail) is True
    assert page.nexts.count("Crop") == 2, "Next is pressed on the second Crop too"
    assert left == [], "no Close: the wizard is walked on"
    assert trail.looped_back_to == "Media"
    assert len(applies) == 2, "the crop is read and set again on the returned Crop"
    assert crop_step.outcome() == ""


def test_second_return_to_crop_stops_honestly_with_looped_back_to(monkeypatch):
    page = FakePage(["Crop", "Media", "Crop", "Media", "Crop", "Text"])
    applies, left = _wire(monkeypatch, page)
    trail = FakeTrail()
    md._walk_creative_wizard_rounds(page, 16, trail)
    assert page.nexts.count("Crop") == 2, "no third Next on Crop"
    assert left == [1]
    assert trail.exit["why"] == "looped_back" and trail.exit["looped_back_to"] == "Media"
    assert crop_step.outcome() == crop_step.WIZARD_STUCK


def test_a_pair_is_not_walked_again_after_the_loop(monkeypatch):
    pair = cp.CropPlan("a.jpg", "b.jpg", True, "pair", "1")
    page = FakePage(["Crop", "Media", "Crop", "Text"])
    applies, left = _wire(monkeypatch, page, plan=pair)
    trail = FakeTrail()
    md._walk_creative_wizard_rounds(page, 16, trail)
    assert page.nexts.count("Crop") == 1 and left == [1]
    assert trail.exit["looped_back_to"] == "Media"
    assert crop_step.outcome() == crop_step.WIZARD_STUCK


def test_a_normal_walk_is_untouched(monkeypatch):
    page = FakePage(["Creative setup", "Crop", "Text", "Image generation", "Enhancements"])
    applies, left = _wire(monkeypatch, page)
    trail = FakeTrail()
    assert md._walk_creative_wizard_rounds(page, 16, trail) is True
    assert trail.looped_back_to == "" and left == [] and len(applies) == 1


def test_trail_names_the_loop_in_the_wizard_row(monkeypatch):
    from src.fb_ads_rpa import wizard_capture as wc

    rows = []
    monkeypatch.setattr(wc._telemetry, "record", lambda step, ok, **kw: rows.append((step, kw)))
    t = wc.Trail()
    t.seen.append({"step": "Crop", "at_s": 0.0, "pressed": []})
    t.loop_back("Media")
    t.loop_back("Creative setup")   # the first one stays
    t.exited({"why": "looped_back", "looped_back_to": "Media", "closed": True, "closed_by": "Close"})
    t.finish(True)
    diag = rows[0][1]["diag"]
    assert diag["looped_back_to"] == "Media" and diag["exit"]["looped_back_to"] == "Media"


def test_crop_then_crop_without_a_step_before_is_still_next_did_not_advance(monkeypatch):
    """F1: an 'advanced' from one empty read, then the same Crop - no second Next."""
    page = FakePage(["Crop", "Crop", "Text"])
    applies, left = _wire(monkeypatch, page)
    trail = FakeTrail()
    md._walk_creative_wizard_rounds(page, 16, trail)
    assert page.nexts.count("Crop") == 1 and left == [1]
    assert trail.exit["why"] == "next_did_not_advance"
    assert crop_step.outcome() == crop_step.WIZARD_STUCK


def test_forward_stuck_set_before_the_loop_blocks_the_retry(monkeypatch):
    page = FakePage(["Crop", "Media", "Crop", "Text"])
    applies, left = _wire(monkeypatch, page)
    crop_step.note_forward_stuck()
    trail = FakeTrail()
    md._walk_creative_wizard_rounds(page, 16, trail)
    assert left == [1] and trail.exit["why"] == "next_did_not_advance"
    assert page.nexts.count("Crop") == 0, "ACL-1551: Next is not pressed on a Crop that already hung"
    assert crop_step.outcome() == crop_step.WIZARD_STUCK
