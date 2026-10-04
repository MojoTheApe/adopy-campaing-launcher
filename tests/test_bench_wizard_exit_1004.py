# -*- coding: utf-8 -*-
"""Story not in the library: the media wizard is left cleanly (ACL-1551).

Live run on Facebook_1443, 2026-10-04 (232d398471bd, campaign 04.10, ad 1 =
a video post/story pair whose story file is not in Meta's library):

* after the library pick's Next the wizard sat on Media with Next and Cancel
  aria-disabled (Meta busy, capture 00_wizard_media); the walk counted that as
  four misses (~4 s) and gave up;
* on Crop, after Replace -> "Search media" -> nothing -> the picker's own
  footer Save (ACL-1533, worked), Next made the wizard busy and then returned
  to the same Crop (02_wizard_crop, Next enabled again); the second Next hung
  for over a minute (04_ad_creative_upload, 05_simple_create_more_ads: Crop,
  Next and Cancel aria-disabled);
* the fallback close looked for the corner icon next to "Creative setup", which
  this step row (Media · Trim · Crop · Text · ...) does not have: nothing was
  pressed, the dialog stayed, and the ad set's Action menu for ads 2-3 never
  opened.

The Crop layout of 04.10 has no Save of its own: its footer is Cancel / Next;
the only Save is in the in-wizard Replace picker (header = wizard's, footer =
the picker's own, ACL-1533). What each button does is wired by the test the way
it went live (Next -> busy -> same Crop; Close -> wizard gone). One scenario is
constructed and says so: a footer that reads Save after Next did not advance.

Fixture: `wizard_crop_story_missing_2026_10_04.html` (scrubbed with
`scripts/capture_to_fixture.py`; media file names are `sample_media_NN`,
numbered per fragment). Offline Chrome, headless.
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
from src.fb_ads_rpa import failure_policy  # noqa: E402
from src.fb_ads_rpa import meta_dom as md  # noqa: E402
from src.fb_ads_rpa import page_capture  # noqa: E402
from src.fb_ads_rpa import run_telemetry as rt  # noqa: E402
from src.fb_ads_rpa import simple_create  # noqa: E402
from tests.bench_support import FIXTURES, WIZARD_ROW_CSS, browser, fragments  # noqa: E402,F401

FRAGS = fragments(FIXTURES / "wizard_crop_story_missing_2026_10_04.html")

#: In the picker fragment sample_media_04.mp4 is the ad's post (1080 x 1080);
#: the story file of the pair is not among the tiles (live: not in the library).
MISSING = cp.CropPlan("sample_media_04.mp4", "sample_media_09.mp4", True, "pair", "2403")

#: Meta's behaviour around the captured markup, as seen live on 04.10.
_WIRE = r"""
(args) => {
  const norm = (s) => (s || '').replace(/[​-‍﻿]/g, '').replace(/\s+/g, ' ').trim();
  window.pressed = []; window.mint = 0; window.stayed = false;
  const host = document.getElementById('host');
  const F = args.frags;
  const paint = (state) => {
    window.state = state;
    if (state === 'closed') { host.innerHTML = ''; return; }
    host.innerHTML = state === 'crop' ? F.crop_step : state === 'busy' ? F.crop_busy
                   : state === 'picker' ? F.in_wizard_picker : F.media_busy;
    for (const h of host.querySelectorAll('[role=heading]')) {
      const t = norm(h.textContent);
      if (t !== 'Square' && t !== 'Vertical') continue;
      const v = document.createElement('video');
      v.style.cssText = 'width:60px;height:60px;display:block';
      v.setAttribute('src', 'blob:https://adsmanager.invalid/render-' + (++window.mint));
      h.after(v);
    }
    for (const t of host.querySelectorAll('[aria-label=grid] [role=button]'))
      t.style.cssText = 'width:96px;height:40px;display:inline-block';
    const relabel = (state === 'crop' && window.stayed && args.footer_after_stay) ? args.footer_after_stay
                  : (state === 'crop' && args.footer_from_start) ? args.footer_from_start : '';
    if (relabel) {
      // Constructed (not seen live): the footer's primary slot reads Save.
      const next = [...host.querySelectorAll('[role=button]')].filter(b => norm(b.textContent) === 'Next').pop();
      if (next) next.querySelector('div div div').textContent = relabel;
    }
  };
  window.paint = paint;
  const inVertical = (b) => {
    const v = [...host.querySelectorAll('[role=heading]')].find(h => norm(h.textContent) === 'Vertical');
    const s = [...host.querySelectorAll('[role=heading]')].find(h => norm(h.textContent) === 'Square');
    return !!v && !!(v.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING)
               && !(s.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);
  };
  document.addEventListener('click', (e) => {
    const b = e.target.closest('[role=button],button');
    if (!b) return;
    const label = norm(b.textContent) || norm(b.getAttribute('aria-label'));
    if (b.closest('#editor-footer')) { window.pressed.push('editor:' + label); return; }
    if (b.getAttribute('aria-disabled') === 'true') { window.pressed.push('disabled:' + label); return; }
    const st = window.state;
    if (b.closest('[role=list]')) { window.pressed.push('row:' + label); return; }
    if (st === 'picker') {
      if (b.closest('[aria-label=grid]')) return;
      const search = host.querySelector('input[placeholder="Search media"]');
      if (label === 'Save' && (search.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING)) {
        window.pressed.push('own Save'); paint('crop'); return;
      }
      if (label === 'Save') { window.pressed.push('header Save'); return; }
    }
    if (label === 'Close') {
      window.pressed.push('Close');
      if (args.close !== 'noop') paint('closed');
      return;
    }
    if (st === 'crop' && label === 'Replace' && inVertical(b)) {
      window.pressed.push('Replace'); paint('picker'); return;
    }
    if (st === 'crop' && label === 'Next') {
      window.pressed.push('Next');
      if (args.next === 'closes') { setTimeout(() => paint('closed'), 300); return; }
      paint('busy');
      if (args.next === 'stays') setTimeout(() => { window.stayed = true; paint('crop'); }, args.busy_ms);
      return;                                     // 'busy_forever': stays busy
    }
    if (st === 'crop' && (label === 'Save' || label === 'Done')) {
      window.pressed.push(label);
      if (args.save !== 'noop') paint('closed');
      return;
    }
    window.pressed.push(label);
  }, true);
  if (args.start === 'media_busy') {
    paint('media_busy');
    setTimeout(() => paint('crop'), args.media_ms);
  } else {
    paint('crop');
  }
}
"""

#: The editor's own footer beside the wizard (single-screen editor): never pressed.
_PAGE = (
    "<!doctype html><html><head>" + WIZARD_ROW_CSS + "</head><body>"
    "<div id='editor-footer' data-surface='/am/lib:syd_simple_create_footer' style='display:flex;gap:8px'>"
    "<div role='button'>Close</div><div role='button'>Save</div><div role='button'>Next</div>"
    "<div role='button'>Done</div><div role='button'>Publish</div></div>"
    "<div id='host'></div></body></html>"
)


@pytest.fixture
def page(browser, monkeypatch, tmp_path):  # noqa: F811
    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    monkeypatch.setattr(md, "_LIBRARY_APPEAR_TRIES", 1)
    monkeypatch.setattr(md, "_CROP_IDLE_GRACE_S", 0.8)
    monkeypatch.setattr(md, "_CROP_ADVANCE_WAIT_S", 3.0)
    monkeypatch.setattr(md, "_WIZARD_BUSY_WAIT_S", 6.0)
    monkeypatch.setattr(md, "_WIZARD_EXIT_WAIT_S", 1.5)
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


def _live(page, **args):
    page.set_content(_PAGE)
    page.evaluate(_WIRE, {"frags": FRAGS, "next": "stays", "busy_ms": 600, "close": "ok",
                          "start": "crop", "media_ms": 800, "footer_after_stay": "", "footer_from_start": "",
                          "save": "closes", **args})


def _pressed(page) -> list[str]:
    return page.evaluate("window.pressed")


def _rows(tmp_path, step: str) -> list[dict]:
    f = tmp_path / "telemetry.jsonl"
    rows = [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines() if x.strip()] if f.exists() else []
    return [r for r in rows if r.get("step") == step]


def _never_pressed_outside(pressed: list[str]) -> None:
    assert not [p for p in pressed if p.startswith(("editor:", "disabled:", "row:"))], pressed
    assert "Publish" not in " ".join(pressed)
    assert "header Save" not in pressed


# ── the captured screens ─────────────────────────────────────────────────

def test_the_04_10_crop_has_no_save_and_its_busy_state_locks_next_and_cancel(page):
    _live(page)
    foot = md._wizard_foot(page)
    assert foot["root"] and not foot["busy"] and foot["forward_enabled"] == 1
    assert md._press_wizard_exit.__name__  # the exit control is looked up, not guessed
    assert page.evaluate(md._WIZARD_EXIT_JS, "complete") is None       # no Save/Done on Crop
    close = page.evaluate(md._WIZARD_EXIT_JS, "close")
    assert close["label"] == "Close"
    page.evaluate("paint('busy')")
    foot = md._wizard_foot(page)
    assert foot["busy"] and foot["forward_enabled"] == 0 and foot["cancel"] == "disabled"
    assert crop_step.read(page)["found"], "busy is still the Crop step"
    page.evaluate("paint('media_busy')")
    assert md._wizard_foot(page)["busy"]
    page.evaluate("paint('picker')")
    # Picker open: its footer Save is the picker's, never the wizard's exit.
    assert page.evaluate(md._WIZARD_EXIT_JS, "complete") == {"refused": "picker_open"}


def test_the_forward_button_is_looked_up_inside_the_wizard_only(page):
    _live(page)
    page.evaluate("paint('busy')")
    # Old document-wide lookup found the editor footer's Done/Save/Next here.
    assert md._press_wizard_forward(page) is None
    assert _pressed(page) == []


# ── 1. story not in the library, the wizard closes ───────────────────────

def test_story_missing_wizard_left_through_save_then_one_next_warning_only(page, tmp_path):
    _live(page, next="closes")
    crop_step.set_plan(MISSING)
    assert md._walk_creative_wizard(page) is True
    pressed = _pressed(page)
    assert pressed == ["Replace", "own Save", "Next"]
    _never_pressed_outside(pressed)
    assert crop_step.outcome() == ""
    assert crop_step.warning() == crop_step.NOT_IN_LIBRARY
    row = _rows(tmp_path, "ad_creative_wizard")[-1]
    assert row["note"] == "other:closed"
    assert row["diag"]["closed_by"] == "Next" and row["diag"]["dialog_left"] is False
    crop = _rows(tmp_path, "ad_creative_crop")[-1]
    assert crop["note"] == crop_step.NOT_IN_LIBRARY and crop["diag"]["picker_back"] == "Save"


def test_constructed_footer_save_after_next_closes_the_wizard_warning_only(page, tmp_path):
    """Constructed: after Next did not advance, the footer's primary slot reads
    Save - the wizard's own completing control is pressed, not Close."""
    _live(page, next="stays", footer_after_stay="Save")
    crop_step.set_plan(MISSING)
    assert md._walk_creative_wizard(page) is True
    pressed = _pressed(page)
    assert pressed == ["Replace", "own Save", "Next", "Save"]
    _never_pressed_outside(pressed)
    assert crop_step.outcome() == ""
    assert crop_step.warning() == crop_step.NOT_IN_LIBRARY
    row = _rows(tmp_path, "ad_creative_wizard")[-1]
    assert row["diag"]["closed_by"] == "Save" and row["diag"]["exit"]["closed_by"] == "Save"
    assert row["diag"]["dialog_left"] is False


