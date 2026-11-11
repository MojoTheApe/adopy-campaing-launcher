"""ACL-2457 in the browser: bulk edit on the real Home campaign table and the real routes.

The table is `_campaign_table.html` in the account drop-down (ACL-1798/1800); `/campaign-commands/*` are the real
handlers over `BulkMeta` in memory (Meta and Multilogin are not touched). Skipped when free memory is under 6 GB.
Screenshots are written when `ACL_SHOTS_DIR` is set.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

pytest.importorskip("playwright.sync_api")
from tests.test_acl1798_home_campaigns_browser import open_acc, sync_playwright  # noqa: E402
from tests._retry_once import goto, launch_chromium  # noqa: E402
from tests.test_acl1798_campaign_inventory import adset, camp, snapshot  # noqa: E402
from tests.test_acl1800_home_commands_browser import CmdSite  # noqa: E402
from tests.test_acl2457_bulk_edit import (  # noqa: E402
    C1, C2, C3, C_ABO, S1, S2, S3, S4, S5, FakeResolver, World)

from src.analysis_agent import campaign_inventory as ci  # noqa: E402
from src.fb_api import bulk_edit as be  # noqa: E402
from src.fb_api import commands as cmd  # noqa: E402

SHOTS = os.environ.get("ACL_SHOTS_DIR", "")


def shot(page, name):
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(SHOTS) / f"{name}.png"))


def inventory():
    snap = snapshot()
    snap.campaigns, _ = ci.parse_inventory([
        camp(C1, "Camp one", status="PAUSED", daily_budget="5000"),
        camp(C2, "Camp two", status="PAUSED", lifetime_budget="90000"),
        camp(C3, "Camp three", status="PAUSED", daily_budget="2500"),
        camp(C_ABO, "Camp ABO", status="PAUSED"),
    ], "campaign")
    snap.adsets, _ = ci.parse_inventory([
        adset(S1, "Set 1a", C1, status="PAUSED"), adset(S2, "Set 1b", C1, status="PAUSED"),
        adset(S3, "Set 2a", C2, status="PAUSED"), adset(S4, "Set 3a", C3, status="PAUSED"),
        adset(S5, "Set abo", C_ABO, status="PAUSED", daily_budget="1000"),
    ], "adset")
    snap.ads, _ = ci.parse_inventory([], "ad")
    snap.insights = {"campaign": {}, "adset": {}, "ad": {}}
    return snap


class BulkSite(CmdSite):
    def handle(self, route):
        req = route.request
        path = urlparse(req.url).path
        if path == "/campaign-commands/bulk/preview":
            from src.web import campaign_commands as cc

            self.requests.append((req.method, path))
            data, code = cc.run_bulk_preview(json.loads(req.post_data or "{}"))
            return route.fulfill(status=code, content_type="application/json", body=json.dumps(data))
        return super().handle(route)


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("ACL_FB_BACKEND", "api")
    monkeypatch.setenv("ACL_BIND_HOST", "127.0.0.1")
    monkeypatch.setenv("ACL_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(cmd, "RECONCILE_PAUSE_S", 0.01)
    cmd._STORES.clear()
    w = World(tmp_path)
    w.store = cmd.get_store()
    from src.web import campaign_commands as cc

    monkeypatch.setattr(cc, "_sessions", lambda: w.open_session)
    monkeypatch.setattr(be, "TargetingResolver", FakeResolver, raising=False)
    import src.fb_api.creator as creator

    monkeypatch.setattr(creator, "TargetingResolver", FakeResolver)
    yield w
    cmd._STORES.clear()


@pytest.fixture
def open_page(world, monkeypatch, tmp_path):
    state = {}

    def opener(backend="api"):
        monkeypatch.setenv("ACL_FB_BACKEND", backend)
        from src.web import app as web

        monkeypatch.setattr(ci, "_store_path", lambda: tmp_path / "campaign_inventory.json")
        ci._RUNNING.clear()
        ci.put_result(inventory())
        site = BulkSite(web, monkeypatch, world)
        pw = sync_playwright().start()
        browser = launch_chromium(pw)
        pg = browser.new_page(viewport={"width": 1400, "height": 900})
        pg.route("http://acl.test/**", site.handle)
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.errors = errors
        goto(pg, "http://acl.test/")
        open_acc(pg, "Acc")
        pg.wait_for_selector(".hc-table tbody tr", timeout=10000)
        pg.wait_for_function("document.querySelector('.amap-campaigns')._hc.state.avail !== null")
        state.update(pw=pw, browser=browser)
        return pg, site

    yield opener
    if state:
        state["browser"].close()
        state["pw"].stop()


def tick(page, cid):
    page.check(f'input.hc-sel[data-sel="campaign:{cid}"]')


def fill_panel(page, **vals):
    page.wait_for_selector("dialog.acm-bulk.acm-edit[open]")
    for name, value in vals.items():
        loc = page.locator(f'dialog.acm-edit [data-acm-field="{name}"]')
        if loc.evaluate("e => e.tagName") == "SELECT":
            loc.select_option(value)
        else:
            loc.fill(str(value))
    page.click('dialog.acm-edit [data-acm="review"]')


def test_bulk_edit_end_to_end_preview_confirm_result(open_page, world):
    page, site = open_page()
    bar = page.locator("tr.hc-bulk")
    assert bar.count() == 1 and page.locator(".hc-bulk-go").is_disabled()
    tick(page, C1)
    tick(page, C2)
    assert "Bulk edit (2)" in page.inner_text(".hc-bulk-go") and not page.locator(".hc-bulk-go").is_disabled()
    page.click(".hc-bulk-go")
    shot(page, "20-bulk-panel")
    fill_panel(page, budget="60", gender="men", age_min="21")
    page.wait_for_selector("dialog.acm-confirm.acm-bulk[open]")
    text = page.inner_text("dialog.acm-confirm")
    assert "Nothing has been written yet" in text
    assert "50.00 USD daily → 60.00 USD daily" in text and "900.00 USD lifetime → 60.00 USD lifetime" in text
    assert "gender All, min age 18" in text.replace("\n", " ") or "gender Men" in text
    shot(page, "21-bulk-confirm")
    assert world.meta.posts == []                         # nothing is written before Confirm
    page.click('dialog.acm-confirm [data-acm="cancel"]')
    page.wait_for_timeout(300)
    assert world.meta.posts == [] and site.posted == []
    # again, this time confirmed
    page.click(".hc-bulk-go")
    fill_panel(page, budget="60", gender="men", age_min="21")
    page.wait_for_selector("dialog.acm-confirm.acm-bulk[open]")
    page.click('dialog.acm-confirm [data-acm="confirm"]')
    page.wait_for_selector("dialog.acm-result[open]", timeout=20000)
    res = page.inner_text("dialog.acm-result")
    assert res.count("Saved - confirmed by readback") == 5            # 2 budgets + 3 ad sets
    shot(page, "22-bulk-result")
    paths = sorted(p for p, _ in world.meta.posts)
    assert paths == sorted([f"/{C1}", f"/{C2}", f"/{S1}", f"/{S2}", f"/{S3}"])
    assert world.meta.objs[C1]["daily_budget"] == "6000" and world.meta.objs[C2]["lifetime_budget"] == "6000"
    assert world.meta.objs[S3]["targeting"]["genders"] == [1] and world.meta.objs[S3]["targeting"]["age_min"] == 21
    assert page.errors == []


def test_one_failed_campaign_is_reported_and_the_others_are_saved(open_page, world):
    page, site = open_page()
    world.meta.fail_next[C1] = "refuse"
    tick(page, C1)
    tick(page, C3)
    page.click(".hc-bulk-go")
    fill_panel(page, budget="10")
    page.wait_for_selector("dialog.acm-confirm.acm-bulk[open]")
    page.click('dialog.acm-confirm [data-acm="confirm"]')
    page.wait_for_selector("dialog.acm-result[open]", timeout=20000)
    res = page.inner_text("dialog.acm-result")
    assert "Meta refused" in res and "Saved - confirmed by readback" in res
    shot(page, "23-bulk-partial")
    assert world.meta.objs[C3]["daily_budget"] == "1000" and world.meta.objs[C1]["daily_budget"] == "5000"


def test_skipped_campaigns_are_listed_with_the_reason(open_page, world):
    page, site = open_page()
    tick(page, C_ABO)
    tick(page, C1)
    page.click(".hc-bulk-go")
    fill_panel(page, budget="75")
    page.wait_for_selector("dialog.acm-confirm.acm-bulk[open]")
    text = page.inner_text("dialog.acm-confirm")
    assert "Skipped - Budget: Budget is set on the ad sets, not on the campaign" in text
    assert "Camp ABO" in text


def test_panel_refuses_an_empty_or_bad_form_without_asking_the_server(open_page, world):
    page, site = open_page()
    tick(page, C1)
    page.click(".hc-bulk-go")
    page.wait_for_selector("dialog.acm-bulk.acm-edit[open]")
    page.click('dialog.acm-edit [data-acm="review"]')
    assert "at least one field" in page.inner_text("dialog.acm-edit")
    page.fill('dialog.acm-edit [data-acm-field="age_min"]', "70")
    page.click('dialog.acm-edit [data-acm="review"]')
    assert "13 to 65" in page.inner_text("dialog.acm-edit")
    assert ("POST", "/campaign-commands/bulk/preview") not in site.requests


def test_bulk_control_is_disabled_with_the_reason_on_the_rpa_backend(open_page, world):
    page, site = open_page(backend="rpa")
    assert page.locator(f'input.hc-sel[data-sel="campaign:{C1}"]').is_disabled()
    bar = page.inner_text("tr.hc-bulk")
    assert "API backend only" in bar
    assert page.locator(".hc-bulk-go").is_disabled()
    assert world.meta.posts == []
