# -*- coding: utf-8 -*-
"""ACL-1669: B97, 07.10 - the library pick does not stick, Crop has no Replace.

Two live runs of the same build on KLD_BFL_B97:

- 3edaf71b0613: every ad ended `selection_state_unavailable` 0.1 s after the
  Media step opened (`ad_creative_reuse`), so reuse refused and nothing was
  uploaded; `url_field_missing` followed on every ad.
- bc4302862ee7: the counter was readable (`before 0, after 1`, the pick took)
  but the Crop step was read while Meta still drew it (loading placeholders,
  no Square/Vertical cards) and answered `crop_replace_button_missing`.

B97's Meta draws step bodies late and the robot read them once, at once. The
fix waits for the counter / the cards. When the counter never shows, the robot
uploads the file instead ONLY if the library grid is drawn (tiles present) and
no tile carries Meta's selected mark; otherwise it refuses
(`library_selection_unreadable`). After such an upload it reads the selection
once and fails if more than one item is selected.

NOTE: the 07.10 picker capture bc4302862ee7/00_wizard_media shows the counter
WAS readable (0.1 s after the Media step opened) in that run, so "the counter is
late" is not established for 3edaf71b0613.

The browser tests (bench) run in CI only; the rest need no browser.
"""
from __future__ import annotations

import inspect

import pytest

from src.fb_ads_rpa import crop_step
from src.fb_ads_rpa import failure_policy as fp
from src.fb_ads_rpa import meta_dom as md
from tests.bench_support import FIXTURES, WIZARD_ROW_CSS, browser, fragment, offline_page  # noqa: F401
from tests.test_additional_ad_without_creative_has_no_text import flow  # noqa: F401

PICKER = FIXTURES / "wizard_media_picker_2026_10_04.html"
SELECTED = FIXTURES / "wizard_media_picker_selected_2026_10_07.html"
CROP = FIXTURES / "crop_cards_loading_2026_10_07.html"


# ── no browser ────────────────────────────────────────────────────────────

@pytest.fixture
def said(monkeypatch):
    out: list[tuple] = []
    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    monkeypatch.setattr(md._telemetry, "record",
                        lambda step, ok=None, **k: out.append((step, ok, k.get("note"))))
    md._reuse_state["counter_missing"] = False
    yield out
    md._reuse_state["counter_missing"] = False


def _no_counter(monkeypatch, *, tiles, marked):
    monkeypatch.setattr(md, "_library_selected_count", lambda page: None)
    monkeypatch.setattr(md, "_library_grid_tile_count", lambda page: tiles)
    monkeypatch.setattr(md, "_library_any_tile_marked", lambda page: marked)
    monkeypatch.setattr(md, "_library_search", lambda *a: pytest.fail("must not search or click"))


def test_counter_that_shows_up_late_is_waited_for(said, monkeypatch):
    reads = iter([None, None, 0])
    monkeypatch.setattr(md, "_library_selected_count", lambda page: next(reads))
    assert md._library_selected_count_waiting(object()) == 0


def test_the_reuse_step_waits_for_the_counter_instead_of_one_plain_read(said, monkeypatch):
    # Kills "_try_reuse_library_media reads the counter once": a counter that is
    # missing for two reads and then shows 0 must reach the library search.
    reads = iter([None, None, 0])
    monkeypatch.setattr(md, "_library_selected_count", lambda page: next(reads, 0))

    class Reached(Exception):
        pass

    def search(*a):
        raise Reached

    monkeypatch.setattr(md, "_library_search", search)
    with pytest.raises(Reached):
        md._try_reuse_library_media(object(), "a.mp4")


def test_counter_missing_grid_drawn_nothing_marked_steps_back_without_clicks(said, monkeypatch):
    _no_counter(monkeypatch, tiles=2, marked=False)
    assert md._try_reuse_library_media(object(), "a.mp4") == md.REUSE_ABSENT
    assert ("ad_creative_reuse", None, "selection_counter_missing") in said
    assert md._reuse_state["counter_missing"] is True
    # the caller then uploads: reuse_before_upload says "carry on"
    monkeypatch.setattr(md, "get_reuse_library_media", lambda: True)
    assert md.reuse_before_upload(object(), "C:/x/a.mp4") is None


