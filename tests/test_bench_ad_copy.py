# -*- coding: utf-8 -*-
"""Ad creative section without a Headline field, with the real "Add text option".

Bench (ACL-1433). `ad_copy_headline_missing_2026_10_01.html` — the whole
`.../ad_creative_section` block of cc8fcfa17ae9/08_ad_copy (CO_bnc FX_Y,
classic editor, video ad, 01.10 08:20; capture truncated, the block whole).
The step `ad_copy` failed: primary text was typed, then four attempts at the
headline ended `locator_count_zero` (`comboboxCount 4`). In this block Meta
renders `message_field`, `text_enhancements_inline_adoption_field`,
`music_on_reels_field`, `self_disclosure_field` — and **no `headline_field`
at all**. The ad copy was replaced with synthetic lines.

The same block is the first live capture of the real **"Add text option"**
control (ACL-1344 had only screenshots): `#add-bodies-option-button` >
`role=button` with `data-surface=".../message_field/lib:dco-add-asset-button"`.
"""
from __future__ import annotations

from src.fb_ads_rpa import meta_dom as md
from tests.bench_support import FIXTURES, browser, fragment, offline_page  # noqa: F401

FIXTURE = FIXTURES / "ad_copy_headline_missing_2026_10_01.html"


def test_primary_text_field_is_found_and_headline_is_not(offline_page):
    """What the robot saw live: the body field marks, the headline does not."""
    offline_page.set_content(fragment(FIXTURE, "creative"))
    assert md.mark_ad_copy_field(offline_page, "message_field", md._AD_COPY_FIELDS["message_field"], "body")
    assert offline_page.locator('[data-rpa-field="body"]').count() == 1
    assert not md.mark_ad_copy_field(offline_page, "headline_field", md._AD_COPY_FIELDS["headline_field"], "title")
    assert offline_page.locator('[data-surface$="/headline_field"]').count() == 0


def test_the_real_add_text_option_is_found(offline_page):
    offline_page.set_content(fragment(FIXTURE, "creative"))
    got = md._text_options(offline_page, "scan")
    assert got["add_found"] is True and got["add_how"] == "surface" and got["add_count"] == 1
    assert [b["body"] for b in got["boxes"]] == [True]
    assert got["boxes"][0]["text"].startswith("Sample primary text line 1")


def test_add_text_option_point_is_the_meta_button(offline_page):
    offline_page.set_content(fragment(FIXTURE, "creative"))
    got = md._text_options(offline_page, "add_point")
    hit = offline_page.evaluate(
        "([x, y]) => { const e = document.elementFromPoint(x, y);"
        " const b = e && e.closest('[role=button]'); return b && b.getAttribute('data-surface'); }",
        [got["point"]["x"], got["point"]["y"]])
    assert hit.endswith("/message_field/lib:dco-add-asset-button")
