# -*- coding: utf-8 -*-
"""A pair whose story cannot be placed keeps the post file with a warning (ACL-1476).

Owner decision 2026-10-01 after the first live runs of ACL-1426:

* `story_not_placed:no_crop_step` - the single-screen editor's wizard walked
  Media -> Text -> Creative generation -> Enhancements, no Crop step (runs
  e4fd79d26e63, bd7b2f...);
* `story_not_placed:not_in_library` - Crop shown, the `_story` file not in
  Meta's library (runs a88e3789f7ab, 362762a797c3). There Meta's "Replace
  media for Stories, Status, Reels" opened INSIDE the wizard dialog - the
  wizard's step row, Close and Save, no Cancel - and the old Cancel/Close
  fallback pressed the wizard's Close.

Both are warnings: the creative step succeeds, the ad keeps the post file,
`failure_policy` keeps the campaign publishable. Everything else in the Crop
gate still blocks. Offline Chrome, headless; the gate tests stub the page.
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
from src.fb_ads_rpa import failure_policy as fp  # noqa: E402
from src.fb_ads_rpa import meta_dom as md  # noqa: E402
from src.fb_ads_rpa import page_capture  # noqa: E402
from src.fb_ads_rpa import run_telemetry as rt  # noqa: E402
from src.fb_ads_rpa import simple_create  # noqa: E402
from src.fb_ads_rpa.ads_manager_flow import apply_step_failures  # noqa: E402
from src.fb_ads_rpa.draft import RpaPublishResult  # noqa: E402

PAIR = cp.CropPlan("3_es_2403_post.mp4", "3_es_2403_story.mp4", True, "pair", "2403")

#: Still blocking (Issue #476, expected behaviour 3).
STILL_BLOCKING = (
    "crop_square_meta_ai_expand_on",
    "crop_vertical_meta_ai_expand_on",
    "crop_meta_ai_expand_on",
    "crop_unknown_toggle_on",
    "crop_vertical_not_story",
    "crop_square_changed",
    "crop_vertical_replace_unconfirmed",          # ACL-1533 review
    "crop_vertical_changed_without_story",
    "crop_replace_picker_unscoped",
    "crop_square_not_original",
    "crop_vertical_not_original",
)


def _rows(tmp_path, step):
    f = tmp_path / "telemetry.jsonl"
    if not f.exists():
        return []
    return [r for r in (json.loads(x) for x in f.read_text(encoding="utf-8").splitlines() if x.strip())
            if r.get("step") == step]


# ── the report line and the policy ────────────────────────────────────────

def test_both_reasons_have_an_english_line():
    assert simple_create.creative_why_text(crop_step.NO_CROP_STEP) \
        .startswith("Story placement uses the post file: Meta showed no Crop step")
    assert simple_create.creative_why_text(crop_step.NOT_IN_LIBRARY) \
        .startswith("Story placement uses the post file: the story file is not in Meta's media library")


@pytest.mark.parametrize("why", crop_step.WARNINGS)
@pytest.mark.parametrize("tag", ["ad", "ad2", "ad3"])
def test_the_warning_row_is_a_warning_and_the_campaign_stays_publishable(monkeypatch, why, tag):
    monkeypatch.setattr(md._crop_warning_tls, "why", why, raising=False)
    row = md.story_not_placed_soft_fail(tag, "Ad 1")
    assert row.startswith(f"{tag}_{why} - Ad 1: Story placement uses the post file")
    assert fp.base_name(row) == f"{tag}_story_not_placed"
    assert fp.severity(row) == fp.WARNING
    assert fp.split([row]) == ([], [row])
    result = RpaPublishResult(campaign_name="CO_bnc_All_Video_Day")
    result.success = True
    apply_step_failures(result, [row])
    assert result.publishable is True
    assert result.warnings == [row] and result.blocking_failures == []


def test_no_warning_row_without_a_warning(monkeypatch):
    monkeypatch.setattr(md._crop_warning_tls, "why", "", raising=False)
    assert md.story_not_placed_soft_fail("ad", "Ad 1") is None


def test_the_creative_step_failing_is_still_blocking():
    result = RpaPublishResult(campaign_name="CO_bnc_All_Video_Day")
    result.success = True
    apply_step_failures(result, ["ad_creative_upload", "ad2_creative_upload"])
    assert result.publishable is False


# ── the gate: both warnings pass, everything else still blocks ───────────

@pytest.fixture
def gate(monkeypatch, tmp_path):
    md._failed_uploads.clear()
    md._note_media_refused(None)
    monkeypatch.setattr(rt, "TELEMETRY_PATH", tmp_path / "telemetry.jsonl")
    rt.set_current_run("run-1476", "CAMP")
    for name, value in {
        "_sleep_h": lambda *a, **k: None, "_page_read_trouble": lambda *a, **k: None,
        "_upload_stalled": lambda page: False, "reuse_before_upload": lambda page, path: None,
        "upload_already_failed": lambda path: False, "upload_already_succeeded": lambda path: False,
        "remember_upload_succeeded": lambda path: None, "_remember_upload_failed": lambda path: None,
        "_flow_budget_step": lambda *a, **k: None,
    }.items():
        monkeypatch.setattr(md, name, value)
    monkeypatch.setattr(crop_step, "read", lambda page, mark=None: {"found": False})
    (tmp_path / "3_es_2403_post.mp4").write_bytes(b"x")
    (tmp_path / "3_es_2403_story.mp4").write_bytes(b"x")
    try:
        yield {"post": str(tmp_path / "3_es_2403_post.mp4"), "story": str(tmp_path / "3_es_2403_story.mp4"),
               "tmp": tmp_path}
    finally:
        rt.clear_current_run()
        md._failed_uploads.clear()
        md._note_media_refused(None)
        crop_step.set_plan(None)


def _walk_sets(failure: str = "", warning: str = ""):
    def upload(page, kind, path, **k):
        crop_step._tls.done = True                 # the walk reached Crop
        if failure:
            crop_step._tls.failure = failure
        crop_step._tls.warning = warning
        upload.paths.append(Path(path).name)
        return True
    upload.paths = []
    return upload


@pytest.mark.parametrize("chosen", ["post", "story"])
def test_story_not_in_library_passes_with_a_warning_and_the_post_file(monkeypatch, gate, chosen):
    upload = _walk_sets(warning=crop_step.NOT_IN_LIBRARY)
    monkeypatch.setattr(md, "upload_ad_creative", upload)
    assert md.upload_ad_creative_with_retry(object(), "video", gate[chosen]) is True
    assert upload.paths == ["3_es_2403_post.mp4"]        # the Builder's story -> the post
    assert md.last_upload_why() == ""
    assert md.last_crop_warning() == crop_step.NOT_IN_LIBRARY
    assert fp.severity(md.story_not_placed_soft_fail("ad", "Ad 1")) == fp.WARNING


def test_no_crop_step_passes_with_a_warning_and_the_post_file(monkeypatch, gate):
    uploaded = []
    monkeypatch.setattr(md, "upload_ad_creative", lambda page, kind, path, **k: uploaded.append(path) or True)
    assert md.upload_ad_creative_with_retry(object(), "video", gate["story"]) is True
    assert [Path(p).name for p in uploaded] == ["3_es_2403_post.mp4"]
    assert md.last_crop_warning() == crop_step.NO_CROP_STEP
    rows = _rows(gate["tmp"], "ad_creative_crop")
    assert [(r["ok"], r["note"]) for r in rows] == [(None, "story_not_placed:no_crop_step")]


@pytest.mark.parametrize("why", STILL_BLOCKING)
def test_the_other_crop_reasons_still_block(monkeypatch, gate, why):
    monkeypatch.setattr(md, "upload_ad_creative", _walk_sets(failure=why))
    monkeypatch.setattr(md, "_verify_ad_media_present", lambda page: True)
    assert md.upload_ad_creative_with_retry(object(), "video", gate["post"]) is False
    assert md.last_upload_why() == why
    assert md.last_crop_warning() == ""
    assert fp.severity("ad_creative_upload") == fp.BLOCKING


def test_a_failed_step_carries_no_warning_row(monkeypatch, gate):
    # The warning belongs to a successful step only.
    monkeypatch.setattr(md, "upload_ad_creative", lambda *a, **k: False)
    monkeypatch.setattr(md, "_close_creative_setup_modal", lambda page: True)
    monkeypatch.setattr(md, "_verify_ad_media_present", lambda page: False)
    assert md.upload_ad_creative_with_retry(object(), "video", gate["post"]) is False
    assert md.last_crop_warning() == ""


# ── in the browser: the live wizard sequence and Meta's in-wizard picker ─

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
    monkeypatch.setattr(md, "_WIZARD_STEP_SETTLE_S", 0.3)
    monkeypatch.setattr(rt, "TELEMETRY_PATH", tmp_path / "telemetry.jsonl")
    monkeypatch.setattr(page_capture, "_root", lambda: tmp_path / "rpa_pages")
    monkeypatch.delenv(crop_step.REPLACE_SETTING, raising=False)
    crop_step.set_plan(None)
    ctx = browser.new_context(viewport={"width": 1400, "height": 1000})
    p = ctx.new_page()
    p.route("**/*", lambda route: route.abort())
    try:
        yield p
    finally:
        ctx.close()
        crop_step.set_plan(None)


_WALK = """<!doctype html><html><body>
<div role="dialog" id="wiz">
  <div role="list" id="row"></div>
  <div id="body"></div>
  <button>Cancel</button><button id="fwd" onclick="advance()">Next</button>