def test_a_footer_save_already_pressed_as_forward_is_not_pressed_again(page, tmp_path):
    """Review F1 (constructed): Crop's footer reads Save from the start and does
    nothing. Save is the forward press; the exit must not press it a second
    time - straight to the wizard's Close."""
    _live(page, footer_from_start="Save", save="noop")
    assert md._walk_creative_wizard(page) is True
    pressed = _pressed(page)
    assert pressed == ["Save", "Close"], pressed
    _never_pressed_outside(pressed)
    assert crop_step.outcome() == crop_step.WIZARD_STUCK
    row = _rows(tmp_path, "ad_creative_wizard")[-1]
    assert row["diag"]["exit"]["tried"] == ["Close"] and row["diag"]["closed_by"] == "Close"


def test_the_exit_lookup_never_offers_a_skipped_label(page):
    _live(page, footer_from_start="Save")
    assert page.evaluate(md._WIZARD_EXIT_JS, {"want": "complete", "skip": ["Save"]}) is None
    assert page.evaluate(md._WIZARD_EXIT_JS, {"want": "complete", "skip": []})["label"] == "Save"


def test_a_busy_media_step_is_waited_out_not_abandoned(page, tmp_path):
    """00_wizard_media: Next and Cancel locked while Meta takes the pick."""
    _live(page, start="media_busy", next="closes")
    crop_step.set_plan(MISSING)
    assert md._walk_creative_wizard(page) is True
    assert _pressed(page) == ["Replace", "own Save", "Next"]
    row = _rows(tmp_path, "ad_creative_wizard")[-1]
    assert row["diag"]["sequence"][:2] == ["Media", "Crop"]
    assert row["diag"]["busy_s"], "the busy wait is said"


