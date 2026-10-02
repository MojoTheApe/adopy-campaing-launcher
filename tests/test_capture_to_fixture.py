# -*- coding: utf-8 -*-
"""scripts/capture_to_fixture.py: a live capture becomes a scrubbed, trimmed fixture (ACL-1433)."""
from __future__ import annotations

import json
import re

import pytest

from scripts import capture_to_fixture as ctf
from tests.bench_support import fragments

CAMPAIGN = "CO_xyz_All_Video_FX_35_01.10"
# Built at run time so the repository scanner never sees an id-shaped literal.
ACT = "act" + "=" + "9876" * 4
UUID = "-".join(["550e8400", "e29b", "41d4", "a716", "446655440001"])

PAGE = f"""<!doctype html><html><head><script>var token="EAAB{'x' * 40}";</script>
<style>.a{{color:red}}</style></head><body>
<div class="x1 x2" data-auto-logging-id="f6dc" data-surface="/am/business_scope:personal:12345:Jane Roe">
  <div role="heading" aria-level="3" id="js_1xu">Placements</div>
  <div aria-labelledby="js_1xu js_2ab" role="region" data-surface="/am/editor:ad_set:120251922781320070">
    <span>Campaign {CAMPAIGN}</span>
    <a href="https://adsmanager.facebook.com/adsmanager/manage?{ACT}&id=120251688107840070">link</a>
    <span>Contact jane.roe@example.org or +57 300 123 4567</span>
    <span>Tracking https://tkr.offer-site.com/abc?x=1 and tkr.offer-site.com</span>
    <span>File 4_factoryblack_1962_ar_facebook_1080x1080.jpg and 4_es_2403_post.mp4</span>
    <span>Object 120251922781320070 again 120251922781320070</span>
    <span>Pixel Facebook_6540</span>
    <span>Profile {UUID}</span>
    <span>Hash a1b2c3d4e5f6a7b8c9d0a1b2c3d4e5f6a7b8</span>
    <label for="js_9">Name</label><input id="js_9" name="js_21i" value="Real Name" placeholder="Enter">
    <svg viewBox="0 0 1 1"><path d="M0 0"/></svg><img src="https://scontent.example/x.jpg">
    <iframe src="https://evil.example"></iframe>
    <!-- a comment with {ACT} -->
    <div onclick="steal()" style="display:none" data-rpa-placements-marker="1" tabindex="0">Inner</div>
  </div>
</div>
<div role="dialog"><div>Other block</div></div>
</body></html>"""


@pytest.fixture
def capture(tmp_path):
    d = tmp_path / "rpa_pages" / "abcdef123456" / "01_placements"
    d.mkdir(parents=True)
    (d / "page.html").write_text(PAGE, encoding="utf-8")
    (d / "context.json").write_text(json.dumps({
        "step": "placements", "run_id": "abcdef123456", "campaign": CAMPAIGN,
        "ts": "2026-10-01T08:00:58+00:00", "html_truncated": True}), encoding="utf-8")
    return d


def _build(capture, *specs, **kw):
    return ctf.build_fixture(capture, [ctf.parse_fragment(s) for s in specs], **kw)


def test_only_the_named_block_is_kept(capture):
    text, info = _build(capture, "card=[role=heading]|text=Placements|up=1")
    body = fragments_from(text)["card"]
    assert "Other block" not in body
    assert info == {"step": "placements", "run": "abcdef123456", "capture": "01_placements",
                    "source": "page.html", "date": "2026_10_01", "truncated": True,
                    "fragments": {"card": 1}}


def fragments_from(text: str) -> dict[str, str]:
    return {m.group(1): m.group(2) for m in re.finditer(
        r"<!-- fragment: (\w+) \| source: [^>]*-->\n(.*?)(?=\n<!-- fragment: |\Z)", text, re.S)}


def test_structure_is_kept_and_everything_else_goes(capture):
    text, _ = _build(capture, "card=[role=heading]|text=Placements|up=1")
    body = fragments_from(text)["card"]
    for gone in ("class=", "data-auto-logging-id", "onclick", "style=", "<script", "<style",
                 "<svg", "<img", "<iframe", "steal", "data-rpa-placements-marker", "a comment"):
        assert gone not in body, gone
    assert 'role="heading"' in body and 'aria-level="3"' in body and 'tabindex="0"' in body
    assert body.count("<i></i>") == 2                       # svg and img stand-ins
    assert 'href="#"' in body


def test_render_ids_become_synthetic_and_stay_linked(capture):
    text, _ = _build(capture, "card=[role=heading]|text=Placements|up=1")
    body = fragments_from(text)["card"]
    assert "js_" not in body
    head = re.search(r'role="heading" aria-level="3" id="(syn_\d+)"', body).group(1)
    assert f'aria-labelledby="{head} syn_' in body
    label_for = re.search(r'<label for="(syn_\d+)"', body).group(1)
    assert f'id="{label_for}"' in body


