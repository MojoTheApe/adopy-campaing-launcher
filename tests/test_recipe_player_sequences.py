# -*- coding: utf-8 -*-
"""RecipePlayer.run() walked step by step on the recorded-page bench (ACL-341).

`recipe_player` is the engine that replays the golden recipe through `meta_dom`.
Until now nothing ran its `run()` loop: the unit tests touched single helpers.
Here the player runs whole (small) recipes, and the real golden recipe up to its
campaign gate, against fixtures from tests/bench/CATALOGUE.md in offline
headless Chromium: the real step functions, the real selectors, no live account.

The page is served at an Ads Manager edit URL (every request except the document
is aborted), because the player decides "am I in the wizard / did I leave Ads
Manager" from the URL as well as the markup. Pauses and screenshots are cut down
and the intent runner's evidence/ranks files are redirected to a temp dir;
no step function is mocked.

Fixtures used (CATALOGUE.md):
- simple_create_2026_09_27.html - single-screen editor: Campaign name, Budget,
  Ad set name, footer with Publish (steps campaign_name / budget / adset_name).
- audience_age_open_2026_09_24.html - Age row open (delegated audience_age).
- conversion_event_heading_group_2026_10_01.html - Conversion event card, Lead
  not selectable (delegated conversion_event failure).

Covered: step order and outcomes; skipped / optional steps; the stop_after gate;
a field that cannot be verified; a missing control named as FAIL; Publish never
pressed; a delegated section that runs once; hard stop on Conversion location;
a lost wizard stops the run before any step. Two strict xfail tests describe
gaps of the player itself (see their reasons); fixing them belongs to the RPA
track.
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
import yaml
from playwright.sync_api import Page

from src.fb_ads_rpa import intent_runner as ir
from src.fb_ads_rpa import meta_dom as md
from src.fb_ads_rpa import recipe_player as rp
from tests.bench_support import FIXTURES, browser  # noqa: F401

EDIT_URL = "https://adsmanager.facebook.com/adsmanager/manage/campaigns/edit/standalone?act=1"
GOLDEN = rp._RECIPE_PATH

EDITOR = (FIXTURES / "simple_create_2026_09_27.html").read_text(encoding="utf-8")
AGE_OPEN = (FIXTURES / "audience_age_open_2026_09_24.html").read_text(encoding="utf-8")
EVENT_CARD = (FIXTURES / "conversion_event_heading_group_2026_10_01.html").read_text(encoding="utf-8")

#: Records every click that lands on the page (capture phase), by visible text.
_CLICK_LOG = (
    "<script>window.landed=[];document.addEventListener('click',"
    "e=>window.landed.push((e.target.innerText||'').trim().slice(0,40)),true)</script>"
)
#: Meta's React field takes the typed value and then puts its old one back.
_REVERTING_BUDGET = (
    "<script>document.addEventListener('input',e=>{const t=e.target;"
    "if(t.type==='number'){setTimeout(()=>{t.value='10.0'},0)}},true)</script>"
)

NAME = "input[placeholder^='Enter your campaign name']"
BUDGET = "input[type=number]"
NO_SUCH = "No such control on this page"


def step(sid, *, tab="campaign", section="", action="click", risk="safe", **extra):
    return {"id": sid, "left_tab": tab, "section": section, "action": action, "risk": risk, **extra}


def fill(sid, param, *, tab="campaign"):
    return step(sid, tab=tab, action="fill", risk="risky", param=param)


def click_text(sid, own_text, **extra):
    return step(sid, match={"own_text": own_text}, **extra)


def draft():
    aset = SimpleNamespace(name="Bench Camp", daily_budget=7.0, targeting_label="", os="all",
                           age_min=18, age_max=65, ads=[SimpleNamespace(creative_path=None)])
    return SimpleNamespace(name="Bench Camp", country="SA", link="https://example.test", ad_sets=[aset])


@pytest.fixture
def play(browser, tmp_path, monkeypatch):  # noqa: F811
    """play(steps_or_None, html=EDITOR, stop_after=None, url=EDIT_URL) -> (result, page)."""
    monkeypatch.setattr(rp, "pause_short", lambda *a, **k: None)
    monkeypatch.setattr(rp, "pause_medium", lambda *a, **k: None)
    monkeypatch.setattr(rp, "capture_screenshot", lambda page, label, base_dir=None: f"shot:{label}")
    monkeypatch.setattr(md, "wait_editor_saved", lambda *a, **k: True)
    real_wait = Page.wait_for_timeout
    monkeypatch.setattr(Page, "wait_for_timeout", lambda self, ms: real_wait(self, min(ms, 60)))
    monkeypatch.setattr(md, "_sleep_h", lambda base, spread=0.3: time.sleep(min(base, 0.02)))
    # Keep the run's evidence packs and learned-strategy ranks out of the repository.
    monkeypatch.setattr(ir, "_EVIDENCE_ROOT", tmp_path / "evidence")
    monkeypatch.setattr(ir, "RANKS_PATH", tmp_path / "strategy_ranks.json")
    # The manage-list reopen is navigation inside Meta's SPA; there is no list to reopen here.
    monkeypatch.setattr(rp.RecipePlayer, "_reopen_wizard_from_list", lambda self: False)
    contexts = []

    def _play(steps, html=EDITOR, stop_after=None, url=EDIT_URL):
        monkeypatch.setattr(rp, "get_recipe_stop_after", lambda: stop_after)
        recipe_path = None
        if steps is not None:
            recipe_path = tmp_path / "recipe.yaml"
            recipe_path.write_text(yaml.safe_dump({"steps": steps}), encoding="utf-8")
        ctx = browser.new_context(viewport={"width": 1600, "height": 1000})
        contexts.append(ctx)
        body = f"<html><body>{html}{_CLICK_LOG}</body></html>"
        ctx.route("**/*", lambda r: r.fulfill(status=200, content_type="text/html", body=body)
                  if r.request.resource_type == "document" else r.abort())
        page = ctx.new_page()
        page.goto(url)
        result = rp.RecipePlayer(page, draft(), recipe_path=recipe_path).run()
        return result, page

    yield _play
    for c in contexts:
        c.close()


def outcomes(result):
    return [(o.step_id, o.ok) for o in result.steps]


def field(page, selector):
    return page.locator(selector).first.input_value()


# ── a good sequence ─────────────────────────────────────────────────────────

def test_name_and_budget_are_typed_in_recipe_order(play):
    result, page = play([fill("s05_campaign", "{{campaign_name}}"), fill("s06_campaign", "{{daily_budget}}")])
    assert result.ok and result.error is None
    assert outcomes(result) == [("s05_campaign", True), ("s06_campaign", True)]
    assert field(page, NAME) == "Bench Camp"
    assert field(page, BUDGET) == "7"
    assert result.screenshots[-1] == "shot:player_handoff"


def test_a_skipped_step_is_logged_and_nothing_is_pressed(play):
    result, page = play([
        click_text("s03_create", "Publish", skip=True),
        fill("s05_campaign", "{{campaign_name}}"),
    ])
    assert result.ok
    assert (result.steps[0].step_id, result.steps[0].strategy, result.steps[0].ok) == (
        "s03_create", "skipped_optional", True)
    assert "Publish" not in page.evaluate("window.landed")
    assert field(page, NAME) == "Bench Camp"


def test_the_layer_confirm_button_is_always_skipped(play):
    result, _ = play([step("s03_create", match={"tag": "BUTTON", "css": "button.layerConfirm"})])
    assert [(o.step_id, o.strategy) for o in result.steps] == [("s03_create", "skipped_optional")]


def test_an_optional_control_that_is_absent_does_not_fail_the_run(play):
    result, _ = play([click_text("s00b_manual", "Manual campaign setup", optional=True, on_fail="skip"),
                      fill("s05_campaign", "{{campaign_name}}")])
    assert result.ok
    assert outcomes(result) == [("s00b_manual", False), ("s00b_manual", True), ("s05_campaign", True)]
    assert result.steps[1].strategy == "skip_optional_miss"


# ── the gate ────────────────────────────────────────────────────────────────

def test_the_gate_stops_after_the_named_step_and_later_steps_do_not_run(play):
    result, page = play([
        fill("s05_campaign", "{{campaign_name}}"),
        fill("s06_campaign", "{{daily_budget}}"),
        fill("s09_adset", "{{adset_name}}", tab="adset"),
    ], stop_after="s06_campaign")
    assert result.ok
    assert outcomes(result) == [("s05_campaign", True), ("s06_campaign", True), ("gate_campaign_open", True)]
    assert result.screenshots[-1] == "shot:player_gate_campaign_open"
    assert field(page, NAME) == "Bench Camp"


def test_the_gate_does_not_pass_when_the_name_step_never_succeeded(play):
    # The campaign-name step is not in the recipe at all: the gate has to say so.
    result, _ = play([fill("s06_campaign", "{{daily_budget}}")], stop_after="s06_campaign")
    assert not result.ok
    assert result.steps[-1].step_id == "gate_campaign_open" and not result.steps[-1].ok
    assert "incomplete" in result.error


def test_the_real_golden_recipe_runs_to_the_campaign_gate_in_order(play):
    ids = [s["id"] for s in rp.load_recipe(GOLDEN)["steps"]]
    result, page = play(None, stop_after="s06_campaign")
    seen = [o.step_id for o in result.steps]
    # recipe steps answered in recipe order, up to and no further than the gate
    recorded = list(dict.fromkeys(i for i in seen if i in ids))  # s00b_manual logs FAIL then skip_optional_miss
    assert recorded == [i for i in ids[: ids.index("s06_campaign") + 1] if i in recorded]
    assert seen.index("s02_objective_continue") < seen.index("wizard_tab_switch") < seen.index("s05_campaign")
    done = dict(outcomes(result))
    assert done["s05_campaign"] and done["s06_campaign"]
    assert seen[-1] == "gate_campaign_open" and result.ok
    assert (field(page, NAME), field(page, BUDGET)) == ("Bench Camp", "7")
    assert "Publish" not in page.evaluate("window.landed")


# ── a broken flow fails ─────────────────────────────────────────────────────

def test_a_name_field_that_is_not_there_stops_the_run_with_a_named_error(play):
    gone = EDITOR.replace("Enter your campaign name here...", "x").replace("Campaign name", "Campaign title")
    result, _ = play([fill("s05_campaign", "{{campaign_name}}"), fill("s06_campaign", "{{daily_budget}}")],
                     html=gone, stop_after="s06_campaign")
    assert not result.ok
    assert "IVHP: campaign_name verify failed" in result.error
    assert outcomes(result) == [("s05_campaign", False)]  # budget step never ran, no gate reported


def test_a_budget_that_reverts_is_not_reported_as_typed(play):
    result, _ = play([fill("s05_campaign", "{{campaign_name}}"), fill("s06_campaign", "{{daily_budget}}")],
                     html=EDITOR + _REVERTING_BUDGET)
    assert not result.ok
    assert outcomes(result) == [("s05_campaign", True), ("s06_campaign", False)]
    assert "IVHP: daily_budget verify failed" in result.error


def test_a_failed_budget_never_lets_the_gate_pass(play):
    result, _ = play([fill("s05_campaign", "{{campaign_name}}"), fill("s06_campaign", "{{daily_budget}}")],
                     html=EDITOR + _REVERTING_BUDGET, stop_after="s06_campaign")
    assert not result.ok
    assert all(o.step_id != "gate_campaign_open" for o in result.steps)


def test_a_missing_control_is_logged_as_a_failed_step_and_nothing_is_clicked(play):
    result, page = play([click_text("sM", NO_SUCH, risk="risky")])
    assert outcomes(result) == [("sM", False)]
    assert result.steps[0].detail == "all strategies failed"
    assert page.evaluate("window.landed") == []


@pytest.mark.xfail(strict=True, reason=(
    "Player gap (RPA track): a non-optional risky click that finds nothing is logged FAIL, but run() "
    "keeps going and ends ok=True with error=None, and ads_manager_flow takes result.success from "
    "run.ok. A buyer's run reports success with its Leads/Continue click undone."))
def test_a_risky_click_that_found_nothing_does_not_end_in_success(play):
    result, _ = play([click_text("s01_objective", NO_SUCH, risk="risky")])
    assert not result.ok


@pytest.mark.xfail(strict=True, reason=(
    "Player gap (RPA track): with no semantic match the click falls back to the recorded x/y and is "
    "logged ok (strategy cdp_coords) although nothing is at that spot on this page."))
def test_a_click_by_stale_coordinates_is_not_reported_as_ok(play):
    s = click_text("s00_create", NO_SUCH)
    s["recorded"] = {"css_path": "", "x": 120, "y": 234, "w": 95, "h": 36}
    result, _ = play([s])
    assert not result.steps[0].ok


# ── Publish is never pressed ────────────────────────────────────────────────

@pytest.mark.parametrize("label", ["Publish", "Review and publish", "Discard drafts", "About this update"])
def test_forbidden_controls_are_refused_and_no_click_lands(play, label):
    html = EDITOR + f"<div role='button' tabindex='0'>{label}</div>"
    result, page = play([click_text("sP", label, risk="risky")], html=html)
    assert outcomes(result) == [("sP", False)]
    assert result.steps[0].strategy == "forbidden_zone"
    assert label not in page.evaluate("window.landed")


def test_the_editors_own_publish_footer_is_not_pressed_by_a_recorded_publish_step(play):
    result, page = play([click_text("sP", "Publish", risk="risky")])
    assert result.steps[0].strategy == "forbidden_zone"
    assert page.evaluate("window.landed") == []


# ── delegated sections ──────────────────────────────────────────────────────

def test_a_delegated_section_runs_its_handler_once_for_all_its_raw_steps(play):
    result, _ = play([step(f"s2{i}", tab="adset", section="audience_age", risk="risky") for i in range(3)],
                     html=EDITOR + AGE_OPEN)
    assert result.ok
    assert outcomes(result) == [("audience_age", True)]
    assert result.steps[0].strategy == "meta_dom.audience_age"


def test_conversion_location_failure_is_a_hard_stop_before_the_next_step(play):
    # The single-screen editor shows Conversion location = Instant forms; there is no menu to pick Website from.
    result, _ = play([step("s10", tab="adset", section="conversion_location", risk="risky"),
                      fill("s11", "{{adset_name}}", tab="adset")])
    assert not result.ok
    assert result.error.startswith("Conversion location") and "failed" in result.error
    assert outcomes(result) == [("conversion_location", False)]


def test_a_failed_conversion_event_is_named_but_the_run_goes_on_and_ends_not_ok(play):
    result, _ = play([step("s13", tab="adset", section="conversion_event", risk="risky"),
                      fill("s09_adset", "{{adset_name}}", tab="adset")],
                     html=EDITOR + EVENT_CARD)
    assert not result.ok
    assert result.error == "Delegated section failed: conversion_event"
    assert ("conversion_event", False) in outcomes(result)
    assert ("s09_adset", True) in outcomes(result)


def test_a_wizard_that_is_not_on_screen_stops_the_run_before_any_step(play):
    result, _ = play([step("s20", tab="adset", section="audience_age", risk="risky")], html=AGE_OPEN)
    assert not result.ok
    assert result.error == "Lost campaign wizard before adset steps"
    assert result.steps == []
    assert result.screenshots[-1] == "shot:player_handoff"