# ── 2. the wizard does not close: its own Close, blocking ───────────────

def test_next_back_on_the_same_crop_is_not_pressed_again_close_and_block(page, tmp_path):
    _live(page, next="stays")
    crop_step.set_plan(MISSING)
    assert md._walk_creative_wizard(page) is True        # gone: closed by its Close
    pressed = _pressed(page)
    assert pressed == ["Replace", "own Save", "Next", "Close"]
    assert pressed.count("Next") == 1
    _never_pressed_outside(pressed)
    assert crop_step.failure_pending()
    assert crop_step.outcome() == crop_step.WIZARD_STUCK
    assert crop_step.warning() == "", "blocking, not the story warning"
    assert page.evaluate(md._OPEN_DIALOGS_JS) == 0
    row = _rows(tmp_path, "ad_creative_wizard")[-1]
    assert row["note"] == "other:closed_by_close"
    assert row["diag"]["closed_by"] == "Close" and row["diag"]["dialog_left"] is False
    assert row["diag"]["exit"]["why"] == "next_did_not_advance"
    assert row["diag"]["exit"]["next_presses"] == 1


def test_a_crop_busy_for_good_is_closed_with_its_close(page, tmp_path):
    """05_simple_create_more_ads: the second Next hung for over a minute."""
    _live(page, next="busy_forever")
    crop_step.set_plan(MISSING)
    assert md._walk_creative_wizard(page) is True
    pressed = _pressed(page)
    assert pressed == ["Replace", "own Save", "Next", "Close"]
    _never_pressed_outside(pressed)
    assert crop_step.outcome() == crop_step.WIZARD_STUCK
    row = _rows(tmp_path, "ad_creative_wizard")[-1]
    assert row["diag"]["exit"]["why"] == "busy" and row["diag"]["dialog_left"] is False


