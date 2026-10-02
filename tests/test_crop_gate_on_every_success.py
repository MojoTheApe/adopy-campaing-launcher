# -*- coding: utf-8 -*-
"""Every success return of the creative step checks the Crop outcome first (ACL-1426, review PR #440 H1).

Before the fix a Crop failure stored by the retry loop's close-walk was erased
by `crop_step.set_plan(None)` while the step reported success on "media is on
the ad"; it must fail the step with a named reason. ACL-1476 (owner,
2026-10-01): a post/story pair whose wizard never showed Crop is no longer a
refusal - the ad keeps the post file and the step succeeds with the warning
`story_not_placed:no_crop_step`. No browser: the page helpers are stubbed.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import crop_step  # noqa: E402
from src.fb_ads_rpa import meta_dom as md  # noqa: E402
from src.fb_ads_rpa import run_telemetry as rt  # noqa: E402

EXPAND_ON = "crop_square_meta_ai_expand_on"


@pytest.fixture
def folder(monkeypatch, tmp_path):
    md._failed_uploads.clear()
    md._note_media_refused(None)
    monkeypatch.setattr(rt, "TELEMETRY_PATH", tmp_path / "telemetry.jsonl")
    rt.set_current_run("run-1426h1", "CAMP")
    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    monkeypatch.setattr(md, "_page_read_trouble", lambda *a, **k: None)
    monkeypatch.setattr(md, "_upload_stalled", lambda page: False)
    monkeypatch.setattr(md, "reuse_before_upload", lambda page, path: None)
    monkeypatch.setattr(md, "upload_already_failed", lambda path: False)
    monkeypatch.setattr(md, "upload_already_succeeded", lambda path: False)
    monkeypatch.setattr(md, "remember_upload_succeeded", lambda path: None)
    monkeypatch.setattr(md, "_remember_upload_failed", lambda path: None)
    monkeypatch.setattr(md, "_flow_budget_step", lambda *a, **k: None)
    monkeypatch.setattr(crop_step, "read", lambda page, mark=None: {"found": False})
    pair = tmp_path / "pair"
    pair.mkdir()
    (pair / "4_es_2403_post.mp4").write_bytes(b"x")
    (pair / "4_es_2403_story.mp4").write_bytes(b"x")
    single = tmp_path / "single"
    single.mkdir()
    (single / "4_es_2403_post.mp4").write_bytes(b"x")
    try:
        yield {"pair": str(pair / "4_es_2403_post.mp4"), "single": str(single / "4_es_2403_post.mp4"),
               "tmp": tmp_path}
    finally:
        rt.clear_current_run()
        md._failed_uploads.clear()
        md._note_media_refused(None)
        crop_step.set_plan(None)


def _rows(tmp_path, step):
    f = tmp_path / "telemetry.jsonl"
    if not f.exists():
        return []
    return [r for r in (json.loads(x) for x in f.read_text(encoding="utf-8").splitlines() if x.strip())
            if r.get("step") == step]


def _crop_fails_in_close_walk(page):
    # The close-walk reached Crop and could not bring it to the plan.
    crop_step._tls.failure = EXPAND_ON
    crop_step._tls.done = True
    return True


# ── retry loop ────────────────────────────────────────────────────────────

def test_a_crop_failure_in_the_retry_close_walk_fails_the_step(monkeypatch, folder):
    """Review probe 1: upload fails, close-walk fails Crop, media is on the ad."""
    monkeypatch.setattr(md, "upload_ad_creative", lambda *a, **k: False)
    monkeypatch.setattr(md, "_close_creative_setup_modal", _crop_fails_in_close_walk)
    checks = []
    monkeypatch.setattr(md, "_verify_ad_media_present", lambda page: checks.append(1) or True)
    ok = md.upload_ad_creative_with_retry(object(), "video", folder["single"])
    assert ok is False
    assert md.last_upload_why() == EXPAND_ON
    assert md.last_upload_diag().get("why") == EXPAND_ON
    assert checks == []                                   # no "media is there" success, no retry
    row = _rows(folder["tmp"], "ad_creative_upload")[-1]
    assert (row["ok"], row["note"], row["diag"]["where"]) == (False, EXPAND_ON, "retry_close_walk")


def test_a_pair_whose_wizard_never_showed_crop_succeeds_with_a_warning_after_the_media_check(
        monkeypatch, folder):
    """Review probe 2, ACL-1476: pair on disk, the wizard never on Crop, media on the ad."""
    monkeypatch.setattr(md, "upload_ad_creative", lambda *a, **k: False)
    monkeypatch.setattr(md, "_close_creative_setup_modal", lambda page: True)
    monkeypatch.setattr(md, "_verify_ad_media_present", lambda page: True)
    ok = md.upload_ad_creative_with_retry(object(), "video", folder["pair"])
    assert ok is True
    assert md.last_upload_why() == ""
    assert md.last_crop_warning() == crop_step.NO_CROP_STEP
    assert [(r["ok"], r["note"]) for r in _rows(folder["tmp"], "ad_creative_crop")] \
        == [(None, "story_not_placed:no_crop_step")]
    assert not [r for r in _rows(folder["tmp"], "ad_creative_upload") if r["ok"] is False]


def test_a_single_file_whose_crop_was_not_shown_still_succeeds_with_a_warning(monkeypatch, folder):
    monkeypatch.setattr(md, "upload_ad_creative", lambda *a, **k: False)
    monkeypatch.setattr(md, "_close_creative_setup_modal", lambda page: True)
    monkeypatch.setattr(md, "_verify_ad_media_present", lambda page: True)
    assert md.upload_ad_creative_with_retry(object(), "video", folder["single"]) is True
    assert [(r["ok"], r["note"]) for r in _rows(folder["tmp"], "ad_creative_crop")] \
        == [(None, "crop_step_not_shown")]


def test_a_successful_upload_of_a_pair_without_crop_is_a_success_with_a_warning(monkeypatch, folder):
    # ACL-1476: the post file stays on the ad; the warning is named.
    uploaded = []
    monkeypatch.setattr(md, "upload_ad_creative",
                        lambda page, kind, path, **k: uploaded.append(path) or True)
    assert md.upload_ad_creative_with_retry(object(), "video", folder["pair"]) is True
    assert [Path(p).name for p in uploaded] == ["4_es_2403_post.mp4"]
    assert md.last_upload_why() == ""
    assert md.last_crop_warning() == crop_step.NO_CROP_STEP


def test_a_successful_upload_whose_crop_was_done_stays_a_success(monkeypatch, folder):
    def upload(*a, **k):
        crop_step._tls.done = True               # the walk applied Crop as planned
        return True

    monkeypatch.setattr(md, "upload_ad_creative", upload)
    assert md.upload_ad_creative_with_retry(object(), "video", folder["pair"]) is True
    assert md.last_upload_why() == ""
    assert md.last_crop_warning() == ""


def test_a_crop_failure_in_the_give_up_close_is_named_not_erased(monkeypatch, folder):
    closes = []

    def close(page):
        closes.append(1)
        if len(closes) == 3:                      # the final clean-up close
            return _crop_fails_in_close_walk(page)
        return True

    monkeypatch.setattr(md, "upload_ad_creative", lambda *a, **k: False)
    monkeypatch.setattr(md, "_close_creative_setup_modal", close)
    monkeypatch.setattr(md, "_verify_ad_media_present", lambda page: False)
    assert md.upload_ad_creative_with_retry(object(), "video", folder["single"]) is False
    assert md.last_upload_why() == EXPAND_ON


# ── the known-file paths ("media already on ad") ─────────────────────────

@pytest.fixture
def known(monkeypatch, folder):
    monkeypatch.setattr(md, "upload_already_succeeded", lambda path: True)
    monkeypatch.setattr(md, "upload_ad_creative", lambda *a, **k: False)
    monkeypatch.setattr(md, "_media_block_filled_now", lambda page: False)
    monkeypatch.setattr(md, "_wizard_past_media", lambda page: False)
    monkeypatch.setattr(md, "_creative_wizard_open", lambda page: False)
    monkeypatch.setattr(md, "_close_creative_setup_modal", lambda page: True)
    return folder


def test_known_file_media_already_on_ad_of_a_pair_succeeds_with_a_warning(monkeypatch, known):
    monkeypatch.setattr(md, "_media_block_filled_now", lambda page: True)
    assert md.upload_ad_creative_with_retry(object(), "video", known["pair"]) is True
    assert md.last_upload_why() == ""
    assert md.last_crop_warning() == crop_step.NO_CROP_STEP


def test_known_file_media_already_on_ad_of_a_single_file_succeeds(monkeypatch, known):
    monkeypatch.setattr(md, "_media_block_filled_now", lambda page: True)
    assert md.upload_ad_creative_with_retry(object(), "video", known["single"]) is True


def test_known_file_wizard_at_text_with_a_crop_failure_fails(monkeypatch, known):
    monkeypatch.setattr(md, "_wizard_past_media", lambda page: True)
    monkeypatch.setattr(md, "_close_creative_setup_modal", _crop_fails_in_close_walk)
    assert md.upload_ad_creative_with_retry(object(), "video", known["single"]) is False
    assert md.last_upload_why() == EXPAND_ON
    assert _rows(known["tmp"], "ad_creative_upload")[-1]["diag"]["where"] \
        == "media_already_on_ad_wizard_at_text"


def test_known_file_after_wizard_closed_with_a_crop_failure_fails(monkeypatch, known):
    state = {"closed": False}

    def close(page):
        state["closed"] = True
        return _crop_fails_in_close_walk(page)

    monkeypatch.setattr(md, "_creative_wizard_open", lambda page: True)
    monkeypatch.setattr(md, "_close_creative_setup_modal", close)
    monkeypatch.setattr(md, "_media_block_filled_now", lambda page: state["closed"])
    assert md.upload_ad_creative_with_retry(object(), "video", known["single"]) is False
    assert md.last_upload_why() == EXPAND_ON
    assert _rows(known["tmp"], "ad_creative_upload")[-1]["diag"]["where"] \
        == "media_already_on_ad_after_wizard_closed"


def test_known_file_reuse_of_a_pair_without_crop_succeeds_with_a_warning(monkeypatch, known):
    monkeypatch.setattr(md, "upload_ad_creative", lambda *a, **k: True)
    assert md.upload_ad_creative_with_retry(object(), "video", known["pair"]) is True
    assert md.last_crop_warning() == crop_step.NO_CROP_STEP


# ── L4: the user's sentence ───────────────────────────────────────────────

def test_ad_form_gone_has_an_english_sentence():
    from src.fb_ads_rpa import simple_create

    text = simple_create.creative_why_text(md.AD_FORM_GONE)
    assert text.startswith("After the media step Meta stopped showing this ad's form")
    assert "crop_vertical_not_story" in simple_create.creative_why_text("crop_vertical_not_story")
    assert simple_create.creative_why_text("upload_stalled") == ""
