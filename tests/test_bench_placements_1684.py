# -*- coding: utf-8 -*-
"""Doc120: a complete "Stories, Status, Reels" list that flickers is still read as "not offered" (ACL-1684).

Bench. FacebookDoc120, run 4e4969867d28 (2026-10-07 10:53 UTC, 00_placements): the capture
has the same layout as the B90 one (`button[aria-label=Expand|Collapse]` expanders, five
rows in the Stories list, no "Messenger Stories" row in the DOM). The check read
`stable: false, reads: 6, waited_s: 6.4, reopened: 2` with `end: true, hidden: 0`: Meta folded
the list twice, the re-openings used up the wait counted from the first read, and the row
count changed between reads. The fixture is therefore the B90 card (placements_button_expanders_2026_10_06.html); the flicker (a fold
and a changing `rows`) is played by wrapping `_CATEGORY_END_JS`.
"""
from __future__ import annotations

import pytest

from src.fb_ads_rpa import meta_dom as md
from tests.bench_support import browser  # noqa: F401
from tests.test_bench_placements_1629 import _load_b, page  # noqa: F401

STORIES = "Stories, Status, Reels"


def _wrap(pre: str = "", post: str = "") -> str:
    """The robot's end-proof JS with page-side code before and after it (Stories list only)."""
    return ("(name) => { const stories = name === 'Stories, Status, Reels';"
            " if (stories) { window.__n = (window.__n || 0) + 1; }"
            " const n = window.__n || 0;"
            " const wait = (ms) => { const t = Date.now(); while (Date.now() - t < ms) {} };"
            + pre
            + " const out = (" + md._CATEGORY_END_JS + ")(name);"
            + post
            + " return out; }")


def test_a_list_folded_twice_and_with_a_flickering_row_count_is_not_offered(page, monkeypatch):
    """Doc120: two folds, each slow to redraw, eat a wait counted from the first read; the
    row count differs on every read. The list is complete (end, bottom, not busy, nothing
    hidden) and the label is absent: "Messenger Stories" is not offered, the step passes."""
    pre = (" if (stories && (n === 3 || n === 5)) {"
           " wait(400); window.__sim.cats[name].btn.click(); }")
    post = " if (stories && out.rows !== undefined) out.rows += (n % 2);"
    monkeypatch.setattr(md, "_CATEGORY_END_JS", _wrap(pre, post))
    monkeypatch.setattr(md, "_SUBITEMS_SETTLE_MAX_S", 0.7)
    _load_b(page)
    report: dict = {}
    assert md.fill_placements(page, report) is True, report.get("not_offered_check")
    check = report["not_offered_check"][STORIES]
    assert check["reopened"] == 2, check
    assert check["complete"] and check["stable"], check
    assert check["end"]["end"] is True and check["end"]["hidden"] == 0, check
    assert report["subitems_not_offered"] == ["Messenger Stories"]
    assert report["subitems_missing"] == []


def test_a_list_that_never_proves_its_end_still_blocks(page, monkeypatch):
    """A truly incomplete list (hidden rows, never at the bottom) is never judged: the wait
    is bounded, nothing is called "not offered", the absent placement stays missing."""
    post = " if (stories) { out.end = false; out.at_bottom = false; out.hidden = 1 + (n % 2); }"
    monkeypatch.setattr(md, "_CATEGORY_END_JS", _wrap("", post))
    monkeypatch.setattr(md, "_SUBITEMS_SETTLE_MAX_S", 0.6)
    monkeypatch.setattr(md, "_SUBITEMS_SETTLE_HARD_S", 1.5)
    _load_b(page)
    report: dict = {}
    assert md.fill_placements(page, report) is False
    check = report["not_offered_check"][STORIES]
    assert check["complete"] is False and check["stable"] is False, check
    assert "subitems_not_offered" not in report
    assert "Messenger Stories" in report["subitems_missing"]


def test_labels_that_never_stop_changing_give_up_at_the_hard_limit(page, monkeypatch):
    """The end proof holds but the list never settles (a new row set on every read): the
    wait ends at the hard limit and the placement is not judged."""
    post = " if (stories) { out.hidden = n; }"
    monkeypatch.setattr(md, "_CATEGORY_END_JS", _wrap("", post))
    monkeypatch.setattr(md, "_SUBITEMS_SETTLE_MAX_S", 0.6)
    monkeypatch.setattr(md, "_SUBITEMS_SETTLE_HARD_S", 1.5)
    _load_b(page)
    report: dict = {}
    assert md.fill_placements(page, report) is False
    check = report["not_offered_check"][STORIES]
    assert check["stable"] is False and check["complete"] is False, check
    assert check["waited_s"] < 10, check
    assert "subitems_not_offered" not in report


def test_an_offered_but_hidden_placement_is_never_not_offered(page):
    """"Messenger Stories" is in the Stories list but its row is hidden: the placement is
    offered, so it must not be reported as "not offered"."""
    _load_b(page)
    page.evaluate(r"""() => {
      const c = window.__sim.cats['Stories, Status, Reels'];
      const row = c.rows[c.rows.length - 1].cloneNode(true);
      const label = row.querySelector('label');
      label.textContent = 'Messenger Stories';
      row.id = 'sim_messenger_stories_check';
      row.style.display = 'none';
      c.rows[c.rows.length - 1].after(row);
    }""")
    report: dict = {}
    md.fill_placements(page, report)
    assert "Messenger Stories" not in report.get("subitems_not_offered", [])