def test_a_close_that_does_not_close_says_the_dialog_is_left(page, tmp_path):
    _live(page, next="stays", close="noop")
    crop_step.set_plan(MISSING)
    assert md._walk_creative_wizard(page) is False
    pressed = _pressed(page)
    assert pressed.count("Next") == 1 and pressed.count("Close") == 1
    _never_pressed_outside(pressed)
    assert crop_step.outcome() == crop_step.WIZARD_STUCK
    row = _rows(tmp_path, "ad_creative_wizard")[-1]
    assert row["note"] == "other:left_open" and row["diag"]["dialog_left"] is True


def test_no_next_loop_a_later_walk_of_the_same_ad_goes_straight_to_close(page, tmp_path):
    """Live the retry and the reuse path walked the same Crop again and pressed
    Next again. Within one ad, a Crop whose Next did not advance is not pressed."""
    _live(page, next="stays")
    crop_step.set_plan(MISSING)
    md._walk_creative_wizard(page)
    crop_step.reset_attempt()                          # a new attempt, same ad
    page.evaluate("paint('crop')")                    # the wizard is back on Crop
    assert md._close_creative_setup_modal(page) is True
    pressed = _pressed(page)
    assert pressed.count("Next") == 1, pressed
    assert pressed[-1] == "Close"
    _never_pressed_outside(pressed)


