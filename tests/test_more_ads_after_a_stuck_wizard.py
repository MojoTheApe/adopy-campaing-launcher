# -*- coding: utf-8 -*-
"""A stuck media wizard on one ad never blocks creating the next ads (ACL-1551).

Live 04.10 (232d398471bd, Facebook_1443): the media wizard of ad 1 stayed open
on Crop (Next and Cancel locked), and the ad set's Action menu did not open
under it - `simple_create_more_ads action_menu_not_opened`, `ad2_create
action_menu_not_opened`, `ad3_create not_tried`.

1. `simple_create.create_next_ad` closes a leftover wizard with the wizard's
   own Close before the Action menu (never Next, Save or Publish). Page: the
   live single-screen tree of 30.09 (`test_simple_create_more_ads.env`) with
   the scrubbed 04.10 wizard on top (`wizard_crop_story_missing_2026_10_04`,
   fragment `crop_busy`); while it is on screen it takes every click, as the
   modal did live.
2. The recorded flow: ad 1's creative fails with `crop_wizard_stuck` - the ad
   is blocking with a readable reason, ads 2 and 3 are still created and
   filled (the flow test page of `test_the_single_screen_editor_flow`).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import crop_step  # noqa: E402
from src.fb_ads_rpa import failure_policy  # noqa: E402
from src.fb_ads_rpa import meta_dom as md  # noqa: E402
from src.fb_ads_rpa import simple_create as sc  # noqa: E402
from tests.bench_support import FIXTURES, fragments  # noqa: E402
from tests.test_create_quick_retry_returns_to_list import flow  # noqa: E402,F401
from tests.test_simple_create_more_ads import ADSET_MENU, CREATE_SURFACE, _clicks, browser, env  # noqa: E402,F401
from tests.test_the_single_screen_editor_flow import _THREE, _creates, sim  # noqa: E402,F401

WIZARD = fragments(FIXTURES / "wizard_crop_story_missing_2026_10_04.html")["crop_busy"]

#: The 04.10 wizard over the editor: a modal - while it is there, a click
#: anywhere else does nothing; its Close removes it ('noop': does nothing).
_WIZARD_OVER_EDITOR = r"""
([html, close]) => {
  const wrap = document.createElement('div');
  wrap.id = 'wiz';
  wrap.style.cssText = 'position:fixed;left:340px;top:0;width:900px;height:760px;background:#fff;z-index:20;overflow:auto';
  wrap.innerHTML = html;
  document.body.appendChild(wrap);
  window.wizPressed = [];
  window.addEventListener('click', (e) => {
    const w = document.getElementById('wiz');
    if (!w) return;
    if (!w.contains(e.target)) { e.stopImmediatePropagation(); e.preventDefault(); return; }
    const b = e.target.closest('[role=button],button');
    const label = b ? (b.textContent || '').replace(/[​-‍﻿]/g, '').replace(/\s+/g, ' ').trim() : '';
    window.wizPressed.push(label);
    if (label === 'Close' && close !== 'noop') w.remove();
    e.stopImmediatePropagation();
  }, true);
}
"""


def _wizard(env, close: str = "ok") -> None:
    env["page"].evaluate(_WIZARD_OVER_EDITOR, [WIZARD, close])


# ── 1. create_next_ad closes the leftover wizard first ───────────────────

def test_the_leftover_wizard_is_closed_by_its_close_then_the_ad_is_created(env):
    page = env["page"]
    _wizard(env)
    got = sc.create_next_ad(page, tag="ad2", known_names=["Sample Ad 01"])
    assert got["ok"], got
    assert page.evaluate("window.wizPressed") == ["Close"]
    assert page.evaluate("document.getElementById('wiz')") is None
    cleared = got["wizard_cleared"]
    assert cleared["closed"] is True and cleared["closed_by"] == "Close"
    assert cleared["dialog_left"] is False and cleared["tried"] == ["Close"]
    assert got["pressed"] == ["adset_action_menu", "menu_create_ad"]
    clicks = _clicks(page)
    assert ADSET_MENU in clicks[-2] and clicks[-1] == CREATE_SURFACE + "|Create ad"
    assert not [c for c in clicks if c.endswith(("|Next", "|Save", "|Publish", "|Done"))], clicks
    row = [r for r in env["rows"] if r[0] == "simple_create_more_ads"][0]
    assert row[1] is True and row[3]["wizard_cleared"]["closed_by"] == "Close"


def test_without_the_wizard_nothing_is_pressed_before_the_action_menu(env):
    got = sc.create_next_ad(env["page"], tag="ad2", known_names=["Sample Ad 01"])
    assert got["ok"] and "wizard_cleared" not in got
    assert len(_clicks(env["page"])) == 2


def test_a_wizard_its_close_cannot_close_is_named_and_nothing_else_is_pressed(env):
    page = env["page"]
    _wizard(env, close="noop")
    got = sc.create_next_ad(page, tag="ad2", known_names=["Sample Ad 01"])
    assert not got["ok"] and got["why"] == "creative_wizard_left_open", got
    assert got["text"].startswith("Meta's media wizard of the previous ad stayed open")
    assert got["wizard_cleared"]["dialog_left"] is True
    assert page.evaluate("window.wizPressed") == ["Close"]
    assert got["pressed"] == [], "the Action menu was pressed under the wizard"
    failure = f"ad2_create:{got['why']} - {got['text']}"
    problem = failure_policy.problem(failure)
    assert problem["severity"] == failure_policy.BLOCKING
    assert problem["title"] == "Ad not created - media wizard left open"


# ── 2. the flow goes on to ads 2 and 3 ───────────────────────────────────

def test_a_stuck_wizard_on_ad_1_blocks_ad_1_only_and_ads_2_3_are_built(sim, monkeypatch):
    seen = _creates(sim)
    uploads: list = []

    def upload(p, ctype, path, **kw):
        uploads.append(path)
        if path == "a.png":
            md._set_upload_why(crop_step.WIZARD_STUCK)
            return False
        md._set_upload_why(None)
        return True

    monkeypatch.setattr(md, "upload_ad_creative_with_retry", upload)
    monkeypatch.setattr(md, "write_ad_copy", lambda p, title, body, **kw: True)
    monkeypatch.setattr(md, "set_ad_destination_website", lambda p, url: True)
    monkeypatch.setattr(md, "_browser_addon_none_checked", lambda p: True)
    monkeypatch.setattr(md, "_review_errors_visible", lambda p: False)
    out: list[str] = []
    sim["run"](additional_ads=_THREE, soft_fails_out=out)
    assert [s[0] for s in seen] == ["ad2", "ad3"], "the next ads were not created"
    assert uploads == ["a.png", "b.png", "c.png"]
    stuck = [f for f in out if failure_policy.base_name(f) == "ad_creative_upload"]
    assert stuck == ["ad_creative_upload:crop_wizard_stuck - ad 1: "
                     + sc.creative_why_text(crop_step.WIZARD_STUCK)], out
    assert failure_policy.severity(stuck[0]) == failure_policy.BLOCKING
    assert not [f for f in out if failure_policy.base_name(f) in (
        "ad2_create", "ad3_create", "ad2_creative_upload", "ad3_creative_upload",
        "simple_create_more_ads")], out
    blocking, _ = failure_policy.split(out)
    assert stuck[0] in blocking
    json.dumps(failure_policy.problems(out))      # the dashboard can serialise it
