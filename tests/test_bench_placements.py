# -*- coding: utf-8 -*-
"""Placements layouts met live since 29.09, run through the robot's `fill_placements`.

Bench (ACL-1433).

1. `placements_doc19_2026_10_01.html` — SA Doc19 (run 5edc0e7cca0a,
   01_placements, 2026-10-01 08:00 UTC; capture truncated, the card is whole).
   The classic ad set editor with SIX categories (a new "Marketing messages"
   between "Search results" and "Apps and sites") and a "Stories, Status,
   Reels" list WITHOUT "Messenger Stories". The step failed with
   `subitems_missing: ["Messenger Stories"]` — the only miss. The same code
   passed that morning on other accounts: Meta's variation, not a regression.
   The captured card is the robot's own marked block after the failure; the
   Feeds and Stories categories are collapsed in it, so their rows are built
   here by the accordion emulation of `test_rpa_placements_category_recollapse`
   from a real In-stream row, with the labels the robot saw live
   (`diag.subitems_seen`).

2. `placements_simple_create_2026_09_30.html` — CO_bnc Mix (run cb854b07857b,
   02_placements, 2026-09-30, not truncated): the single-screen editor's
   Placements card, open (`aria-pressed=true`), shows only "Advantage+ on" and
   "About placements" — no categories, no "Account controls", no "Show more
   settings". The step failed with `placements_card_not_found`, stage `tag`.
   The capture shows why: the card body is only a "Loading..." progressbar
   (the same card collapsed, 01_advantage_plus_still_on, had "Account
   controls" in its summary).

3. `placements_simple_create_summary_2026_10_01.html` — no capture: rebuilt
   from the owner's screenshot text after SA_ARC_All_Video_IPO_35_01.10
   (tester, 01.10, single-screen editor). Placements were set ("Included: 7,
   Allowed with limited spend: 0, Excluded: 13"), the list behind the summary
   collapsed, and the step reported a failure.

ACL-1445 fixed (1) and (2): an OFF placement Meta does not offer is "not
offered" once its category is open and its list settled (`_settle_category`
in `fill_placements`); the card is found by its own `data-surface` anchor and
its body waited for. (3) is not trusted by its counts: the summary is opened and its rows read
back, or the step fails (review of PR #449).
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from src.fb_ads_rpa import meta_dom as md
from tests.bench_support import FIXTURES, browser, fragment  # noqa: F401
from tests.test_rpa_placements_category_recollapse import _SIM_JS

DOC19 = FIXTURES / "placements_doc19_2026_10_01.html"
SIMPLE = FIXTURES / "placements_simple_create_2026_09_30.html"
SUMMARY = FIXTURES / "placements_simple_create_summary_2026_10_01.html"

#: `diag.subitems_seen` of 5edc0e7cca0a/01_placements, by category. No
#: "Messenger Stories" anywhere on the page (one match in the whole 4 MB
#: capture, and it is in the robot's own report text, not in the editor).
DOC19_ROWS = {
    "Feeds": [
        ("Facebook Feed", True, False),
        ("Facebook profile feed", True, False),
        ("Instagram feed", True, False),
        ("Instagram profile feed", True, False),
        ("Facebook Marketplace", True, False),
        ("Facebook right column", True, False),
        ("Instagram Explore home", True, False),
        ("Facebook Business Explore", True, False),
        ("Threads feed", True, False),
        ("Facebook Notifications", True, False),
    ],
    "Stories, Status, Reels": [
        ("Instagram Stories", True, False),
        ("Facebook Stories", True, False),
        ("Instagram Reels", True, False),
        ("Facebook Reels", True, False),
        ("WhatsApp Status", True, False),
    ],
    "In-stream ads for reels": [
        ("Facebook in-stream reels", True, False),
        ("Ads on Facebook Reels", True, False),
    ],
}


@pytest.fixture
def page(browser, monkeypatch, tmp_path):
    monkeypatch.setattr(md, "_sleep_h", lambda base, spread=0.3: time.sleep(min(base, 0.05)))
    monkeypatch.setattr(md, "ensure_account_placement_exclusions", lambda pg, report=None: False)
    monkeypatch.setattr(md, "_PLACEMENTS_CARD_LOAD_WAIT_S", 1.5)
    monkeypatch.chdir(tmp_path)          # output/debug_placements_fill.txt stays out of the repo
    (tmp_path / "output").mkdir()
    ctx = browser.new_context(viewport={"width": 1000, "height": 800})
    ctx.route("**/*", lambda route: route.abort())
    p = ctx.new_page()
    try:
        yield p
    finally:
        ctx.close()


def _sim_cfg(rows: dict | None = None) -> dict:
    rows = DOC19_ROWS if rows is None else rows
    return {"rows": {k: [list(r) for r in v] for k, v in rows.items()}, "collapse": "never"}


def _load_doc19(page, rows: dict | None = None) -> None:
    page.set_content("<html><body style='margin:0;padding-top:120px'>"
                     f"{fragment(DOC19, 'card')}</body></html>")
    page.evaluate(_SIM_JS, _sim_cfg(rows))


#: Hide the rows of the given labels each time the emulation renders them and
#: show them again after `delay` ms (never when `delay` < 0) — a list that is
#: not fully rendered yet.
_LATE_ROWS_JS = r"""
([labels, delay]) => {
  const hide = (row) => {
    row.style.display = 'none';
    if (delay >= 0) setTimeout(() => { row.style.display = ''; }, delay);
  };
  const scan = (root) => {
    for (const cb of root.querySelectorAll('input[type=checkbox]'))
      if (labels.includes(cb.dataset.simLabel)) hide(cb.closest('[id^="sim-row-"]'));
  };
  new MutationObserver((ms) => {
    for (const m of ms) for (const n of m.addedNodes) if (n.nodeType === 1) scan(n);
  }).observe(document.body, { childList: true, subtree: true });
}
"""


def _state(page) -> dict[str, bool]:
    return {k: v["checked"] for k, v in page.evaluate("() => window.__sim.state").items()}


def test_doc19_layout_is_the_failing_one(page):
    _load_doc19(page)
    cats = page.evaluate(r"""() => [...document.querySelectorAll('[role=button][aria-expanded]')]
      .map(b => (b.textContent || '').replace(/\s+/g, ' ').trim())""")
    assert "Marketing messagesMarketing messages" in cats
    assert "Messenger Stories" not in page.content()


def test_doc19_every_offered_placement_is_set(page):
    """Everything Meta offers on Doc19 ends in the wanted state (read back);
    the placement Meta does not offer is named as such, not as missing."""
    _load_doc19(page)
    report: dict = {}
    md.fill_placements(page, report)
    state = _state(page)
    for label in md._SUBITEMS_ON:
        if label in state:
            assert state[label] is True, label
    for label in md._SUBITEMS_OFF:
        if label in state:
            assert state[label] is False, label
    assert report["subitems_missing"] == []
    assert report["subitems_not_offered"] == ["Messenger Stories"]


def test_doc19_a_placement_meta_does_not_offer_is_not_a_failure(page):
    """Live 2026-10-01 (Doc19, 5edc0e7cca0a): 'Messenger Stories' is no longer
    offered; its category is open and settled, so it is 'not offered'."""
    _load_doc19(page)
    report: dict = {}
    assert md.fill_placements(page, report) is True, report.get("subitems_missing")
    check = report["not_offered_check"]["Stories, Status, Reels"]
    assert check["complete"] and check["stable"] and check["absent"] == ["Messenger Stories"]
    assert check["end"]["end"] is True and check["end"]["hidden"] == 0, check["end"]


def test_a_row_rendered_late_is_waited_for_not_called_not_offered(page):
    """The row shows up a second after its category opened: the step waits for
    the list to settle and switches it off — no false 'not offered'."""
    rows = {k: list(v) for k, v in DOC19_ROWS.items()}
    rows["Stories, Status, Reels"] = rows["Stories, Status, Reels"] + [("Messenger Stories", True, False)]
    page.set_content("<html><body style='margin:0;padding-top:120px'>"
                     f"{fragment(DOC19, 'card')}</body></html>")
    page.evaluate(_LATE_ROWS_JS, [["Messenger Stories"], 1000])
    page.evaluate(_SIM_JS, _sim_cfg(rows))
    report: dict = {}
    assert md.fill_placements(page, report) is True, report
    assert _state(page)["Messenger Stories"] is False
    assert "subitems_not_offered" not in report


def test_an_unrendered_list_is_not_a_false_not_offered(page):
    """The Stories list never renders its rows: the step fails as before and
    names nothing 'not offered' — absence proves nothing until the list is there."""
    hidden = [label for label, _, _ in DOC19_ROWS["Stories, Status, Reels"]]
    page.set_content("<html><body style='margin:0;padding-top:120px'>"
                     f"{fragment(DOC19, 'card')}</body></html>")
    page.evaluate(_LATE_ROWS_JS, [hidden, -1])
    page.evaluate(_SIM_JS, _sim_cfg())
    report: dict = {}
    assert md.fill_placements(page, report) is False
    assert "subitems_not_offered" not in report
    assert {"Messenger Stories", "WhatsApp Status", "Instagram Stories"} <= set(report["subitems_missing"])
    assert report["not_offered_check"]["Stories, Status, Reels"]["complete"] is False


def test_a_row_arriving_after_the_quiet_window_is_still_set(page):
    """M1 (review #449): the row appears 3 s after its category opened, later
    than the 1.5 s quiet window. While a row of the open category is not shown
    the end is not proven, so the step keeps waiting and switches it off."""
    rows = {k: list(v) for k, v in DOC19_ROWS.items()}
    rows["Stories, Status, Reels"] = rows["Stories, Status, Reels"] + [("Messenger Stories", True, False)]
    page.set_content("<html><body style='margin:0;padding-top:120px'>"
                     f"{fragment(DOC19, 'card')}</body></html>")
    page.evaluate(_LATE_ROWS_JS, [["Messenger Stories"], 3000])
    page.evaluate(_SIM_JS, _sim_cfg(rows))
    report: dict = {}
    assert md.fill_placements(page, report) is True, report
    assert _state(page)["Messenger Stories"] is False
    assert "subitems_not_offered" not in report


#: The open category's list becomes an inner scroller, and the rows of
#: `labels` mount only once it is scrolled to its bottom — 8e9acb2 / ACL-1220
#: (17.09): "Threads feed" and "Facebook Notifications" appeared only after
#: scrolling.
_TAIL_ON_SCROLL_JS = r"""
([labels]) => {
  const apply = (region) => {
    region.style.maxHeight = '60px';
    region.style.overflowY = 'auto';
    const rows = [...region.querySelectorAll('input[type=checkbox]')]
      .filter(cb => labels.includes(cb.dataset.simLabel))
      .map(cb => cb.closest('[id^="sim-row-"]'));
    if (!rows.length) return;
    for (const r of rows) r.style.display = 'none';
    region.addEventListener('scroll', () => {
      if (region.scrollTop + region.clientHeight >= region.scrollHeight - 2)
        for (const r of rows) r.style.display = '';
    });
  };
  new MutationObserver((ms) => {
    for (const m of ms) for (const n of m.addedNodes)
      if (n.nodeType === 1 && n.matches('[role=region]')) apply(n);
  }).observe(document.body, { childList: true, subtree: true });
}
"""

FEEDS_TAIL = ["Threads feed", "Facebook Notifications"]


def test_a_tail_that_mounts_after_scrolling_is_switched_off(page):
    """B1 (review #449): the Feeds tail mounts only when its list is scrolled
    to the end — the step scrolls there and switches both rows off."""
    page.set_content("<html><body style='margin:0;padding-top:120px'>"
                     f"{fragment(DOC19, 'card')}</body></html>")
    page.evaluate(_TAIL_ON_SCROLL_JS, [FEEDS_TAIL])
    page.evaluate(_SIM_JS, _sim_cfg())
    report: dict = {}
    assert md.fill_placements(page, report) is True, report
    state = _state(page)
    assert state["Threads feed"] is False and state["Facebook Notifications"] is False
    assert report.get("subitems_not_offered") == ["Messenger Stories"]


def test_a_tail_that_never_mounts_fails_and_is_not_called_not_offered(page):
    """B1 (review #449, probe): the Feeds tail is never shown. The end of the
    list is not proven, so nothing in Feeds is 'not offered' and the step fails."""
    page.set_content("<html><body style='margin:0;padding-top:120px'>"
                     f"{fragment(DOC19, 'card')}</body></html>")
    page.evaluate(_LATE_ROWS_JS, [FEEDS_TAIL, -1])
    page.evaluate(_SIM_JS, _sim_cfg())
    report: dict = {}
    assert md.fill_placements(page, report) is False
    assert set(FEEDS_TAIL) <= set(report["subitems_missing"])
    assert not set(FEEDS_TAIL) & set(report.get("subitems_not_offered") or [])
    check = report["not_offered_check"]["Feeds"]
    assert check["complete"] is False and check["end"]["end"] is False
    assert _state(page)["Threads feed"] is True


def test_an_item_seen_in_another_category_and_still_on_fails(page):
    """B2 (review #449, probe): 'Messenger Stories' is absent from Stories but
    shows up in In-stream, locked ON. Seen and not switched off = a failure."""
    rows = {k: list(v) for k, v in DOC19_ROWS.items()}
    rows["In-stream ads for reels"] = rows["In-stream ads for reels"] + [("Messenger Stories", True, True)]
    _load_doc19(page, rows)
    report: dict = {}
    assert md.fill_placements(page, report) is False
    assert "Messenger Stories" in report["subitems_missing"]
    assert "Messenger Stories" not in (report.get("subitems_not_offered") or [])
    assert _state(page)["Messenger Stories"] is True


def test_every_wanted_subitem_has_its_category():
    assert set(md._SUBITEM_CATEGORY) == set(md._SUBITEMS_ON) | set(md._SUBITEMS_OFF)
    assert set(md._SUBITEMS_BY_CATEGORY) == {k for k, v in md._PLACEMENT_CATEGORIES.items() if v}


def _load_simple(page) -> None:
    page.set_content("<html><body style='margin:0;padding-top:120px'>"
                     f"{fragment(SIMPLE, 'card')}</body></html>")


def test_simple_create_card_failure_is_named(page):
    """The live capture: the card is found by its own anchor, its body never
    leaves 'Loading...' — named as such, not as a missing card."""
    _load_simple(page)
    report: dict = {}
    assert md.fill_placements(page, report) is False
    assert report["why"] == "placements_card_loading"
    card = report["placements_card"]
    assert card["via"] == "section" and card["tagged"] is True and card["loading"] is True
    assert card["pressed"] == "true"


def test_simple_create_card_is_found(page):
    _load_simple(page)
    report: dict = {}
    md.fill_placements(page, report)
    assert report.get("why") != "placements_card_not_found", report.get("placements_card")
    assert page.evaluate("() => !!document.querySelector("
                         "'[data-rpa-placements-marker][data-surface$=adset_placement_static_pgd_section]')")


#: When the single-screen card finishes loading, its body is the classic one
#: (d66a22d0fa30/01_ad_destination, 30.09): put the Doc19 card there and run
#: the accordion emulation on it.
_LOAD_LATER_JS = r"""
([html, sim, cfg, delay]) => { setTimeout(() => {
  const bar = document.querySelector('[data-surface$="adset_placement_static_pgd_section"] [role=progressbar]');
  bar.parentElement.innerHTML = html;
  (new Function('return (' + sim + ')'))()(cfg);
}, delay); }
"""


def test_simple_create_card_sets_manual_placements_once_loaded(page):
    """The single-screen card's body loads after the step began: it waits, then
    sets and reads back the manual placements inside that card."""
    _load_simple(page)
    page.evaluate(_LOAD_LATER_JS, [fragment(DOC19, "card"), _SIM_JS, _sim_cfg(), 600])
    report: dict = {}
    assert md.fill_placements(page, report) is True, report
    assert report["placements_card"]["via"] == "section"
    assert report["placements_card"]["loading"] is False
    state = _state(page)
    assert all(state[x] is True for x in md._SUBITEMS_ON if x in state)
    assert all(state[x] is False for x in md._SUBITEMS_OFF if x in state)
    assert report["subitems_not_offered"] == ["Messenger Stories"]


def _load_summary(page) -> None:
    page.set_content("<html><body style='margin:0;padding-top:120px'>"
                     f"{fragment(SUMMARY, 'card')}</body></html>")


#: The collapsed "Placements · Included: 7 · …" node expands on click into the
#: list of categories (the Doc19 card, under the accordion emulation).
_SUMMARY_EXPANDS_JS = r"""
([html, sim, cfg]) => {
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const btn = [...document.querySelectorAll('[role=button][aria-expanded]')]
    .find(b => /^Placements\s*Included:/.test(norm(b.textContent)));
  btn.addEventListener('click', () => {
    if (btn.getAttribute('aria-expanded') === 'true') return;
    btn.setAttribute('aria-expanded', 'true');
    const box = document.createElement('div');
    box.innerHTML = html;
    btn.parentElement.appendChild(box);
    (new Function('return (' + sim + ')'))()(cfg);
  });
}
"""


def test_a_summary_alone_is_not_proof(page):
    """B3 (review #449): 'Included: 7, Allowed with limited spend: 0,
    Excluded: 13' cannot say WHICH seven are included. A summary that does not
    open is a failure, kept in the report for diagnosis."""
    _load_summary(page)
    report: dict = {}
    assert md.fill_placements(page, report) is False
    assert report["placements_summary"]["included"] == 7
    assert report["placements_summary"]["excluded"] == 13
    assert report["why"] in ("placements_list_collapsed", "placement_categories_not_reached")


def test_a_collapsed_summary_is_opened_and_its_rows_read_back(page):
    """01.10, tester, single-screen editor: the list behind the summary is
    opened and every row set and read back — then the step passes."""
    _load_summary(page)
    page.evaluate(_SUMMARY_EXPANDS_JS, [fragment(DOC19, "card"), _SIM_JS, _sim_cfg()])
    report: dict = {}
    assert md.fill_placements(page, report) is True, report
    state = _state(page)
    assert all(state[x] is True for x in md._SUBITEMS_ON if x in state)
    assert all(state[x] is False for x in md._SUBITEMS_OFF if x in state)


def test_right_counts_hiding_a_wrong_set_fail(page):
    """B3 (review #449): the summary still says 'Included: 7', but behind it
    'Instagram feed' (wanted ON) is locked off. The rows are read back and the
    step fails."""
    rows = {k: list(v) for k, v in DOC19_ROWS.items()}
    rows["Feeds"] = [("Instagram feed", False, True) if r[0] == "Instagram feed" else r
                     for r in rows["Feeds"]]
    _load_summary(page)
    page.evaluate(_SUMMARY_EXPANDS_JS, [fragment(DOC19, "card"), _SIM_JS, _sim_cfg(rows)])
    report: dict = {}
    assert md.fill_placements(page, report) is False
    assert "Instagram feed" in report["subitems_missing"]


def test_debug_file_is_written_to_the_temp_dir(page):
    _load_simple(page)
    md.fill_placements(page, {})
    assert Path("output/debug_placements_fill.txt").exists()