def test_the_force_close_uses_the_wizards_own_close_on_this_step_row(page, tmp_path):
    """The corner-icon fallback was anchored on "Creative setup" (not in this
    row) and pressed nothing. A wizard the walk cannot move is closed by Close."""
    _live(page, next="busy_forever")
    page.evaluate("paint('busy')")
    assert md._close_creative_setup_modal(page) is True
    assert _pressed(page) == ["Close"]
    rows = _rows(tmp_path, "ad_creative_wizard")
    assert any(r["note"] == "force_close:closed_by_close" for r in rows)


# ── the creative step: post attached with a warning, or blocking ─────────

def _upload_through_bench(page, calls):
    def fake(p, creative_type, creative_path, *, edit_url="", reuse_only=False):
        # As the library-pick path: walk the wizard, then ask the Crop step.
        calls.append(Path(creative_path).name)
        closed = md._walk_creative_wizard(p)
        if md._crop_refusal(Path(creative_path).name):
            return False
        return closed
    return fake


def test_the_creative_step_keeps_the_post_with_the_story_warning(page, monkeypatch, tmp_path):
    _live(page, next="closes")
    calls: list = []
    monkeypatch.setattr(md, "upload_ad_creative", _upload_through_bench(page, calls))
    monkeypatch.setattr(crop_step, "plan_for", lambda path: MISSING)
    path = str(tmp_path / "sample_media_04.mp4")
    assert md.upload_ad_creative_with_retry(page, "video", path) is True
    assert md.last_crop_warning() == crop_step.NOT_IN_LIBRARY
    row = md.story_not_placed_soft_fail("ad", "ad 1")
    assert row.startswith("ad_story_not_placed:not_in_library - ad 1: ")
    assert failure_policy.severity(row) == failure_policy.WARNING


def test_a_stuck_wizard_blocks_only_this_ad_with_a_clear_reason(page, monkeypatch, tmp_path):
    _live(page, next="stays")
    calls: list = []
    monkeypatch.setattr(md, "upload_ad_creative", _upload_through_bench(page, calls))
    monkeypatch.setattr(crop_step, "plan_for", lambda path: MISSING)
    path = str(tmp_path / "sample_media_04.mp4")
    assert md.upload_ad_creative_with_retry(page, "video", path) is False
    assert calls == ["sample_media_04.mp4"], "no second attempt - it would press Next again"
    assert md.last_upload_why() == crop_step.WIZARD_STUCK
    # Review F2: the reason named by the Crop step is the step's reason (the
    # retry wrapper names it; `_creative_why` keeps the first one named).
    diag = md.last_upload_diag()
    assert diag["why"] == crop_step.WIZARD_STUCK
    assert diag["text"] == simple_create.creative_why_text(crop_step.WIZARD_STUCK)
    assert md.last_crop_warning() == ""
    assert page.evaluate(md._OPEN_DIALOGS_JS) == 0
    assert _pressed(page).count("Next") == 1
    row = md.creative_soft_fail("ad", "Sample Ad 01")
    assert row == ("ad_creative_upload:crop_wizard_stuck - Sample Ad 01: "
                   + simple_create.creative_why_text(crop_step.WIZARD_STUCK))
    problem = failure_policy.problem(row)
    assert problem["severity"] == failure_policy.BLOCKING
    assert problem["title"] == "Ad media - Meta's media wizard stuck on Crop"
    assert problem["ad_index"] == 1 and problem["ad_name"] == "Sample Ad 01"
    assert "Close" in failure_policy.reason(row)


def test_the_new_reasons_read_in_english():
    text = simple_create.creative_why_text(crop_step.WIZARD_STUCK)
    assert text.startswith("Meta's media wizard stayed on its Crop step")
    assert text.isascii()
    assert simple_create.why_text("creative_wizard_left_open").startswith("Meta's media wizard")
    assert md.creative_soft_fail.__doc__
    # Any other creative failure keeps the bare step name.
    md._set_upload_why("library_pick_left_selection")
    try:
        assert md.creative_soft_fail("ad2", "Two") == "ad2_creative_upload"
    finally:
        md._set_upload_why(None)
