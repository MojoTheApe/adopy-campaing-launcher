# -*- coding: utf-8 -*-
"""The campaigns list where the robot expected an editor or the objective dialog.

Bench (ACL-1433). `editor_closed_list_2026_10_01.html`:

- `table_header` — 68cc447bd867/03_ad_name (GT FX, 01.10 06:28): the step
  `ad_name` found the campaigns table instead of the ad editor; live report
  `fields 0, shown 0, sections 0, levels [], why not_on_ad_level`. The same
  page (no editor, only the table) is in 53e1f3ea8bd8/02_placements and
  e5beb2079e39/02_audience_location + 03_languages (CO_bnc, 01.10 06:28–06:29):
  the editor closed under the robot in three campaigns within two minutes.
- `notifications_panel` — c6a785dddd0e/01_objective_leads (01.10): after
  Create no objective dialog, only Meta's Notifications panel (a `role=dialog`).
  Same in ead714bafcc4, 20583efb8c41 (01.10), 21f211e9656e, 44ad1b9fc167,
  f2217ff15cab (29–30.09); their `open` outcomes were `radio`,
  `loading_ceiling`, `budget`, `table_loading`.
"""
from __future__ import annotations

import pytest

from src.fb_ads_rpa import meta_dom as md
from src.fb_ads_rpa import simple_create
from tests.bench_support import FIXTURES, browser, fragment, offline_page  # noqa: F401

FIXTURE = FIXTURES / "editor_closed_list_2026_10_01.html"


def test_ad_name_on_the_list_says_not_on_ad_level(offline_page):
    offline_page.set_content(fragment(FIXTURE, "table_header"))
    got = md.ad_name_failure(offline_page, {"tries": 5, "error": "timeout"})
    assert got["why"] == "not_on_ad_level"
    assert (got["fields"], got["shown"], got["sections"], got["levels"]) == (0, 0, 0, [])


def test_the_list_is_not_any_editor(offline_page):
    offline_page.set_content(fragment(FIXTURE, "table_header"))
    assert simple_create.page_markers(offline_page) == {
        "campaign": False, "adset": False, "ad": False, "publish": False}
    assert md.destination_state(offline_page) == {"section": False}


@pytest.mark.parametrize("outcome", ["radio", "loading_ceiling", "budget"])
def test_no_objective_dialog_is_named_with_the_open_outcome(offline_page, monkeypatch, outcome):
    """Meta's Notifications panel is a `role=dialog` — it is neither the table's
    loading modal nor network trouble, so the reason carries the open outcome."""
    offline_page.set_content(fragment(FIXTURE, "table_header") + fragment(FIXTURE, "notifications_panel"))
    monkeypatch.setattr(md._LAST_OPEN, "outcome", outcome, raising=False)
    assert md._objective_missing_reason(offline_page) == "no_objective_dialog:" + outcome


def test_table_loading_outcome_stays_table_loading(offline_page, monkeypatch):
    """f2217ff15cab (29.09): `open: table_loading` — the reason keeps it even
    after the table's modal is gone."""
    offline_page.set_content(fragment(FIXTURE, "table_header") + fragment(FIXTURE, "notifications_panel"))
    monkeypatch.setattr(md._LAST_OPEN, "outcome", "table_loading", raising=False)
    assert md._objective_missing_reason(offline_page) == "table_loading"
