# -*- coding: utf-8 -*-
"""Crop step: Original on both cards, Meta AI expansion off, story on Vertical (ACL-1426).

Fixtures cut from the owner's capture run 2026-10-01 (phase A):
`crop_step_video_simple_create_2026_10_01.html` (single-screen editor, video,
Original preselected on both cards) and `crop_step_image_classic_2026_10_01.html`
(classic editor, image, Vertical preselected on 9:16). The expansion checkbox
and the Replace picker were not on any captured screen, so they are synthetic
here, shaped like the captured cards (label > native radio, role=heading
"Square"/"Vertical", Replace button) and the media library (search box
"Search media", role=button tiles captioned with the file name, "N selected").
Offline Chrome, headless.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import creative_pairing as cp  # noqa: E402
from src.fb_ads_rpa import crop_step  # noqa: E402
from src.fb_ads_rpa import meta_dom as md  # noqa: E402
from src.fb_ads_rpa import page_capture  # noqa: E402
from src.fb_ads_rpa import run_telemetry as rt  # noqa: E402

FIX = ROOT / "tests" / "fixtures" / "ads_manager"
SINGLE = cp.CropPlan("4_es_2403_post.mp4", "4_es_2403_post.mp4", False, "post_only", "2403")
PAIR = cp.CropPlan("4_es_2403_post.mp4", "4_es_2403_story.mp4", True, "pair", "2403")


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
    monkeypatch.setattr(md, "wait_editor_saved", lambda *a, **k: True)
    monkeypatch.setattr(rt, "TELEMETRY_PATH", tmp_path / "telemetry.jsonl")
    monkeypatch.setattr(page_capture, "_root", lambda: tmp_path / "rpa_pages")
    monkeypatch.delenv(crop_step.REPLACE_SETTING, raising=False)
    page_capture._taken.clear()
    page_capture._quota_taken.clear()
    crop_step.set_plan(None)
    ctx = browser.new_context(viewport={"width": 1400, "height": 1000})
    p = ctx.new_page()
    p.route("**/*", lambda route: route.abort())
    try:
        yield p
    finally:
        ctx.close()
        crop_step.set_plan(None)
        page_capture._quota_taken.clear()


def _rows(tmp_path, step):
    f = tmp_path / "telemetry.jsonl"
    if not f.exists():
        return []
    return [r for r in (json.loads(x) for x in f.read_text(encoding="utf-8").splitlines() if x.strip())
            if r.get("step") == step]


def _fixture(page, name):
    page.set_content("<!doctype html><html><body>" + (FIX / name).read_text(encoding="utf-8")
                     + "</body></html>")


# ── the captured screens ─────────────────────────────────────────────────

def test_video_single_screen_both_cards_already_original(page, tmp_path):
    _fixture(page, "crop_step_video_simple_create_2026_10_01.html")
    state = crop_step.read(page)
    assert state["found"]
    assert state["cards"]["Square"]["original"] is True
    assert state["cards"]["Vertical"]["original"] is True
    assert state["cards"]["Vertical"]["replace"] is True
    assert crop_step.apply(page, SINGLE) == ""
    row = _rows(tmp_path, "ad_creative_crop")[-1]
    assert row["ok"] is True and row["diag"]["clicks"] == []


def test_image_classic_vertical_moved_from_9_16_to_original(page, tmp_path):
    _fixture(page, "crop_step_image_classic_2026_10_01.html")
    before = crop_step.read(page)
    assert before["cards"]["Square"]["original"] is True
    assert before["cards"]["Vertical"]["original"] is False      # Meta default 9:16
    assert crop_step.apply(page, SINGLE) == ""
    after = crop_step.read(page)
    assert after["cards"]["Vertical"]["original"] is True
    assert _rows(tmp_path, "ad_creative_crop")[-1]["diag"]["clicks"] == ["Vertical:original"]


def test_readback_names_what_is_wrong():
    ok = {"found": True, "cards": {n: {"original": True, "expand": [], "media": n}
                                   for n in crop_step.CARDS}}
    assert crop_step.readback(ok, SINGLE) == ""
    bad = json.loads(json.dumps(ok))
    bad["cards"]["Vertical"]["original"] = False
    assert crop_step.readback(bad, SINGLE) == "crop_vertical_not_original"
    bad = json.loads(json.dumps(ok))
    bad["cards"]["Square"]["expand"] = [{"checked": True}]
    assert crop_step.readback(bad, SINGLE) == "crop_square_meta_ai_expand_on"
    assert crop_step.readback(ok, PAIR, ok) == "crop_vertical_not_replaced"
    assert crop_step.readback({"found": False}, SINGLE) == "crop_step_not_read"


# ── synthetic: expansion checkbox and Replace ─────────────────────────────

_CARD = """
<div class="card"><div role="heading" aria-level="4">%(name)s</div>
  <img id="img_%(name)s" src="https://x.invalid/%(src)s" style="width:60px;height:60px">
  <div role="radiogroup">
    <label><input type="radio" name="r_%(name)s" %(orig)s disabled_orig> Original</label>
    <label><input type="radio" name="r_%(name)s" %(ratio)s> %(ratio_label)s</label>
  </div>
  <label><input type="checkbox" id="exp_%(name)s" %(expand)s> Crop and expand (Meta AI)</label>
  <button id="crop_%(name)s">Crop</button>
  <button id="replace_%(name)s" onclick="openPicker()">Replace</button>