@pytest.mark.parametrize("tiles,marked", [
    (2, True), (2, None),          # a tile is marked, or marks cannot be read
    (0, False), (None, False),     # grid not drawn yet / no grid: "not marked" proves nothing
    (None, None), (0, None), (None, True),
])
def test_counter_missing_without_a_drawn_clean_grid_is_refused(said, monkeypatch, tiles, marked):
    _no_counter(monkeypatch, tiles=tiles, marked=marked)
    assert md._try_reuse_library_media(object(), "a.mp4") == md.REUSE_DIRTY
    assert ("ad_creative_reuse", False, "selection_state_unavailable") in said


def test_later_ads_do_not_ask_the_library_again_when_it_has_no_counter(said, monkeypatch):
    md._reuse_state["counter_missing"] = True
    monkeypatch.setattr(md, "_reuse_upload_decision", lambda *a: pytest.fail("asked again"))
    assert md._reuse_known_copy(object(), "C:/x/a.mp4", True) is None


# ── upload_ad_creative driven up to the reuse verdict ────────────────────

@pytest.fixture
def upload(said, monkeypatch):
    from unittest.mock import MagicMock

    page = MagicMock()
    page.get_by_role.return_value.count.return_value = 1  # Upload is on the step
    monkeypatch.setattr(md, "_scroll_until_mounted", lambda *a, **k: None)
    monkeypatch.setattr(md, "_open_creative_wizard", lambda *a, **k: (True, 1))
    monkeypatch.setattr(md, "_wizard_media_step", lambda p: None)
    monkeypatch.setattr(md, "_close_creative_setup_modal", lambda p: None)
    md._set_upload_why(None)

    def go(verdict, *, counter_missing, reuse_only=False, known="absent"):
        def fake_reuse(p, path):
            md._reuse_state["counter_missing"] = counter_missing
            return verdict

        monkeypatch.setattr(md, "reuse_before_upload", fake_reuse)
        if known != "absent":
            monkeypatch.setattr(md, "_reuse_known_copy", known)
        return md.upload_ad_creative(page, "video", "C:/x/a.mp4", reuse_only=reuse_only)

    return go


def test_the_counter_flag_is_reset_for_every_ad(said, monkeypatch):
    # Kills "the per-call reset of counter_missing is dropped": an earlier ad's
    # flag must not leak into this ad's call.
    class Stop(Exception):
        pass

    def stop(*a, **k):
        raise Stop

    monkeypatch.setattr(md, "_scroll_until_mounted", stop)
    md._reuse_state["counter_missing"] = True
    with pytest.raises(Stop):
        md.upload_ad_creative(object(), "video", "C:/x/a.mp4")
    assert md._reuse_state["counter_missing"] is False


def test_a_refused_pick_is_named_by_its_cause(upload):
    # Kills "the reason is always library_pick_left_selection".
    assert upload(False, counter_missing=True) is False
    assert md.last_upload_why() == md.LIBRARY_COUNTER_MISSING == "library_selection_unreadable"
    assert upload(False, counter_missing=False) is False
    assert md.last_upload_why() == "library_pick_left_selection"


def test_a_library_that_was_not_searched_does_not_say_it_has_no_copy(upload):
    # The counter is unreadable on a reuse-only ad: the library was never asked.
    assert upload(None, counter_missing=True, reuse_only=True) is False
    assert md.last_upload_why() == md.LIBRARY_NOT_SEARCHED == "library_not_searched_counter_unreadable"


def test_a_searched_library_without_a_copy_still_says_so(upload):
    assert upload(None, counter_missing=False, reuse_only=True, known=lambda *a: None) is False
    assert md.last_upload_why() == md.LIBRARY_HAS_NO_COPY


def test_the_one_reupload_follows_a_library_that_was_not_searched():
    src = inspect.getsource(md._upload_ad_creative_with_retry)
    assert "LIBRARY_NOT_SEARCHED" in src and "LIBRARY_HAS_NO_COPY" in src


# ── after a fallback upload the selection is read once ───────────────────

@pytest.mark.parametrize("counter,marks,refused", [
    (2, 1, True), (1, 2, True), (None, 2, True), (3, None, True),
    (1, 1, False), (None, 1, False), (1, None, False), (None, None, False), (0, 0, False),
])
def test_a_doubled_selection_after_the_fallback_upload_fails(said, monkeypatch, counter, marks, refused):
    md._reuse_state["counter_missing"] = True
    md._set_upload_why(None)
    monkeypatch.setattr(md, "_library_selected_count", lambda page: counter)
    monkeypatch.setattr(md, "_library_marked_count", lambda page: marks)
    assert md._refuse_doubled_selection(object(), "C:/x/a.mp4") is refused
    if refused:
        assert md.last_upload_why() == md.LIBRARY_SELECTION_NOT_SINGLE
        assert ("ad_creative_upload", False, md.LIBRARY_SELECTION_NOT_SINGLE) in said


