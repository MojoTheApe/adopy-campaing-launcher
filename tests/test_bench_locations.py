# -*- coding: utf-8 -*-
"""Locations with only Meta's default country left, read by the robot's location readers.

Bench (ACL-1433). `locations_default_only_2026_10_01.html` — the Locations
card of the failing `audience_location` captures:

- `simple_create` — 0131b6fa3476/01 (CO_bnc FX, single-screen editor, 01.10):
  "Colombia" typed in "Search locations", no suggestion list
  (`aria-expanded=false`), only "United States" included and its remove
  control disabled (Meta keeps the last location).
- `classic` — abd6661ea5d5/01 (SA PF, classic editor, 30.09): the same card,
  empty search box. 53e1f3ea8bd8/01 and 64cc3d5510b2/01 show the same.

Live the step failed without a reason in `diag`; these readers are what it
had: the only row is the default country and it cannot be removed, and no
suggestion list is open for the typed country.
"""
from __future__ import annotations

import pytest

from src.fb_ads_rpa import meta_dom as md
from tests.bench_support import FIXTURES, browser, fragment, offline_page  # noqa: F401

FIXTURE = FIXTURES / "locations_default_only_2026_10_01.html"


@pytest.mark.parametrize("name", ["simple_create", "classic"])
def test_only_the_default_country_and_it_is_locked(offline_page, name):
    offline_page.set_content(fragment(FIXTURE, name))
    rows = md._location_rows(offline_page)
    assert [(r["name"], r["disabled"]) for r in rows] == [("United States", True)]


def test_typed_country_has_no_suggestions(offline_page):
    offline_page.set_content(fragment(FIXTURE, "simple_create"))
    assert offline_page.input_value("input[placeholder='Search locations']") == "Colombia"
    assert md._wait_location_options(offline_page, timeout_s=0.5) is False


def test_an_open_list_is_seen(offline_page):
    """Control: the same field with Meta's list open counts as suggestions."""
    offline_page.set_content(fragment(FIXTURE, "simple_create"))
    offline_page.evaluate("() => document.querySelector(\"input[placeholder='Search locations']\")"
                          ".setAttribute('aria-expanded', 'true')")
    assert md._wait_location_options(offline_page, timeout_s=0.5) is True