def test_identifiers_are_scrubbed(capture):
    text, _ = _build(capture, "card=[role=heading]|text=Placements|up=1", replace=[("Real Name", "Sample Name")])
    for leaked in (CAMPAIGN, "9876987698769876", "120251922781320070", "jane.roe", "300 123 4567",
                   "offer-site", "factoryblack", "2403_post", "Facebook_6540", "Jane Roe", "personal:12345",
                   "446655440001", "a1b2c3d4e5f6", "Real Name", "adsmanager.facebook.com/adsmanager"):
        assert leaked not in text, leaked
    for synthetic in ("Sample Campaign 01", "user@example.com", "+10000000000", "example.com",
                      "sample_media_01.jpg", "sample_media_02.mp4", "Sample Account",
                      "business_scope:personal:0:Sample Person", "TOKEN_REDACTED", "Sample Name",
                      "00000000-0000-0000-0000-000000000000"):
        assert synthetic in text, synthetic
    # The same object id gets the same synthetic number everywhere.
    nums = re.findall(r"Object (\d+) again (\d+)", text)[0]
    assert nums[0] == nums[1]
    assert ctf.secret_findings(text) == []


def test_meta_surface_names_are_not_mistaken_for_tokens():
    s = ctf.Scrubber()
    v = "/am/editor_drawer/editor:ad/ad_image_video_selector_section/lib:ads_sfcrop_tool_preview_overlay_button_ef10"
    assert s(v) == v
    assert s("Facebook Feed") == "Facebook Feed" and s("facebook.com") == "facebook.com"


def test_a_missing_fragment_refuses(capture):
    with pytest.raises(ValueError, match="not found"):
        _build(capture, "x=[role=tree]")


def test_what_the_scanner_flags_refuses(capture, monkeypatch):
    monkeypatch.setattr(ctf.Scrubber, "__call__", lambda self, t: t)
    with pytest.raises(ValueError, match="identifiers"):
        _build(capture, "card=[role=heading]|text=Placements|up=1")


def test_block_captures_are_read_when_there_is_no_page(tmp_path):
    d = tmp_path / "r" / "02_wizard_crop"
    d.mkdir(parents=True)
    (d / "block_creative_wizard.html").write_text('<div role="dialog" aria-labelledby="js_1"><b id="js_1">Crop</b></div>',
                                                 encoding="utf-8")
    text, info = ctf.build_fixture(d, [ctf.parse_fragment("crop=[role=dialog]")])
    assert info["source"] == "block_creative_wizard.html"
    assert "truncated no (block capture)" in text and "unknown_date" == info["date"]


def test_names_and_specs_are_normalised():
    assert ctf.normalise_name("Placements Doc19!") == "placements_doc19"
    spec = ctf.parse_fragment("card=[role=heading]|text=Placements|up=17")
    assert (spec.name, spec.selector, spec.texts, spec.up, spec.smallest) == (
        "card", "[role=heading]", ["Placements"], 17, True)
    with pytest.raises(ValueError):
        ctf.parse_fragment("bad name=div")
    with pytest.raises(ValueError):
        ctf.parse_fragment("x=div|wat")


def test_cli_writes_fixture_and_stub_and_appends(capture, tmp_path):
    out = tmp_path / "fixtures"
    stub = tmp_path / "test_stub.py"
    assert ctf.main([str(capture), "--name", "Doc19 Placements", "--out", str(out),
                     "--fragment", "card=[role=heading]|text=Placements|up=1", "--stub", str(stub)]) == 0
    fx = out / "doc19_placements_2026_10_01.html"
    assert set(fragments(fx)) == {"card"}
    assert "doc19_placements_2026_10_01.html" in stub.read_text(encoding="utf-8")
    compile(stub.read_text(encoding="utf-8"), str(stub), "exec")
    # Same fragment again is refused; a new one is appended; overwrite needs --force.
    assert ctf.main([str(capture), "--name", "doc19_placements", "--out", str(out), "--append",
                     "--fragment", "card=[role=dialog]"]) == 1
    assert ctf.main([str(capture), "--name", "doc19_placements", "--out", str(out), "--append",
                     "--fragment", "other=[role=dialog]"]) == 0
    assert set(fragments(fx)) == {"card", "other"}
    assert ctf.main([str(capture), "--name", "doc19_placements", "--out", str(out),
                     "--fragment", "card=[role=dialog]"]) == 1


def test_every_bench_fixture_cut_by_the_tool_passes_the_scanner():
    from tests.bench_support import FIXTURES

    for p in FIXTURES.glob("*.html"):
        text = p.read_text(encoding="utf-8")
        if "cut by scripts/capture_to_fixture.py" in text:
            assert ctf.secret_findings(text) == [], p.name