</div>
<script>
  const steps = %(steps)s;
  let at = 0; window.clicks = [];
  function paint() {
    document.getElementById('row').innerHTML = steps.map((s, i) => '<div role="listitem"><div role="button" '
      + (i === at ? 'aria-current="step"' : '') + '><div>' + s + '</div></div></div>').join('');
    document.getElementById('body').innerHTML = '<div role="heading">' + steps[at] + '</div>';
    document.getElementById('fwd').textContent = at === steps.length - 1 ? 'Done' : 'Next';
  }
  function advance() {
    window.clicks.push(steps[at] + ':' + document.getElementById('fwd').textContent);
    at += 1;
    if (at >= steps.length) { document.getElementById('wiz').remove(); window.clicks.push('close'); return; }
    paint();
  }
  paint();
</script></body></html>"""

#: The upload wizard of the single-screen editor, live run e4fd79d26e63
#: (telemetry `ad_creative_wizard`, origin upload): no Trim, no Crop.
LIVE_SEQUENCE = ["Media", "Text", "Creative generation", "Enhancements"]


def test_the_live_single_screen_sequence_without_crop_ends_with_a_warning(page, tmp_path):
    page.set_content(_WALK % {"steps": json.dumps(LIVE_SEQUENCE)})
    crop_step.set_plan(PAIR)
    assert md._walk_creative_wizard(page) is True
    assert page.evaluate("window.clicks") == [
        "Media:Next", "Text:Next", "Creative generation:Next", "Enhancements:Done", "close"]
    assert crop_step.applied() is False
    assert crop_step.outcome() == ""                     # not a refusal
    assert crop_step.warning() == crop_step.NO_CROP_STEP
    assert md._crop_gate("3_es_2403_post.mp4", "upload") == ""
    assert [(r["ok"], r["note"]) for r in _rows(tmp_path, "ad_creative_crop")] \
        == [(None, "story_not_placed:no_crop_step")]
    assert not _rows(tmp_path, "ad_creative_upload")


_CARD = """
<div class="card"><div role="heading" aria-level="4">%(name)s</div>
  <img src="https://x.invalid/3_es_2403_post.mp4" style="width:60px;height:60px">
  <div role="radiogroup">
    <label><input type="radio" name="r_%(name)s" %(orig)s %(disabled)s> Original</label>
    <label><input type="radio" name="r_%(name)s" %(ratio)s> %(ratio_label)s</label>
  </div>
  <label><input type="checkbox" id="exp_%(name)s" %(expand)s> Crop and expand (Meta AI)</label>
  <button>Crop</button>
  <button onclick="openPicker()">Replace</button>