</div>
"""

_PAGE = """<!doctype html><html><body>
<div role="dialog" id="wiz">
  <div role="heading" aria-level="3">Select media crop for placements</div>
  %(cards)s
  %(extra)s
  <button>Cancel</button><button id="next" onclick="window.pressed.push('Next')">Next</button>
</div>
<script>
  window.pressed = [];
  const THUMBS = %(thumbs)s;
  function openPicker() {
    const d = document.createElement('div');
    if (%(picker_dialog)s) d.setAttribute('role', 'dialog');
    d.id = 'picker';
    d.innerHTML = '<input placeholder="Search media">'
      + '<div id="count">0 selected</div>'
      + %(tiles)s.map(n => '<div role="button" class="tile" style="width:90px;height:90px" '
          + 'onclick="this.setAttribute(\\'aria-pressed\\', \\'true\\');'
          + 'document.getElementById(\\'count\\').textContent=\\'1 selected\\'">'
          + (THUMBS[n] ? '<img src="https://x.invalid/' + THUMBS[n] + '.jpg" style="width:20px;height:20px">' : '')
          + '<div><div>' + n + '</div></div></div>').join('')
      + '<button onclick="document.getElementById(\\'picker\\').remove()">Cancel</button>'
      + '<button onclick="done()">Done</button>';
    document.body.appendChild(d);
  }
  function done() {
    const t = [...document.querySelectorAll('#picker .tile')].find(x => x.getAttribute('aria-pressed') === 'true');
    const img = t && t.querySelector('img');
    if (t) document.getElementById('img_Vertical').src = window.forceSrc
      || (img ? img.getAttribute('src') : 'https://x.invalid/' + t.textContent);
    document.getElementById('picker').remove();
  }