def test_the_selection_is_not_judged_when_the_counter_was_readable(said, monkeypatch):
    md._reuse_state["counter_missing"] = False
    monkeypatch.setattr(md, "_library_selected_count", lambda page: 5)
    monkeypatch.setattr(md, "_library_marked_count", lambda page: 5)
    assert md._refuse_doubled_selection(object(), "C:/x/a.mp4") is False


def test_the_finishing_step_asks_for_the_selection_check():
    assert "_refuse_doubled_selection(page, creative_path)" in inspect.getsource(md.upload_ad_creative)


def test_new_reasons_are_named_blocking_creative_reasons(monkeypatch):
    for why in (md.LIBRARY_COUNTER_MISSING, md.LIBRARY_NOT_SEARCHED, md.LIBRARY_SELECTION_NOT_SINGLE):
        monkeypatch.setattr(md, "last_upload_why", lambda why=why: why)
        line = md.creative_soft_fail("ad2", "Ad 2")
        assert line.startswith(f"ad2_creative_upload:{why} - Ad 2:")
        assert fp.severity(line) == fp.BLOCKING
        assert fp._detail(line) is not None, why


# ── url_field_missing_no_media ───────────────────────────────────────────

def test_url_field_after_missing_media_is_named_as_a_consequence():
    md._last_destination.update(why="url_field_missing", diag={})
    line = md.destination_soft_fail("ad2", "Ad 2", no_media=True)
    assert line.startswith("ad2_destination:url_field_missing_no_media - Ad 2:")
    assert "no media" in line and "consequence" in line
    # with media it is a destination problem as before
    assert md.destination_soft_fail("ad2", "Ad 2") == "ad2_destination"
    md._last_destination.update(why="url_field_not_focused", diag={})
    assert md.destination_soft_fail("ad2", "Ad 2", no_media=True) == "ad2_destination"


def test_the_relabel_stays_blocking_and_has_a_dashboard_text():
    md._last_destination.update(why="url_field_missing", diag={})
    line = md.destination_soft_fail("ad2", "Ad 2", no_media=True)
    assert fp.severity(line) == fp.BLOCKING
    assert "no media" in fp._detail(line)[0]


@pytest.mark.parametrize("filled,creative_failed,expected", [
    (False, True, True),    # the creative failed and the media block is empty
    (True, True, False),    # the creative step failed but the media IS on the ad
    (False, False, False),  # the creative step did not fail
])
def test_no_media_is_judged_on_the_media_block_not_on_the_creative_step(monkeypatch, filled, creative_failed, expected):
    monkeypatch.setattr(md, "_media_block_filled_now", lambda page: filled)
    assert md._ad_has_no_media(object(), creative_failed=creative_failed) is expected


def test_a_media_block_that_cannot_be_read_does_not_claim_no_media(monkeypatch):
    def boom(page):
        raise RuntimeError("page gone")

    monkeypatch.setattr(md, "_media_block_filled_now", boom)
    assert md._ad_has_no_media(object(), creative_failed=True) is False


# The flow passes the observed media state (kills "no_media=False everywhere").

def _destination_fails(flow, monkeypatch, *, media_on_ad):
    monkeypatch.setattr(md, "set_ad_destination_website", lambda p, url: False)
    # the end-of-run sweep must not "confirm" (and so clear) the early failures
    monkeypatch.setattr(md, "verify_and_fix_ad_destinations", lambda p, names, url: list(names))
    md._last_destination.update(why="url_field_missing", diag={})
    monkeypatch.setattr(md, "_media_block_filled_now", lambda page: media_on_ad)
    _result, soft = flow["run"]()
    return soft


def test_flow_names_a_missing_url_field_after_missing_media(flow, monkeypatch):
    flow["fail_upload"] = {"a.png", "b.png"}
    soft = _destination_fails(flow, monkeypatch, media_on_ad=False)
    assert any(s.startswith("ad_destination:url_field_missing_no_media") for s in soft), soft
    assert any(s.startswith("ad2_destination:url_field_missing_no_media") for s in soft), soft