</div>
"""

#: Shaped like the live picker (a88e3789f7ab, `03_wizard_crop_replace`): the
#: picker replaces the Crop step inside the wizard dialog; the dialog keeps the
#: step row (Media/Trim disabled, Crop enabled, ...), Close and Save; no Cancel.
#: ACL-1533 (live 03.10, fixture `crop_replace_in_wizard_picker_2026_10_03`):
#: the row's Crop is the current step and does nothing; the picker's own Save
#: (footer, after "Search media") brings Crop back.
_IN_WIZARD = """<!doctype html><html><body>
<div role="dialog" id="wiz">
  <div id="row">
    <div role="button" aria-disabled="true">Creative setup</div>
    <div role="button" aria-disabled="true">Media</div>
    <div role="button" aria-disabled="true">Trim</div>
    <div role="button" id="row_crop" aria-current="step" onclick="window.pressed.push('Crop row')">Crop</div>
    <div role="button" aria-disabled="true">Text</div>
    <div role="button" aria-disabled="true">Creative generation</div>
    <div role="button" aria-disabled="true">Enhancements</div>
  </div>
  <button onclick="window.pressed.push('Close'); document.getElementById('wiz').remove()">Close</button>
  <button onclick="window.pressed.push('Save')">Save</button>
  <div id="body"><div id="crop">
    <div role="heading" aria-level="3">Select media crop for placements</div>
    %(cards)s
    %(extra)s
  </div></div>
  <button id="next" onclick="window.pressed.push('Next')">Next</button>