</script>
</body></html>"""


def _synthetic(page, *, expand=("", ""), vertical_ratio_checked=True, tiles=("4_es_2403_story.mp4",),
               original_disabled=False, extra="", picker_dialog=True, thumbs=None, expand_label=None):
    cards = []
    for name, exp in zip(("Square", "Vertical"), expand):
        nine = name == "Vertical" and vertical_ratio_checked
        cards.append((_CARD % {
            "name": name, "src": "4_es_2403_post.mp4",
            "orig": "" if nine else "checked", "ratio": "checked" if nine else "",
            "ratio_label": "9:16 (recommended)" if name == "Vertical" else "1:1 (recommended)",
            "expand": exp,
        }).replace("disabled_orig", "disabled" if original_disabled and name == "Vertical" else ""))
    html = "".join(cards)
    if expand_label:
        html = html.replace("Crop and expand (Meta AI)", expand_label)
    page.set_content(_PAGE % {"cards": html, "tiles": json.dumps(list(tiles)), "extra": extra,
                              "picker_dialog": "true" if picker_dialog else "false",
                              "thumbs": json.dumps(thumbs or {})})


def test_meta_ai_expansion_is_switched_off_on_both_cards(page):
    _synthetic(page, expand=("checked", "checked"))
    assert [c["checked"] for c in crop_step.read(page)["cards"]["Square"]["expand"]] == [True]
    assert crop_step.apply(page, SINGLE) == ""
    state = crop_step.read(page)
    for name in crop_step.CARDS:
        assert state["cards"][name]["original"] is True
        assert not any(c["checked"] for c in state["cards"][name]["expand"])
    assert page.evaluate("window.pressed") == []          # Next is the walk's, not ours


def test_a_pair_gets_the_story_on_vertical_through_replace(page, tmp_path):
    _synthetic(page)
    rt.set_current_run("run-1426b", "CAMP")
    try:
        assert crop_step.apply(page, PAIR) == ""
    finally:
        rt.clear_current_run()
    state = crop_step.read(page)
    assert state["cards"]["Vertical"]["media"].endswith("4_es_2403_story.mp4")
    assert state["cards"]["Square"]["media"].endswith("4_es_2403_post.mp4")
    assert state["cards"]["Vertical"]["original"] is True
    assert page.evaluate("!document.getElementById('picker')")
    diag = _rows(tmp_path, "ad_creative_crop")[-1]["diag"]
    assert "Vertical:replace" in diag["clicks"] and diag["confirm"] == "Done"
    # The Replace picker is captured for the next round of fixtures.
    assert any(d.name.endswith("wizard_crop_replace")
               for d in (tmp_path / "rpa_pages" / "run-1426b").iterdir())


def test_a_story_missing_from_the_library_is_a_warning_and_the_rest_is_still_set(page, tmp_path):
    # ACL-1476: Replace skipped, Original on Vertical (was 9:16) and the
    # expansion off still applied and read back; the post stays on Vertical.
    _synthetic(page, tiles=("other.mp4",), expand=("checked", ""))
    crop_step.set_plan(PAIR)
    assert crop_step.apply(page, PAIR) == ""
    assert crop_step.warning() == crop_step.NOT_IN_LIBRARY
    assert page.evaluate("!document.getElementById('picker')")
    state = crop_step.read(page)
    assert state["cards"]["Vertical"]["original"] is True
    assert not any(c["checked"] for c in state["cards"]["Square"]["expand"])
    assert state["cards"]["Vertical"]["media"].endswith("4_es_2403_post.mp4")
    row = _rows(tmp_path, "ad_creative_crop")[-1]
    assert (row["ok"], row["note"]) == (None, "story_not_placed:not_in_library")
    assert row["diag"]["picker_back"] == "Cancel"
    assert crop_step.outcome() == ""


def test_replace_switched_off_blocks_a_pair(page, monkeypatch):
    monkeypatch.setenv(crop_step.REPLACE_SETTING, "0")
    _synthetic(page, vertical_ratio_checked=False)
    assert crop_step.apply(page, PAIR) == "crop_replace_off"


def test_original_that_cannot_be_chosen_is_a_blocking_reason(page):
    _synthetic(page, original_disabled=True)
    assert crop_step.apply(page, SINGLE) == "crop_vertical_not_original"


# ── review PR #440: M2 (expansion fails closed), M3 (story identity), L1 ──

STORY_THUMB = "111111_2222222222_3333333333_n"
POST_THUMB = "444444_5555555555_6666666666_n"


def test_an_expansion_box_shared_by_both_cards_is_switched_off(page, tmp_path):
    _synthetic(page, extra='<label><input type="checkbox" id="exp_shared" checked> AI expansion</label>')
    state = crop_step.read(page)
    assert state["shared"] == [{"label": "AI expansion", "checked": True, "expand": True}]
    assert crop_step.apply(page, SINGLE) == ""
    assert page.evaluate("document.getElementById('exp_shared').checked") is False
    assert "shared:expand_off" in _rows(tmp_path, "ad_creative_crop")[-1]["diag"]["clicks"]


def test_an_expansion_box_under_another_label_in_a_card_is_recognised(page):
    _synthetic(page, expand=("", "checked"), expand_label="AI expansion")
    assert crop_step.read(page)["cards"]["Vertical"]["expand"][0]["checked"] is True
    assert crop_step.apply(page, SINGLE) == ""
    assert page.evaluate("document.getElementById('exp_Vertical').checked") is False


def test_an_unknown_checked_toggle_on_the_step_blocks(page):
    _synthetic(page, extra='<div role="switch" aria-checked="true" aria-label="Optimise for placements" '
                           'style="width:20px;height:20px"></div>')
    assert crop_step.apply(page, SINGLE) == "crop_unknown_toggle_on"
    # ... and in a card as well (a localized expansion label is unknown, so it blocks).
    _synthetic(page, expand=("checked", ""), expand_label="Recortar y ampliar")
    assert crop_step.apply(page, SINGLE) == "crop_unknown_toggle_on"


def test_unchecked_unknown_toggles_do_not_block(page):
    _synthetic(page, extra='<label><input type="checkbox"> Something else</label>')
    assert crop_step.apply(page, SINGLE) == ""


def test_replace_is_proven_by_the_picked_tiles_thumbnail(page, tmp_path):
    _synthetic(page, thumbs={"4_es_2403_story.mp4": STORY_THUMB})
    assert crop_step.apply(page, PAIR) == ""
    row = _rows(tmp_path, "ad_creative_crop")[-1]
    assert row["ok"] is True and row["note"] == "as_planned"
    assert row["diag"]["thumb"] == "match"
    assert row["diag"]["picked_thumbs"] == [STORY_THUMB]


def test_a_vertical_card_showing_another_file_than_the_picked_tile_blocks(page, tmp_path):
    _synthetic(page, thumbs={"4_es_2403_story.mp4": STORY_THUMB})
    # Meta put another tile (the post) on Vertical: the src changed, but not
    # to the story.
    page.evaluate(f"window.forceSrc = 'https://x.invalid/{POST_THUMB}.jpg'")
    assert crop_step.apply(page, PAIR) == "crop_vertical_not_story"
    assert _rows(tmp_path, "ad_creative_crop")[-1]["diag"]["thumb"] == "mismatch"


def test_a_replace_without_thumbnails_is_a_warning_not_a_silent_success(page, tmp_path):
    _synthetic(page)
    assert crop_step.apply(page, PAIR) == ""
    row = _rows(tmp_path, "ad_creative_crop")[-1]
    assert row["ok"] is None and row["note"] == "as_planned_thumb_unconfirmed"
    assert row["diag"]["thumb"] == "unconfirmed"


def test_a_picker_without_its_own_dialog_is_refused_and_the_wizard_next_untouched(page):
    _synthetic(page, picker_dialog=False)
    assert crop_step.apply(page, PAIR) == "crop_replace_picker_unscoped"
    assert page.evaluate("window.pressed") == []
    assert crop_step._picker(page, "confirm").get("point") is None


# ── inside the wizard walk ────────────────────────────────────────────────

_WALK = """<!doctype html><html><body>
<div role="dialog" id="wiz">
  <div role="list" id="row"></div>
  <div id="body"></div>
  <button>Cancel</button><button id="fwd" onclick="advance()">Next</button>
