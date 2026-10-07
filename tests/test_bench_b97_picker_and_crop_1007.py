# -*- coding: utf-8 -*-
"""ACL-1669: B97, 07.10 - the library pick does not stick, Crop has no Replace.

Two live runs of the same build on KLD_BFL_B97:

- 3edaf71b0613: every ad ended `selection_state_unavailable` 0.1 s after the
  Media step opened (`ad_creative_reuse`), so reuse refused and nothing was
  uploaded; `url_field_missing` followed on every ad.
- bc4302862ee7: the counter was readable (`before 0, after 1`, the pick took)
  but the Crop step was read while Meta still drew it (loading placeholders,
  no Square/Vertical cards) and answered `crop_replace_button_missing`.

B97's Meta may draw step bodies late and the robot read them once, at once. The
fix waits for the counter (4 s) and the Crop cards (20 s). When the counter
never shows, the robot refuses (`library_selection_unreadable`): it neither
clicks a tile nor uploads. A safe automatic fallback needs a live capture of
the missing-counter state and is follow-up work.

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


def _no_counter(monkeypatch):
    monkeypatch.setattr(md, "_library_selected_count", lambda page: None)
    monkeypatch.setattr(md, "_library_search", lambda *a: pytest.fail("must not search or click"))
    monkeypatch.setattr(md, "_library_tiles", lambda *a: pytest.fail("must not look at tiles"))


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


def test_counter_missing_after_the_wait_is_refused_without_a_click_or_an_upload(said, monkeypatch):
    _no_counter(monkeypatch)
    assert md._try_reuse_library_media(object(), "a.mp4") == md.REUSE_DIRTY
    assert ("ad_creative_reuse", False, "selection_state_unavailable") in said
    assert md._reuse_state["counter_missing"] is True
    # reuse_before_upload turns the refusal into "the step must fail", never "upload"
    monkeypatch.setattr(md, "get_reuse_library_media", lambda: True)
    assert md.reuse_before_upload(object(), "C:/x/a.mp4") is False


def test_the_removed_fallback_leaves_no_trace():
    for name in ("_library_grid_tile_count", "_library_marked_count", "_selection_after_fallback",
                 "_refuse_doubled_selection", "LIBRARY_SELECTION_NOT_SINGLE"):
        assert not hasattr(md, name), name


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


def test_a_library_that_was_not_searched_is_not_a_reason_to_upload_again():
    # ACL-1637 re-uploads only after a SEARCHED library answered "no copy".
    src = inspect.getsource(md._upload_ad_creative_with_retry)
    assert "blocked_by == LIBRARY_HAS_NO_COPY" in src
    assert "LIBRARY_NOT_SEARCHED" not in src


def test_new_reasons_are_named_blocking_creative_reasons(monkeypatch):
    for why in (md.LIBRARY_COUNTER_MISSING, md.LIBRARY_NOT_SEARCHED):
        monkeypatch.setattr(md, "last_upload_why", lambda why=why: why)
        line = md.creative_soft_fail("ad2", "Ad 2")
        assert line.startswith(f"ad2_creative_upload:{why} - Ad 2:")
        assert fp.severity(line) == fp.BLOCKING
        assert "by hand" in fp._detail(line)[0] or "by hand" in fp._detail(line)[3], why


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


class _Page:
    def __init__(self, seen=None, boom=False):
        self.seen, self.boom = seen, boom

    def evaluate(self, js):
        if self.boom:
            raise RuntimeError("page gone")
        return self.seen


@pytest.mark.parametrize("seen,expected", [(True, True), (False, False), (None, False)])
def test_classic_editor_empty_media_needs_the_empty_text_on_screen(monkeypatch, seen, expected):
    # The real function: no "Please specify the media" on screen (block unmounted,
    # unreadable, or filled) is NOT an empty block.
    monkeypatch.setattr(md, "_simple_page", lambda page: False)
    assert md._media_block_empty_seen(_Page(seen)) is expected


def test_a_page_that_raises_is_not_an_empty_block(monkeypatch):
    monkeypatch.setattr(md, "_simple_page", lambda page: False)
    assert md._media_block_empty_seen(_Page(boom=True)) is False


@pytest.mark.parametrize("shown,expected", [
    ({"mounted": True, "state": "empty"}, True),
    ({"mounted": True, "state": "filled"}, False),
    ({"mounted": False, "state": "empty"}, False),   # unmounted
    ({"mounted": True, "state": ""}, False),         # unreadable
    ({}, False),
])
def test_single_screen_editor_empty_media_needs_a_mounted_empty_state(monkeypatch, shown, expected):
    monkeypatch.setattr(md, "_simple_page", lambda page: True)
    monkeypatch.setattr(md._simple, "show_media_block", lambda page, **k: shown)
    assert md._media_block_empty_seen(object()) is expected


@pytest.mark.parametrize("creative_failed,empty,expected", [
    (True, True, True), (True, False, False), (False, True, False),
])
def test_no_media_needs_a_failed_creative_and_a_seen_empty_block(monkeypatch, creative_failed, empty, expected):
    monkeypatch.setattr(md, "_media_block_empty_seen", lambda page: empty)
    assert md._ad_has_no_media(object(), creative_failed=creative_failed) is expected


# The flow passes the observed media state (kills "no_media=False everywhere").

def _destination_fails(flow, monkeypatch, *, media_on_ad):
    monkeypatch.setattr(md, "set_ad_destination_website", lambda p, url: False)
    # the end-of-run sweep must not "confirm" (and so clear) the early failures
    monkeypatch.setattr(md, "verify_and_fix_ad_destinations", lambda p, names, url: list(names))
    md._last_destination.update(why="url_field_missing", diag={})
    monkeypatch.setattr(md, "_media_block_empty_seen", lambda page: not media_on_ad)
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
    assert md._library_any_tile_marked(offline_page) is True
    assert md._try_reuse_library_media(offline_page, "sample_media_01.mp4") == md.REUSE_DIRTY


def test_picker_still_loading_and_no_counter_is_refused(offline_page, bench_quiet):
    offline_page.set_content(WIZARD_ROW_CSS + fragment(PICKER, "loading"))
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
