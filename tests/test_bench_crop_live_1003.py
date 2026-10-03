# -*- coding: utf-8 -*-
"""Crop step on the live 03.10 screens: Square check and the in-wizard picker (ACL-1533).

Live runs on Facebook_1443, 2026-10-03 (d54d68303e57, f35a2b31f7d8,
2e98a75f252d, a39407b72507): every post/story pair ended "do not publish".

1. `crop_square_changed`: the video cards' media is a `blob:` URL that Meta
   re-mints whenever the Crop step is drawn again. After Replace both cards had
   a new `blob:` src, the Square card still on Original with the post (1:1
   still disabled, no thumbnail ids on either card) - the src comparison read
   that as "Square changed".
2. `crop_replace_picker_not_closed`: the in-wizard "Replace media for
   Stories, Status, Reels" has the step row (Crop is `aria-current=step`, a
   no-op), the header's Close/Save and the picker's own Save after the
   "Search media" box. The row's Crop was pressed and nothing happened; the
   picker's Save is what brought Crop back after a pick live.

Fixtures (scrubbed by `scripts/capture_to_fixture.py`; media files are
`sample_media_NN`, videos replaced by `<i>`):
`crop_step_pair_video_2026_10_03.html` (2e98a75f252d/06_wizard_crop) and
`crop_replace_in_wizard_picker_2026_10_03.html` (2e98a75f252d/07_wizard_crop_replace).
The page behaviour (Replace opens the picker, Save returns to Crop, blob
re-minting) is wired by the test the way it went live. Offline Chrome, headless.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import creative_pairing as cp  # noqa: E402
from src.fb_ads_rpa import crop_step  # noqa: E402
from src.fb_ads_rpa import meta_dom as md  # noqa: E402
from src.fb_ads_rpa import page_capture  # noqa: E402
from src.fb_ads_rpa import run_telemetry as rt  # noqa: E402
from tests.bench_support import FIXTURES, browser, fragment  # noqa: E402,F401

CROP = fragment(FIXTURES / "crop_step_pair_video_2026_10_03.html", "crop_step")
PICKER = fragment(FIXTURES / "crop_replace_in_wizard_picker_2026_10_03.html", "in_wizard_picker")

#: In the picker fixture sample_media_01.mp4 is the 1080 x 1920 tile (the story),
#: sample_media_02.mp4 the 1080 x 1080 one (the post).
PAIR = cp.CropPlan("sample_media_02.mp4", "sample_media_01.mp4", True, "pair", "2403")
MISSING = cp.CropPlan("sample_media_02.mp4", "sample_media_09.mp4", True, "pair", "2403")
STORY_THUMB = "111111_2222222222_3333333333_n"

#: Meta's behaviour around the captured markup, as seen live.
_WIRE = r"""
(args) => {
  const norm = (s) => (s || '').replace(/[​-‍﻿]/g, '').replace(/\s+/g, ' ').trim();
  window.pressed = []; window.mint = 0;
  const host = document.getElementById('host');
  const cardOf = (name) => {
    const h = [...host.querySelectorAll('[role=heading]')].find(e => norm(e.textContent) === name);
    if (!h) return null;
    const other = [...host.querySelectorAll('[role=heading]')]
      .find(e => norm(e.textContent) === (name === 'Square' ? 'Vertical' : 'Square'));
    let best = null;
    for (let n = h.parentElement; n && n !== host; n = n.parentElement) {
      if (other && n.contains(other)) break;
      if (n.querySelector('input[type=radio]')) best = n;
    }
    return best;
  };
  // The captured cards with a video each, its src a blob: URL minted per render.
  const paintCrop = (story) => {
    host.innerHTML = args.crop;
    for (const name of ['Square', 'Vertical']) {
      const card = cardOf(name);
      const v = document.createElement('video');
      v.style.cssText = 'width:60px;height:60px;display:block';
      v.setAttribute('src', 'blob:https://adsmanager.invalid/render-' + (++window.mint));
      card.prepend(v);
      if (name === 'Vertical' && story) {
        // Live `after`: the story on Vertical - 9:16 is its own shape now.
        const r = [...card.querySelectorAll('input[type=radio]')].find(i => /9:16/.test(norm(i.closest('label') ? i.closest('label').textContent : i.parentElement.textContent)));
        if (r) r.disabled = true;
      }
      if (name === 'Square' && story && args.square_story) {
        const img = document.createElement('img');
        img.style.cssText = 'width:10px;height:10px';
        img.setAttribute('src', 'https://x.invalid/' + args.thumb + '.jpg');
        card.prepend(img);
      }
    }
  };
  const paintPicker = () => {
    host.innerHTML = args.picker;
    for (const t of host.querySelectorAll('[aria-label=grid] [role=button]')) {
      t.style.cssText = 'width:96px;height:40px;display:inline-block';
      if (norm(t.textContent).startsWith(args.story_tile)) {
        const img = document.createElement('img');
        img.style.cssText = 'width:10px;height:10px';
        img.setAttribute('src', 'https://x.invalid/' + args.thumb + '.jpg');
        t.prepend(img);
      }
    }
  };
  paintCrop(false);
  document.addEventListener('click', (e) => {
    const b = e.target.closest('[role=button],button');
    if (!b) return;
    const label = norm(b.textContent);
    const search = host.querySelector('input[placeholder="Search media"]');
    if (!search) {                                   // on Crop
      if (label === 'Replace' && cardOf('Vertical').contains(b)) { window.pressed.push('Replace'); paintPicker(); }
      else if (label) window.pressed.push(label);
      return;
    }
    if (b.closest('[aria-label=grid]')) { b.setAttribute('aria-pressed', 'true'); return; }
    if (label === 'Save' && (search.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING)) {
      window.pressed.push('own Save');
      if (args.own_save === 'stuck') return;
      const picked = host.querySelector('[aria-label=grid] [aria-pressed=true]');
      // 'noop': Save returns to Crop, but Meta left the post on Vertical
      // (only the blob: src re-minted, 9:16 still enabled).
      paintCrop((!!picked && args.own_save !== 'noop') || args.own_save === 'changes_vertical');
      return;
    }
    if (label === 'Save') { window.pressed.push('header Save'); return; }
    if (label === 'Close') { window.pressed.push('Close'); host.innerHTML = ''; return; }
    if (b.getAttribute('aria-current') === 'step') { window.pressed.push('Crop row'); return; }
    window.pressed.push(label);
  }, true);
}
"""


@pytest.fixture
def page(browser, monkeypatch, tmp_path):  # noqa: F811
    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    monkeypatch.setattr(md, "_LIBRARY_APPEAR_TRIES", 1)
    monkeypatch.setattr(rt, "TELEMETRY_PATH", tmp_path / "telemetry.jsonl")
    monkeypatch.setattr(page_capture, "_root", lambda: tmp_path / "rpa_pages")
    monkeypatch.delenv(crop_step.REPLACE_SETTING, raising=False)
    crop_step.set_plan(None)
    ctx = browser.new_context(viewport={"width": 1600, "height": 1000})
    ctx.route("**/*", lambda route: route.abort())
    p = ctx.new_page()
    try:
        yield p
    finally:
        ctx.close()
        crop_step.set_plan(None)


def _live(page, *, own_save="back", square_story=False):
    page.set_content("<!doctype html><html><body><div id='host'></div></body></html>")
    page.evaluate(_WIRE, {"crop": CROP, "picker": PICKER, "own_save": own_save, "thumb": STORY_THUMB,
                          "story_tile": "sample_media_01.mp4", "square_story": square_story})


def _rows(tmp_path):
    f = tmp_path / "telemetry.jsonl"
    rows = [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines() if x.strip()] if f.exists() else []
    return [r for r in rows if r.get("step") == "ad_creative_crop"]


# ── 1. the Square check ───────────────────────────────────────────────────

def test_the_captured_cards_carry_blob_media_and_no_thumbnail_ids(page):
    _live(page)
    state = crop_step.read(page)
    assert state["found"]
    for name in crop_step.CARDS:
        card = state["cards"][name]
        assert card["original"] is True
        assert card["media"].startswith("blob:")
        assert card["thumbs"] == []
        assert crop_step.media_key(card["media"]) == ""


def test_square_re_rendered_without_ids_is_not_changed(page):
    # Live `after` of every 03.10 pair: both cards re-minted, Square still
    # Original with 1:1 disabled, no ids; the picked tile had one.
    _live(page)
    before = crop_step.read(page)
    page.evaluate("(a) => { const v = [...document.querySelectorAll('video')];"
                  " v.forEach((x, i) => x.setAttribute('src', 'blob:https://adsmanager.invalid/after-' + i)); }", None)
    page.evaluate(r"""() => { for (const i of document.querySelectorAll('input[type=radio]'))
        if (/9:16/.test(i.closest('label') ? i.closest('label').textContent : i.parentElement.textContent)) i.disabled = true; }""")
    after = crop_step.read(page)
    assert after["cards"]["Square"]["media"] != before["cards"]["Square"]["media"]
    assert crop_step.square_changed(before, after, [STORY_THUMB]) == ""
    assert crop_step.readback(after, PAIR, before, [STORY_THUMB]) == ""


def test_square_showing_the_picked_story_id_is_changed(page):
    _live(page)
    before = crop_step.read(page)
    page.evaluate(r"""(t) => { const v = document.querySelectorAll('video');
        v.forEach((x, i) => x.setAttribute('src', 'blob:https://adsmanager.invalid/after-' + i));
        const img = document.createElement('img'); img.style.cssText = 'width:10px;height:10px';
        img.setAttribute('src', 'https://x.invalid/' + t + '.jpg'); v[1].parentElement.prepend(img); }""", STORY_THUMB)
    after = crop_step.read(page)
    assert STORY_THUMB in after["cards"]["Square"]["thumbs"]
    assert crop_step.square_changed(before, after, [STORY_THUMB]) == "picked_thumb"
    assert crop_step.readback(after, PAIR, before, [STORY_THUMB]) == "crop_square_changed"


def test_other_positive_evidence_still_counts():
    def st(sq_media, sq_radios, thumbs=()):
        return {"found": True, "cards": {
            "Square": {"original": True, "expand": [], "other": [], "media": sq_media,
                       "thumbs": list(thumbs), "radios": sq_radios},
            "Vertical": {"original": True, "expand": [], "other": [], "media": "v", "thumbs": []}}}
    square = [{"label": "Original", "disabled": False}, {"label": "1:1 (recommended)", "disabled": True}]
    taller = [{"label": "Original", "disabled": False}, {"label": "1:1 (recommended)", "disabled": False}]
    # A CDN file changed (the signed query alone is not a change).
    assert crop_step.square_changed(st("https://x.invalid/a.jpg?oh=1", square),
                                     st("https://x.invalid/b.jpg?oh=1", square)) == "media"
    assert crop_step.square_changed(st("https://x.invalid/a.jpg?oh=1", square),
                                     st("https://x.invalid/a.jpg?oh=2", square)) == ""
    # Another shape on Square (1:1 no longer its own ratio).
    assert crop_step.square_changed(st("blob:1", square), st("blob:2", taller)) == "ratios"
    # Square's ids replaced by others.
    assert crop_step.square_changed(st("", square, ["1_2_3_n"]), st("", square, ["4_5_6_n"])) == "thumbs"


# ── 2. the in-wizard picker ───────────────────────────────────────────────

def test_the_captured_picker_is_in_the_wizard_and_its_way_back_is_its_own_save(page):
    _live(page)
    page.evaluate("(p) => { document.getElementById('host').innerHTML = p; }", PICKER)
    got = crop_step._picker(page)
    assert got["open"] and got["scoped"] and got["in_wizard"] and got["own_save"]
    labels = [b["label"] for b in got["buttons"]]
    assert labels.count("Save") == 2 and "Cancel" not in labels and "Next" not in labels
    for want in ("confirm", "cancel"):
        pt = crop_step._picker(page, want)["point"]
        assert pt["label"] == "Save"
        hit = page.evaluate("([x, y]) => { const b = document.elementFromPoint(x, y).closest('[role=button],button');"
                            " const s = document.querySelector('input[placeholder=\"Search media\"]');"
                            " return !!(s.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING); }",
                            [pt["x"], pt["y"]])
        assert hit is True                                # the footer Save, not the header's


def test_the_picker_slot_reading_next_is_never_pressed(page):
    _live(page)
    page.evaluate("(p) => { const h = document.getElementById('host'); h.innerHTML = p;"
                  " const s = [...h.querySelectorAll('[role=button]')].filter(b => b.textContent.trim().startsWith('Save')).pop();"
                  " s.textContent = 'Next'; }", PICKER)
    assert crop_step._picker(page, "confirm")["point"] is None
    assert crop_step._picker(page, "cancel")["point"] is None


def test_a_pair_on_the_live_screens_gets_the_story_and_leaves_through_save(page, tmp_path):
    _live(page)
    crop_step.set_plan(PAIR)
    assert crop_step.apply(page, PAIR) == ""
    assert page.evaluate("window.pressed") == ["Replace", "own Save"]
    assert crop_step.warning() == ""
    row = _rows(tmp_path)[-1]
    assert row["diag"]["confirm"] == "Save" and row["diag"]["picked_thumbs"] == [STORY_THUMB]
    # No ids on the cards: as planned, said as unconfirmed (not blocked).
    assert (row["ok"], row["note"]) == (None, "as_planned_thumb_unconfirmed")


def test_a_pair_whose_square_shows_the_story_still_blocks(page):
    _live(page, square_story=True)
    crop_step.set_plan(PAIR)
    assert crop_step.apply(page, PAIR) == "crop_square_changed"


def test_story_not_in_the_library_leaves_through_save_never_the_row_or_close(page, tmp_path):
    _live(page)
    crop_step.set_plan(MISSING)
    assert crop_step.apply(page, MISSING) == ""
    assert crop_step.warning() == crop_step.NOT_IN_LIBRARY
    assert page.evaluate("window.pressed") == ["Replace", "own Save"]
    row = _rows(tmp_path)[-1]
    assert row["diag"]["tiles"] == 0 and row["diag"]["picker_back"] == "Save"
    assert (row["ok"], row["note"]) == (None, "story_not_placed:not_in_library")
    assert crop_step.read(page)["found"]


def test_save_without_a_pick_that_changes_vertical_blocks(page):
    _live(page, own_save="changes_vertical")
    crop_step.set_plan(MISSING)
    assert crop_step.apply(page, MISSING) == "crop_vertical_changed_without_story"
    assert crop_step.warning() == ""


def test_a_picker_that_save_does_not_leave_blocks(page):
    _live(page, own_save="stuck")
    crop_step.set_plan(MISSING)
    assert crop_step.apply(page, MISSING) == "crop_replace_picker_not_closed"
    assert page.evaluate("window.pressed") == ["Replace", "own Save", "Close"]


# ── review round 2: the Vertical side needs evidence too ─────────────────

def test_a_replace_that_left_the_post_on_vertical_blocks(page, tmp_path):
    # Save after a pick returns to Crop, but Vertical shows the post: only its
    # blob: src is new. Not "as planned".
    _live(page, own_save="noop")
    crop_step.set_plan(PAIR)
    assert crop_step.apply(page, PAIR) == "crop_vertical_replace_unconfirmed"
    assert page.evaluate("window.pressed") == ["Replace", "own Save"]
    row = _rows(tmp_path)[-1]
    assert (row["ok"], row["note"]) == (False, "crop_vertical_replace_unconfirmed")


def test_the_live_evidence_of_the_story_on_vertical_passes(page):
    # Live `after` (d54d68303e57, 2e98a75f252d): 9:16 disabled on Vertical.
    _live(page)
    before = crop_step.read(page)
    page.evaluate(r"""() => { document.querySelectorAll('video').forEach((x, i) =>
        x.setAttribute('src', 'blob:https://adsmanager.invalid/after-' + i));
      for (const i of document.querySelectorAll('input[type=radio]'))
        if (/9:16/.test(i.closest('label') ? i.closest('label').textContent : i.parentElement.textContent)) i.disabled = true; }""")
    after = crop_step.read(page)
    assert crop_step.vertical_replaced(before, after, [STORY_THUMB]) == "ratios"
    # Only the blob re-minted: no evidence.
    page.evaluate(r"""() => { for (const i of document.querySelectorAll('input[type=radio]')) i.disabled = false;
      document.querySelectorAll('input[type=radio]').forEach(i => { if (/1:1/.test(i.closest('label') ? i.closest('label').textContent : '')) i.disabled = true; }); }""")
    churn = crop_step.read(page)
    assert crop_step.vertical_replaced(before, churn, [STORY_THUMB]) == ""
    assert crop_step.readback(churn, PAIR, before, [STORY_THUMB]) == "crop_vertical_replace_unconfirmed"


def test_vertical_evidence_by_ids_and_stable_media():
    def st(v_media, v_thumbs=()):
        return {"found": True, "cards": {
            "Square": {"original": True, "expand": [], "other": [], "media": "blob:s", "thumbs": []},
            "Vertical": {"original": True, "expand": [], "other": [], "media": v_media,
                         "thumbs": list(v_thumbs)}}}
    assert crop_step.vertical_replaced(st("blob:1"), st("blob:2", [STORY_THUMB]), [STORY_THUMB]) == "picked_thumb"
    assert crop_step.readback(st("blob:2", [STORY_THUMB]), PAIR, st("blob:1"), [STORY_THUMB]) == ""
    assert crop_step.vertical_replaced(st("https://x.invalid/a.jpg?x=1"), st("https://x.invalid/b.jpg?x=1")) == "media"
    assert crop_step.vertical_replaced(st("https://x.invalid/a.jpg?x=1"), st("https://x.invalid/a.jpg?x=2")) == ""
    assert crop_step.readback(st("https://x.invalid/a.jpg?x=1"), PAIR, st("https://x.invalid/a.jpg?x=2"),
                              []) == "crop_vertical_replace_unconfirmed"


def test_the_new_reason_has_an_english_line():
    from src.fb_ads_rpa import simple_create
    assert simple_create.creative_why_text("crop_vertical_replace_unconfirmed").startswith(
        "The story file could not be confirmed on the Vertical crop")