</div>
<script>
  const steps = ['Media', 'Trim', 'Crop', 'Text', 'Enhancements'];
  let at = 1; window.clicks = [];
  const crop = %(crop)s;
  function paint() {
    document.getElementById('row').innerHTML = steps.map((s, i) => '<div role="listitem"><div role="button" '
      + (i === at && %(row)s ? 'aria-current="step"' : '') + '><div>' + s + '</div></div></div>').join('');
    document.getElementById('body').innerHTML = steps[at] === 'Crop' ? crop : '<div role="heading">' + steps[at] + '</div>';
    document.getElementById('fwd').textContent = steps[at] === 'Enhancements' ? 'Done' : 'Next';
  }
  function advance() {
    window.clicks.push(steps[at] + ':' + document.getElementById('fwd').textContent);
    at += 1;
    if (at >= steps.length) { document.getElementById('wiz').remove(); window.clicks.push('close'); return; }
    paint();
  }
  paint();
</script></body></html>"""


def _walk_page(page, *, original_disabled=False, row=True):
    cards = "".join((_CARD % {
        "name": n, "src": "a.mp4", "orig": "" if n == "Vertical" else "checked",
        "ratio": "checked" if n == "Vertical" else "",
        "ratio_label": "9:16 (recommended)" if n == "Vertical" else "1:1",
        "expand": "",
    }).replace("disabled_orig", "disabled" if original_disabled and n == "Vertical" else "")
        for n in ("Square", "Vertical"))
    crop_html = '<div role="heading" aria-level="3">Select media crop for placements</div>' + cards
    page.set_content(_WALK % {"crop": json.dumps(crop_html), "row": "true" if row else "false"})


def test_the_walk_sets_original_on_crop_before_next(page, monkeypatch):
    monkeypatch.setattr(md, "_WIZARD_STEP_SETTLE_S", 0.3)
    _walk_page(page)
    crop_step.set_plan(SINGLE)
    seen = {}
    real = crop_step.apply

    def spy(p, plan):
        why = real(p, plan)
        seen["vertical"] = crop_step.read(p)["cards"]["Vertical"]["original"]
        return why

    monkeypatch.setattr(crop_step, "apply", spy)
    assert md._walk_creative_wizard(page) is True
    assert seen == {"vertical": True}
    assert page.evaluate("window.clicks") == ["Trim:Next", "Crop:Next", "Text:Next", "Enhancements:Done", "close"]
    assert crop_step.outcome() == ""


def test_a_crop_mismatch_is_handed_to_the_caller_and_the_wizard_still_closes(page, monkeypatch):
    monkeypatch.setattr(md, "_WIZARD_STEP_SETTLE_S", 0.3)
    _walk_page(page, original_disabled=True)
    crop_step.set_plan(SINGLE)
    assert md._walk_creative_wizard(page) is True
    assert crop_step.outcome() == "crop_vertical_not_original"
    assert crop_step.outcome() == ""                     # once


def test_without_a_plan_the_walk_is_unchanged(page, monkeypatch):
    monkeypatch.setattr(md, "_WIZARD_STEP_SETTLE_S", 0.3)
    _walk_page(page)
    assert md._walk_creative_wizard(page) is True
    assert page.evaluate("window.clicks") == ["Trim:Next", "Crop:Next", "Text:Next", "Enhancements:Done", "close"]
    # Vertical stays on 9:16: nothing was touched.
    assert crop_step.outcome() == ""


def test_the_walk_finds_crop_by_its_heading_when_the_step_row_is_not_read(page, monkeypatch):
    monkeypatch.setattr(md, "_WIZARD_STEP_SETTLE_S", 0.3)
    _walk_page(page, row=False)
    crop_step.set_plan(SINGLE)
    seen = {}
    real = crop_step.apply

    def spy(p, plan):
        why = real(p, plan)
        seen["vertical"] = crop_step.read(p)["cards"]["Vertical"]["original"]
        return why

    monkeypatch.setattr(crop_step, "apply", spy)
    assert md._walk_creative_wizard(page) is True
    assert seen == {"vertical": True}
    assert crop_step.outcome() == ""


def test_a_single_file_whose_crop_was_not_handled_is_said_in_telemetry(tmp_path, monkeypatch):
    monkeypatch.setattr(rt, "TELEMETRY_PATH", tmp_path / "telemetry.jsonl")
    crop_step.set_plan(SINGLE)
    try:
        assert crop_step.outcome() == ""            # not blocking: a single file
        assert crop_step.outcome() == ""            # said once per attempt
    finally:
        crop_step.set_plan(None)
    rows = _rows(tmp_path, "ad_creative_crop")
    assert [(r["ok"], r["note"]) for r in rows] == [(None, "crop_step_not_shown")]


def test_step_names_that_mean_crop():
    assert crop_step.step_is_crop("Crop")
    assert crop_step.step_is_crop("Select media crop  for placements")
    assert not crop_step.step_is_crop("Text")


def test_a_pair_whose_wizard_never_showed_crop_is_a_named_warning(tmp_path, monkeypatch):
    # ACL-1476: not a refusal; the warning is named and said once per attempt.
    monkeypatch.setattr(rt, "TELEMETRY_PATH", tmp_path / "telemetry.jsonl")
    crop_step.set_plan(PAIR)
    try:
        assert crop_step.outcome() == ""
        assert crop_step.outcome() == ""
        assert crop_step.warning() == crop_step.NO_CROP_STEP
    finally:
        crop_step.set_plan(None)
    assert crop_step.warning() == ""
    assert [(r["ok"], r["note"]) for r in _rows(tmp_path, "ad_creative_crop")] \
        == [(None, "story_not_placed:no_crop_step")]


def test_a_crop_refusal_keeps_its_own_name_on_the_step():
    md._note_media_refused("crop_vertical_not_replaced")
    try:
        assert md._refused_why() == "crop_vertical_not_replaced"
        md._note_media_refused("other_file_attached")
        assert md._refused_why() == "media_refused_on_ad"
    finally:
        md._note_media_refused(None)


def test_the_step_can_be_switched_off_per_machine(monkeypatch, tmp_path):
    (tmp_path / "4_es_2403_post.mp4").write_bytes(b"x")
    assert crop_step.plan_for(str(tmp_path / "4_es_2403_post.mp4")).rule == "post_only"
    monkeypatch.setenv(crop_step.STEP_SETTING, "0")
    assert crop_step.plan_for(str(tmp_path / "4_es_2403_post.mp4")) is None
