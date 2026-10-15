"""ACL-1800 в браузере: команды Home-таблицы на настоящем шаблоне и настоящих маршрутах.

Таблица — компонент `_campaign_table.html` в выпадашке аккаунта (ACL-1798/1800); `/campaign-commands/*` — настоящие
обработчики поверх `FakeMeta` в памяти (Meta и Multilogin не трогаются). Тест
пропускается, если свободной памяти меньше 6 ГБ. Снимки экрана пишутся, если задан
`ACL_SHOTS_DIR`.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

pytest.importorskip("playwright.sync_api")
from tests.test_acl1798_home_campaigns_browser import Site, open_acc, sync_playwright  # noqa: E402,F401
from tests._retry_once import goto, launch_chromium  # noqa: E402
from tests.test_acl1798_campaign_inventory import adset, camp, ad, snapshot  # noqa: E402
from tests.test_acl1800_campaign_commands import (  # noqa: E402
    AD_1, CAMP_A, CAMP_CBO, SET_AUTO, SET_BID, SET_CBO, FakeMeta, World)

from src.analysis_agent import account_spend_api as api  # noqa: E402
from src.analysis_agent import campaign_inventory as ci  # noqa: E402
from src.fb_api import commands as cmd  # noqa: E402

SHOTS = os.environ.get("ACL_SHOTS_DIR", "")


def shot(page, name):
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(SHOTS) / f"{name}.png"))


def inventory_snapshot(extra_campaigns=0):
    """Те же объекты, что в FakeMeta: id достаточно длинные для политики записи."""
    snap = snapshot()
    snap.campaigns, _ = ci.parse_inventory([
        camp(CAMP_A, "Camp A", status="PAUSED", bid_strategy="LOWEST_COST_WITHOUT_CAP"),
        camp(CAMP_CBO, "Camp CBO", daily_budget="5000", bid_strategy="LOWEST_COST_WITHOUT_CAP"),
    ] + [camp(1000100 + i, f"Extra {i}", status="PAUSED") for i in range(extra_campaigns)], "campaign")
    snap.adsets, _ = ci.parse_inventory([
        adset(SET_BID, "Set bid cap", CAMP_A, status="PAUSED", daily_budget="3000",
              bid_strategy="LOWEST_COST_WITH_BID_CAP", bid_amount="150"),
        adset(SET_AUTO, "Set auto", CAMP_A, daily_budget="2000", bid_strategy="LOWEST_COST_WITHOUT_CAP"),
        adset(SET_CBO, "Set under CBO", CAMP_CBO),
    ], "adset")
    snap.ads, _ = ci.parse_inventory([ad(AD_1, "Ad one", SET_BID, CAMP_A, status="PAUSED")], "ad")
    snap.insights = {"campaign": {}, "adset": {}, "ad": {}}
    return snap


class CmdSite(Site):
    """Страница-стенд ACL-1798 + статика помощника + настоящие маршруты команд."""

    def __init__(self, web, monkeypatch, world):
        super().__init__(web, monkeypatch)
        self.world = world
        self.posted = []

    def handle(self, route):
        req = route.request
        path = urlparse(req.url).path
        if path == "/static/acm_commands.js":
            return route.fulfill(status=200, content_type="application/javascript",
                                 body=(ROOT / "src" / "web" / "static" / "acm_commands.js").read_text(encoding="utf-8"))
        if path.startswith("/campaign-commands"):
            from src.web import campaign_commands as cc

            self.requests.append((req.method, path))
            if path.endswith("/availability"):
                data, code = cc.availability(), 200
            elif path.endswith("/preview"):
                data, code = cc.run_preview(json.loads(req.post_data or "{}"))
            elif path.endswith("/execute"):
                self.posted.append(json.loads(req.post_data or "{}"))
                data, code = cc.run_execute(json.loads(req.post_data or "{}"))
            else:
                data, code = cc.get_status(path.rsplit("/", 1)[1])
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
    yield w
    cmd._STORES.clear()


def make_site(world, monkeypatch, tmp_path, extra=0):
    from src.web import app as web

    monkeypatch.setattr(ci, "_store_path", lambda: tmp_path / "campaign_inventory.json")
    ci._RUNNING.clear()
    ci.put_result(inventory_snapshot(extra))
    return CmdSite(web, monkeypatch, world)


@pytest.fixture
def open_page(world, monkeypatch, tmp_path):
    state = {}

    def opener(extra=0, bind=None):
        if bind is not None:
            monkeypatch.setenv("ACL_BIND_HOST", bind)
        site = make_site(world, monkeypatch, tmp_path, extra)
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


def expand(page, key):
    page.click(f'[data-exp="{key}"]')


def sw(page, oid):
    return page.locator(f'.hc-switch[data-object-id="{oid}"]')


def test_switch_flow_end_to_end_row_comes_from_the_readback(open_page, world):
    page, site = open_page()
    s = sw(page, CAMP_A)
    assert s.get_attribute("aria-checked") == "false" and not s.is_disabled()
    s.click()
    page.wait_for_selector("dialog.acm-confirm[open]")
    text = page.inner_text("dialog.acm-confirm")
    assert "Switch on 1 object" in text and "Off → On" in text and "USD" in text
    shot(page, "01-confirm-switch-on")
    assert sw(page, CAMP_A).get_attribute("aria-checked") == "false"      # пока не подтвердили — не меняется
    assert world.meta.posts == []
    page.click('dialog.acm-confirm [data-acm="confirm"]')
    page.wait_for_selector(f'.hc-switch[data-object-id="{CAMP_A}"][aria-checked="true"]', timeout=15000)
    assert "Confirmed" in page.inner_text(f'tr[data-msg="row:campaign:{CAMP_A}"]')           # итог — строкой под строкой, не в ячейке Status
    assert "Confirmed" not in page.inner_text(f'tr[data-key="campaign:{CAMP_A}"] td.p2')
    assert world.meta.posts == [(f"/{CAMP_A}", {"status": "ACTIVE"})]
    assert world.meta.objs[CAMP_A]["status"] == "ACTIVE"
    shot(page, "02-switch-confirmed")
    assert page.errors == []


def test_cancel_sends_nothing_and_keeps_the_switch(open_page, world):
    page, site = open_page()
    sw(page, CAMP_A).click()
    page.wait_for_selector("dialog.acm-confirm[open]")
    page.click('dialog.acm-confirm [data-acm="cancel"]')
    page.wait_for_timeout(300)
    assert sw(page, CAMP_A).get_attribute("aria-checked") == "false"
    assert world.meta.posts == [] and site.posted == []


def test_switch_on_shows_the_parent_constraint_and_never_enables_the_parent(open_page, world):
    page, site = open_page()
    expand(page, f"campaign:{CAMP_A}")
    page.wait_for_selector(f'tr[data-key="adset:{SET_BID}"]')
    sw(page, SET_BID).click()
    page.wait_for_selector("dialog.acm-confirm[open]")
    assert "On · Campaign paused" in page.inner_text("dialog.acm-confirm")
    shot(page, "03-parent-constraint")
    page.click('dialog.acm-confirm [data-acm="confirm"]')
    page.wait_for_selector(f'.hc-switch[data-object-id="{SET_BID}"][aria-checked="true"]', timeout=15000)
    assert world.meta.objs[CAMP_A]["status"] == "PAUSED"
    assert [p for p, _ in world.meta.posts] == [f"/{SET_BID}"]


def test_failed_execute_keeps_the_previous_switch_state(open_page, world):
    page, site = open_page()
    world.meta.fail_next[CAMP_A] = "refuse"
    sw(page, CAMP_A).click()
    page.wait_for_selector("dialog.acm-confirm[open]")
    page.click('dialog.acm-confirm [data-acm="confirm"]')
    page.wait_for_selector("text=Failed — previous state kept", timeout=15000)
    assert sw(page, CAMP_A).get_attribute("aria-checked") == "false"
    assert world.meta.objs[CAMP_A]["status"] == "PAUSED"
    shot(page, "04-failed-previous-state-kept")


def test_unknown_outcome_says_to_press_read_campaigns_and_does_not_flip_the_switch(open_page, world):
    page, site = open_page()
    world.meta.fail_next[CAMP_A] = "lost"
    sw(page, CAMP_A).click()
    page.wait_for_selector("dialog.acm-confirm[open]")
    page.click('dialog.acm-confirm [data-acm="confirm"]')
    page.wait_for_selector("text=Outcome unknown — press Read campaigns to check", timeout=15000)
    assert sw(page, CAMP_A).get_attribute("aria-checked") == "false"
    assert len(world.meta.posts) == 1                                   # без повторов
    shot(page, "05-outcome-unknown")


def test_unknown_outcome_that_was_applied_is_confirmed_by_readback(open_page, world):
    page, site = open_page()
    world.meta.fail_next[CAMP_A] = "applied"
    sw(page, CAMP_A).click()
    page.wait_for_selector("dialog.acm-confirm[open]")
    page.click('dialog.acm-confirm [data-acm="confirm"]')
    page.wait_for_selector(f'.hc-switch[data-object-id="{CAMP_A}"][aria-checked="true"]', timeout=15000)
    assert len(world.meta.posts) == 1


def test_budget_editor_appears_only_on_budget_owners(open_page, world):
    page, site = open_page()
    expand(page, f"campaign:{CAMP_A}")
    expand(page, f"campaign:{CAMP_CBO}")
    page.wait_for_selector(f'tr[data-key="adset:{SET_CBO}"]')
    expand(page, f"adset:{SET_BID}")
    page.wait_for_selector(f'tr[data-key="ad:{AD_1}"]')

    def btn(level, oid):
        return page.locator(f'button[data-action="budget"][data-object-id="{oid}"][data-level="{level}"]')

    assert not btn("campaign", CAMP_CBO).is_disabled()                 # владелец бюджета
    assert not btn("adset", SET_BID).is_disabled()
    under = btn("adset", SET_CBO)
    assert under.is_disabled() and under.get_attribute("title") == "Budget is set on the campaign"
    ad_cell = page.locator(f'tr[data-key="ad:{AD_1}"]')
    assert ad_cell.locator('button[data-action="budget"]').count() == 0 and "Ads have no budget" in ad_cell.inner_html()
    # у кампании без бюджета — показать бюджеты её ad set, а не редактор
    camp_btn = btn("campaign", CAMP_A)
    assert camp_btn.get_attribute("title") == "View ad set budgets"
    page.click(f'[data-exp="campaign:{CAMP_A}"]')                      # свернуть
    camp_btn.click()
    page.wait_for_selector(f'tr[data-key="adset:{SET_BID}"]')
    assert page.locator("dialog.acm-edit").count() == 0
    # редактор владельца: ввод -> превью -> подтверждение -> строка из сверки
    btn("campaign", CAMP_CBO).click()
    page.wait_for_selector("dialog.acm-edit[open]")
    shot(page, "06-budget-editor")
    page.fill('dialog.acm-edit [data-acm="value"]', "0")
    page.click('dialog.acm-edit [data-acm="review"]')
    assert "positive amount" in page.inner_text("dialog.acm-edit")        # плохая сумма не уходит дальше
    page.fill('dialog.acm-edit [data-acm="value"]', "60")
    page.click('dialog.acm-edit [data-acm="review"]')
    page.wait_for_selector("dialog.acm-confirm[open]")
    assert "50.00 USD → 60.00 USD" in page.inner_text("dialog.acm-confirm")
    page.click('dialog.acm-confirm [data-acm="confirm"]')
    page.wait_for_selector(f'tr[data-key="campaign:{CAMP_CBO}"]:has-text("$60.00 / day")', timeout=15000)
    assert world.meta.posts == [(f"/{CAMP_CBO}", {"daily_budget": "6000"})]
    shot(page, "07-budget-confirmed")


def test_bid_editor_is_refused_on_automatic_bidding(open_page, world):
    page, site = open_page()
    expand(page, f"campaign:{CAMP_A}")
    page.wait_for_selector(f'tr[data-key="adset:{SET_AUTO}"]')
    auto = page.locator(f'button[data-action="bid-up"][data-object-id="{SET_AUTO}"]')
    assert auto.is_disabled() and auto.get_attribute("title") == "Automatic · no manual bid"
    assert page.locator(f'button[data-action="bid-down"][data-object-id="{SET_AUTO}"]').is_disabled()
    camp_bid = page.locator(f'button[data-action="bid-up"][data-object-id="{CAMP_A}"]')
    assert camp_bid.is_disabled()
    ok = page.locator(f'button[data-action="bid-up"][data-object-id="{SET_BID}"]')
    assert not ok.is_disabled()
    ok.click()
    page.wait_for_selector("dialog.acm-edit[open]")
    assert page.input_value('dialog.acm-edit [data-acm="value"]') == "1.58"      # 1.50 + 5%
    shot(page, "08-bid-editor")
    page.click('dialog.acm-edit [data-acm="review"]')
    page.wait_for_selector("dialog.acm-confirm[open]")
    assert "Bid cap: 1.50 USD → 1.58 USD" in page.inner_text("dialog.acm-confirm")
    page.click('dialog.acm-confirm [data-acm="confirm"]')
    page.wait_for_selector("text=Confirmed", timeout=15000)
    assert world.meta.posts == [(f"/{SET_BID}", {"bid_amount": "158"})]


def test_batch_lists_every_object_with_skipped_and_is_capped(open_page, world):
    page, site = open_page(extra=8)
    page.check(f'input[data-sel="campaign:{CAMP_A}"]')
    page.check(f'input[data-sel="campaign:{CAMP_CBO}"]')
    assert not page.locator('[data-batch="on"]').is_disabled()
    page.click('[data-batch="on"]')
    page.wait_for_selector("dialog.acm-confirm[open]")
    text = page.inner_text("dialog.acm-confirm")
    assert "Switch on 1 object" in text and "Skipped" in text and "Already On" in text   # CBO уже включена
    shot(page, "09-batch-preview-with-skipped")
    page.click('dialog.acm-confirm [data-acm="cancel"]')
    # потолок вызовов: 8 строк > 7
    for i in range(8):
        page.check(f'input[data-sel="campaign:{1000100 + i}"]')
    b = page.locator('[data-batch="on"]')
    assert b.is_disabled() and "At most 7 objects per command" in b.get_attribute("title")
    shot(page, "10-batch-capped")
    assert world.meta.posts == []


def test_commands_are_disabled_with_a_reason_when_not_on_loopback(open_page, world):
    page, site = open_page(bind="0.0.0.0")
    s = sw(page, CAMP_A)
    assert s.is_disabled() and "127.0.0.1" in s.get_attribute("title") and "Commands unavailable" in s.get_attribute("title")
    assert page.locator("[data-batch]").first.is_disabled()
    shot(page, "11-disabled-not-loopback")
    s.click(force=True)
    page.wait_for_timeout(200)
    assert [r for r in site.requests if r[1].endswith("/preview")] == []


def test_duplicate_and_copy_stay_disabled(open_page, world):
    page, site = open_page()
    page.select_option("[data-hc=preset]", "all")
    info = page.evaluate("""() => [...document.querySelectorAll('.hc-cmd[data-action=duplicate], .hc-cmd[data-action=copy], .hc-cmd[data-action=more]')]
        .map(b => ({a: b.dataset.action, d: b.disabled, t: b.title}))""")
    assert info and all(i["d"] for i in info)
    assert {i["t"] for i in info if i["a"] in ("duplicate", "copy")} == {"ACL-1773"}


@pytest.mark.skipif(not SHOTS, reason="снимки экрана пишутся только при заданном ACL_SHOTS_DIR")
def test_country_table_screenshots_light_and_dark(open_page, monkeypatch, world):
    """Снимки для сравнения с макетом (Campaigns в блоке страны): светлая и тёмная темы, цель и BI подставлены."""
    from src.web import app as web

    page, site = open_page()
    bi = {"campaigns": {"Set bid cap": {"spend": 790.0, "leads": 64, "deposits": 1},
                        "Set auto": {"spend": 1200.0, "leads": 140, "deposits": 5},
                        "Set under CBO": {"spend": 1990.0, "leads": 204, "deposits": 6}}, "ads": {}}
    monkeypatch.setattr(web, "_inventory_bi", lambda a, c, f, t, s: (bi, ""))
    monkeypatch.setattr(web, "_inventory_goal_context", lambda c: {
        "country": "Kuwait", "goal_cpa": 500.0, "goal_ftd": 12, "benchmark_cpa": 650.0, "goal_note": "", "benchmark_note": ""})
    page.set_viewport_size({"width": 1500, "height": 1000})
    page.reload()
    open_acc(page, "Acc")
    page.wait_for_selector(".hc-table tbody tr")
    page.click("[data-hc=expand-all]")
    page.wait_for_selector(f'tr[data-key="ad:{AD_1}"]', timeout=15000)
    block = page.locator(".amap")
    for theme in ("light", "dark"):
        page.evaluate("t => document.documentElement.setAttribute('data-theme', t)", theme)
        page.evaluate("document.querySelector('.hc-scroll').scrollLeft = 0")
        page.wait_for_timeout(150)
        block.screenshot(path=str(Path(SHOTS) / f"account-tree-controls-{theme}.png"))
        page.click("[data-jump=bi]")
        page.wait_for_timeout(150)
        block.screenshot(path=str(Path(SHOTS) / f"account-tree-bi-{theme}.png"))
        page.click("[data-jump=meta]")
        page.wait_for_timeout(150)
        block.screenshot(path=str(Path(SHOTS) / f"account-tree-meta-{theme}.png"))
    page.evaluate("t => document.documentElement.setAttribute('data-theme', t)", "light")
    page.evaluate("document.querySelector('.hc-scroll').scrollLeft = 0")
    box = page.locator(".hc-resize").bounding_box()
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x + 180, y, steps=6)
    page.mouse.up()
    page.wait_for_timeout(150)
    block.screenshot(path=str(Path(SHOTS) / "account-tree-pinned-widened-light.png"))
    page.evaluate("localStorage.removeItem('acl.hc.pinw.v1')")
    assert page.errors == []
