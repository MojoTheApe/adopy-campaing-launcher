# -*- coding: utf-8 -*-
"""Conversion card with a button group in its heading and the event still empty.

Bench (ACL-1433). `conversion_event_heading_group_2026_10_01.html` — the
Conversion card of cc8fcfa17ae9/01_conversion_event (CO_bnc FX_Y, classic
ad set editor, 01.10 08:14; capture truncated, the card whole). The heading
now holds "Improve conversion location" next to "Conversion" (the same move
Meta made in the Placements heading on 28.09); Conversion location Website,
Dataset set (pixel name replaced), **Conversion event empty** ("Select an
event or search by name"). The step `conversion_event` failed live.
"""
from __future__ import annotations

from src.fb_ads_rpa import meta_dom as md
from tests.bench_support import FIXTURES, browser, fragment, offline_page  # noqa: F401

FIXTURE = FIXTURES / "conversion_event_heading_group_2026_10_01.html"


def test_the_event_field_is_there_and_empty(offline_page):
    offline_page.set_content(fragment(FIXTURE, "card"))
    assert md.conversion_event_field_present(offline_page) is True
    assert md.is_lead_conversion_event_selected(offline_page) is False


def test_a_chosen_lead_event_is_read(offline_page):
    """Positive control on the same markup: the placeholder replaced by «Lead»."""
    offline_page.set_content(fragment(FIXTURE, "card"))
    offline_page.evaluate(r"""() => {
      const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
      for (let t = w.nextNode(); t; t = w.nextNode())
        if (t.nodeValue.trim() === 'Select an event or search by name') t.nodeValue = 'Lead';
    }""")
    assert md.is_lead_conversion_event_selected(offline_page) is True