def test_flow_keeps_the_plain_name_when_the_media_is_on_the_ad(flow, monkeypatch):
    flow["fail_upload"] = {"a.png", "b.png"}  # the creative STEP failed, e.g. the Crop gate
    soft = _destination_fails(flow, monkeypatch, media_on_ad=True)
    assert "ad_destination" in soft and "ad2_destination" in soft, soft
    assert not any("no_media" in s for s in soft), soft


# ── Crop ──────────────────────────────────────────────────────────────────

def test_crop_waits_for_the_cards_instead_of_naming_a_missing_button(monkeypatch):
    full = {"found": True, "shared": [], "cards": {
        n: {"original": True, "expand": [], "other": [], "radios": [], "replace": True,
            "media": "", "thumbs": []} for n in crop_step.CARDS}}
    empty = {"found": True, "shared": [], "cards": {n: None for n in crop_step.CARDS}}
    states = iter([empty, empty, empty, full])
    last = {"s": empty}

    def read(page, mark=None):
        last["s"] = next(states, last["s"])
        return last["s"]

    monkeypatch.setattr(crop_step, "read", read)
    monkeypatch.setattr(crop_step.time, "sleep", lambda s: None)
    assert crop_step.apply(object(), None) == ""


def test_crop_that_never_draws_its_cards_is_named_by_the_card(monkeypatch):
    empty = {"found": True, "shared": [], "cards": {n: None for n in crop_step.CARDS}}
    monkeypatch.setattr(crop_step, "read", lambda page, mark=None: empty)
    monkeypatch.setattr(crop_step, "CARDS_RENDER_WAIT_S", 0.05)
    monkeypatch.setattr(crop_step.time, "sleep", lambda s: None)
    assert crop_step.apply(object(), None) == "crop_square_card_missing"


# ── bench (offline Chromium, CI) ──────────────────────────────────────────

@pytest.fixture
def bench_quiet(monkeypatch):
    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    monkeypatch.setattr(md._telemetry, "record", lambda *a, **k: None)
    md._reuse_state["counter_missing"] = False
    yield
    md._reuse_state["counter_missing"] = False


def test_real_picker_with_the_selected_tile_and_no_counter_is_refused(offline_page, bench_quiet):
    # Cut from the 07.10 capture bc4302862ee7/00_wizard_media (wizard open on Media,
    # 0.1 s), counters removed. Meta wraps the selected tile in
    # data-test-decoration="selected": the robot must see the selection and refuse.
    offline_page.set_content(WIZARD_ROW_CSS + fragment(SELECTED, "selected_no_counter"))
    assert md._library_selected_count(offline_page) is None
    assert md._library_grid_tile_count(offline_page) == 1
    assert md._library_any_tile_marked(offline_page) is True
    assert md._try_reuse_library_media(offline_page, "sample_media_01.mp4") == md.REUSE_DIRTY


def test_drawn_grid_with_nothing_marked_and_no_counter_steps_back_for_an_upload(offline_page, bench_quiet):
    # The same capture with the selected mark taken off (hand-made variant).
    offline_page.set_content(WIZARD_ROW_CSS + fragment(SELECTED, "grid_unmarked_no_counter"))
    assert md._library_selected_count(offline_page) is None
    assert md._library_grid_tile_count(offline_page) == 1
    assert md._library_any_tile_marked(offline_page) is False
    assert md._try_reuse_library_media(offline_page, "sample_media_01.mp4") == md.REUSE_ABSENT


def test_picker_still_loading_and_no_counter_is_refused(offline_page, bench_quiet):
    offline_page.set_content(WIZARD_ROW_CSS + fragment(PICKER, "loading"))
    assert md._library_grid_tile_count(offline_page) is None
    assert md._try_reuse_library_media(offline_page, "sample_media_01.mp4") == md.REUSE_DIRTY


def test_picker_with_its_footer_counter_is_read(offline_page):
    offline_page.set_content(WIZARD_ROW_CSS + fragment(PICKER, "loaded"))
    assert md._library_selected_count(offline_page) == 1


def test_crop_opened_before_its_cards_are_drawn_reads_as_empty(offline_page):
    html = fragment(CROP, "cards_loading")
    assert html.count('role="progressbar"') == 3, "the real captures carry three Loading placeholders"
    offline_page.set_content(WIZARD_ROW_CSS + html)
    assert offline_page.locator("[role=progressbar]").count() == 3
    state = crop_step.read(offline_page)
    assert state["found"] is True
    assert not any(crop_step._card(state, n) for n in crop_step.CARDS)
