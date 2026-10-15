# -*- coding: utf-8 -*-
"""Ads 2-3: the new ad's form gone after the wizard is named in ~30 s, not 2 x 3 min (ACL-1426).

Live runs 04c8c214d2ba, 0bd520418dbe and 4121938e6dc3 (Facebook_1443, single-screen
editor, 2026-10-01): after the library pick and the wizard walk of ads 2-3 the
Ad section had an EMPTY `/am/editor:ad` surface (no ad name field, no Media
block, no "Set up creative") and the right column showed "Verifying your
changes" with the preflight error `#100 lead_gen_form_id`; it stayed so to the
end of the campaign. The media check waited 150 polls (~3 min), the retry loop
checked again (~3 min) and tried a second wizard (80-120 s) that could not open.
Fixture: `ad_form_gone_verifying_2026_10_01.html` (scrubbed).
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import meta_dom as md  # noqa: E402
from src.fb_ads_rpa import run_telemetry as rt  # noqa: E402

FIX = ROOT / "tests" / "fixtures" / "ads_manager"


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
    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: time.sleep(0.01))
    monkeypatch.setattr(rt, "TELEMETRY_PATH", tmp_path / "telemetry.jsonl")
    md._creative_tls.why = ""
    md._creative_tls.phases = []
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    p = ctx.new_page()
    p.route("**/*", lambda route: route.abort())
    try:
        yield p
    finally:
        ctx.close()
        md._creative_tls.why = ""


def _load(page, name):
    page.set_content("<!doctype html><html><body>" + (FIX / name).read_text(encoding="utf-8")
                     + "</body></html>")


def test_the_captured_state_reads_as_form_gone(page):
    _load(page, "ad_form_gone_verifying_2026_10_01.html")
    got = md._ad_form_gone(page)
    assert got["gone"] is True
    assert got["verifying"] is True
    assert any("lead_gen_form_id" in e for e in got["errors"])


def test_a_mounted_media_block_is_not_form_gone(page):
    page.set_content("<!doctype html><html><body><div role='heading' aria-level='2'>Ad</div>"
                     + (FIX / "simple_create_new_ad_media_2026_09_30.html").read_text(encoding="utf-8")
                     + "</body></html>")
    assert md._ad_form_gone(page)["gone"] is False


def test_an_ad_name_field_is_not_form_gone(page):
    page.set_content("<div role='heading' aria-level='2'>Ad</div>"
                     "<input placeholder='Enter your ad name here...'>")
    assert md._ad_form_gone(page)["gone"] is False


def test_the_media_check_names_it_instead_of_waiting_three_minutes(page, monkeypatch, tmp_path):
    _load(page, "ad_form_gone_verifying_2026_10_01.html")
    monkeypatch.setattr(md, "_simple_page", lambda p: True)
    monkeypatch.setattr(md, "wait_editor_saved", lambda *a, **k: True)
    monkeypatch.setattr(md._simple, "show_media_block", lambda *a, **k: {"mounted": False})
    monkeypatch.setattr(md, "AD_FORM_GONE_S", 0.5)
    started = time.time()
    assert md._verify_ad_media_present(page) is False
    assert time.time() - started < 20
    assert md._creative_tls.why == md.AD_FORM_GONE
    rows = [json.loads(x) for x in (tmp_path / "telemetry.jsonl").read_text(encoding="utf-8").splitlines()]
    row = [r for r in rows if r["step"] == "ad_media_check"][-1]
    assert row["note"] == md.AD_FORM_GONE and row["diag"]["verifying"] is True


def test_the_retry_loop_does_not_check_twice_or_open_a_second_wizard(monkeypatch):
    """Named once → no second media check, no second upload attempt."""
    calls = {"upload": 0, "check": 0}

    def upload(*a, **k):
        calls["upload"] += 1
        md._creative_why(md.AD_FORM_GONE)
        return False

    monkeypatch.setattr(md, "upload_ad_creative", upload)
    monkeypatch.setattr(md, "_verify_ad_media_present",
                        lambda p: calls.__setitem__("check", calls["check"] + 1) or False)
    monkeypatch.setattr(md, "_close_creative_setup_modal", lambda p: True)
    monkeypatch.setattr(md, "_page_read_trouble", lambda *a, **k: None)
    monkeypatch.setattr(md, "upload_already_failed", lambda p: False)
    monkeypatch.setattr(md, "upload_already_succeeded", lambda p: False)
    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    md._creative_tls.why = ""
    assert md._upload_ad_creative_with_retry(object(), "video", "C:/x/4_es_2403_post.mp4",
                                             reload=False) is False
    assert calls == {"upload": 1, "check": 0}
    assert md.last_upload_why() == md.AD_FORM_GONE