</div>
<script>
  window.pressed = [];
  const crop = document.getElementById('crop');
  function openPicker() {
    crop.remove();
    const d = document.createElement('div');
    d.id = 'picker';
    d.innerHTML = '<div role="heading">Replace media for Stories, Status, Reels</div>'
      + '<input placeholder="Search media"><div id="count">0 selected</div>'
      + '<div role="button" class="tile" style="width:90px;height:90px"><div><div>1_es_2292_post.mp4</div></div></div>'
      + '<button id="own_save" onclick="ownSave()">Save</button>';
    document.getElementById('body').appendChild(d);
  }
  function ownSave() { window.pressed.push('own Save'); %(own_save)s }
  function backToCrop() {
    const d = document.getElementById('picker');
    if (d) d.remove();
    document.getElementById('body').appendChild(crop);
  }
</script></body></html>"""


def _in_wizard(page, *, own_save="backToCrop()", original_disabled=False,
               expand=("checked", ""), extra=""):
    cards = "".join(_CARD % {
        "name": n, "orig": "" if n == "Vertical" else "checked",
        "disabled": "disabled" if original_disabled and n == "Vertical" else "",
        "ratio": "checked" if n == "Vertical" else "",
        "ratio_label": "9:16 (recommended)" if n == "Vertical" else "1:1",
        "expand": e,
    } for n, e in zip(("Square", "Vertical"), expand))
    page.set_content(_IN_WIZARD % {"cards": cards, "own_save": own_save, "extra": extra})


def test_story_not_in_an_in_wizard_library_goes_back_through_the_pickers_own_save(page, tmp_path):
    _in_wizard(page)
    crop_step.set_plan(PAIR)
    assert crop_step.apply(page, PAIR) == ""
    assert crop_step.warning() == crop_step.NOT_IN_LIBRARY
    # ACL-1533: the picker's own Save - never the row's Crop, the header
    # Save, Close or Next.
    assert page.evaluate("window.pressed") == ["own Save"]
    state = crop_step.read(page)
    assert state["cards"]["Vertical"]["original"] is True             # was 9:16
    assert not any(c["checked"] for c in state["cards"]["Square"]["expand"])
    assert state["cards"]["Vertical"]["media"].endswith("3_es_2403_post.mp4")
    row = _rows(tmp_path, "ad_creative_crop")[-1]
    assert (row["ok"], row["note"]) == (None, "story_not_placed:not_in_library")
    assert row["diag"]["tiles"] == 0 and row["diag"]["picker_back"] == "Save"
    assert crop_step.outcome() == ""


def test_story_not_in_library_with_original_not_settable_still_blocks(page):
    _in_wizard(page, original_disabled=True)
    crop_step.set_plan(PAIR)
    assert crop_step.apply(page, PAIR) == "crop_vertical_not_original"
    assert crop_step.warning() == ""
    assert crop_step.outcome() == "crop_vertical_not_original"


def test_story_not_in_library_with_an_unknown_toggle_on_still_blocks(page):
    _in_wizard(page, extra='<div role="switch" aria-checked="true" aria-label="Optimise for placements" '
                           'style="width:20px;height:20px"></div>')
    crop_step.set_plan(PAIR)
    assert crop_step.apply(page, PAIR) == "crop_unknown_toggle_on"
    assert crop_step.warning() == ""


def test_a_picker_that_cannot_be_left_still_blocks(page, tmp_path):
    # The picker's Save does not bring Crop back: the cards cannot be read
    # back, so the step is blocked (and the picker closed the old way).
    _in_wizard(page, own_save="")
    crop_step.set_plan(PAIR)
    assert crop_step.apply(page, PAIR) == "crop_replace_picker_not_closed"
    assert crop_step.warning() == ""
    assert page.evaluate("window.pressed") == ["own Save", "Close"]
    assert _rows(tmp_path, "ad_creative_crop")[-1]["ok"] is False
