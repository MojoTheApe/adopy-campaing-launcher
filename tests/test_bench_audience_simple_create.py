# -*- coding: utf-8 -*-
"""Single-screen editor Audience card left with Advantage+ on (run 3100d06ad51e, 30.09).

Bench (ACL-1433). `audience_simple_create_advantage_on_2026_09_30.html`, the
whole Audience card of two failing captures of the same campaign (CO_bnc Mix,
single-screen editor, 20:25–20:26; captures truncated, the card whole):

- `age_step` (01_audience_age): "Advantage+ on"; summary rows (Locations
  United States, Age 18 - 65+, ...) and the open fields' body
  (Minimum/Maximum age) both in the DOM; the age triggers the robot reads
  are not visible — the step `audience_age` failed.
- `suggestion_step` (02_advantage_plus_audience): still "Advantage+ on" after
  the audience steps; Locations Colombia, Age 18/65+, Detailed targeting
  "Your suggestion" (interests replaced), Languages "Spanish (All)" — the step
  `advantage_plus_audience` failed: Advantage+ audience stayed on.
"""
from __future__ import annotations

import pytest

from src.fb_ads_rpa import meta_dom as md
from tests.bench_support import FIXTURES, browser, fragment, offline_page  # noqa: F401

FIXTURE = FIXTURES / "audience_simple_create_advantage_on_2026_09_30.html"


@pytest.mark.parametrize("name", ["age_step", "suggestion_step"])
def test_advantage_plus_audience_reads_on(offline_page, name):
    offline_page.set_content(fragment(FIXTURE, name))
    assert md._advantage_plus_audience_on(offline_page) is True


def test_age_step_capture_has_no_readable_age_triggers(offline_page):
    """What `fill_audience_age` had live: no visible age trigger to read."""
    offline_page.set_content(fragment(FIXTURE, "age_step"))
    assert md._age_triggers_read(offline_page) == []


def test_suggestion_step_capture_reads_the_full_age_range(offline_page):
    offline_page.set_content(fragment(FIXTURE, "suggestion_step"))
    assert md._age_range_read(offline_page) == ["18", "65+"]
    assert md._languages_read(offline_page, ["Spanish"]) == ["Spanish (All)"]
