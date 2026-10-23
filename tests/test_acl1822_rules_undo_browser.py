"""Full-dashboard Apply and Undo need separate human confirmations, using FakeMeta."""
from __future__ import annotations

import asyncio
import concurrent.futures
import copy
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import sync_playwright
from starlette.datastructures import QueryParams

from src.analysis_agent import account_spend_api, campaign_inventory as ci
from src.analysis_agent.rules.store import RulesStore
from src.fb_api import commands as cmd
from src.web import app as web, campaign_commands, rules_api
from tests._retry_once import goto, launch_chromium
from tests.test_acl1798_campaign_inventory import D, adset, camp, snapshot
from tests.test_acl1800_campaign_commands import CAMP_A, CAMP_CBO, SET_AUTO, World
from tests.test_web_drill import _MOCK_REPORT

ROOT = Path(__file__).resolve().parents[1]
OWNER = "Synthetic buyer"


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        instance = launch_chromium(pw)
        yield instance
        instance.close()


@pytest.fixture
def dashboard(browser, monkeypatch, tmp_path):
    monkeypatch.setenv("ACL_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("ACL_FB_BACKEND", "api")
    monkeypatch.setenv("ACL_BIND_HOST", "127.0.0.1")
    monkeypatch.setattr(ci, "_store_path", lambda: tmp_path / "inventory.json")
    monkeypatch.setattr(web, "_inventory_bi", lambda *a: ({"campaigns": {}, "ads": {}}, ""))
    monkeypatch.setattr(web, "_inventory_goal_context", lambda *a: {
        "goal_cpa": None, "benchmark_cpa": None, "goal_note": "No synthetic goal", "benchmark_note": "No synthetic benchmark"})
    ci._RUNNING.clear()
    cmd._STORES.clear()
    monkeypatch.setattr(cmd, "RECONCILE_PAUSE_S", 0.01)
    world = World(tmp_path)
    world.store = cmd.get_store()
    monkeypatch.setattr(campaign_commands, "_sessions", lambda: world.open_session)
    store = RulesStore(tmp_path)
    rule = store.create_from_preset("stop_loss_new_tests", OWNER, by=OWNER)
    store.edit_rule(rule["id"], {"scope": {"country": "Kuwait", "accounts": ["Acc"], "objects": [
        {"account": "Acc", "level": "adset", "id": SET_AUTO, "name_at_assign": "Synthetic set"}]}}, by=OWNER)
    store.set_mode(rule["id"], "suggest", by=OWNER)
    store.append_action({"rule_id": rule["id"], "version": store.get_rule(rule["id"])["current_version"],
                         "state": "proposed", "account": "Acc", "level": "adset", "id": SET_AUTO,
                         "kind": "pause", "before": {"status": "ACTIVE"}, "requested": {"status": "PAUSED"}})
    version = store.get_rule(rule["id"])["current_version"]
    store.append_evaluation({"rule_id": rule["id"], "version": version, "mode": "suggest", "strategy": 1,
                             "verdict": "matched", "reasons": [], "T": {"value": 60}, "objects": [
                                 {"key": "adset:" + SET_AUTO, "account": "Acc", "level": "adset", "id": SET_AUTO,
                                  "name": "Synthetic set", "identity": "exact", "verdict": "matched",
                                  "conditions_matched": True, "reasons": [], "notes": [], "observed": {"spend": 620},
                                  "action": {"kind": "pause"}}]})
    saved = snapshot(account="Acc")
    saved.campaigns, _ = ci.parse_inventory([camp(CAMP_A, "Synthetic campaign", status="PAUSED")], "campaign")
    saved.adsets, _ = ci.parse_inventory([adset(SET_AUTO, "Synthetic set", CAMP_A, daily_budget="2000")], "adset")
    saved.ads = []
    ci.put_result(saved)
    opened = []

    def open_page(theme, width, surface, kind="pause"):
        selected = rule["id"]
        target = "adset:" + SET_AUTO
        if kind == "budget_up":
            scale = store.create_from_preset("scale_proven", OWNER, by=OWNER)
            store.edit_rule(scale["id"], {"scope": {"country": "Kuwait", "accounts": ["Acc"], "objects": [
                {"account": "Acc", "level": "campaign", "id": CAMP_CBO, "name_at_assign": "Synthetic CBO"}]}}, by=OWNER)
            store.set_mode(scale["id"], "suggest", by=OWNER)
            version = store.get_rule(scale["id"])["current_version"]
            store.append_action({"rule_id": scale["id"], "version": version, "state": "proposed", "account": "Acc",
                                 "level": "campaign", "id": CAMP_CBO, "kind": "budget_up",
                                 "before": {"daily_budget": 50}, "requested": {"daily_budget": 55}})
            ev = copy.deepcopy(store.evaluations()[0])
            ev.update(rule_id=scale["id"], version=version, strategy=3)
            ev["objects"][0].update(key="campaign:" + CAMP_CBO, level="campaign", id=CAMP_CBO,
                                     name="Synthetic CBO", action={"kind": "budget_up"})
            store.append_evaluation(ev)
            saved.campaigns += ci.parse_inventory([camp(CAMP_CBO, "Synthetic CBO", daily_budget="5000")], "campaign")[0]
            ci.put_result(saved)
            selected, target = scale["id"], "campaign:" + CAMP_CBO
        report = copy.deepcopy(_MOCK_REPORT)
        report["my_cm"] = OWNER
        report["countries"][0].update(country="Kuwait", open_recs={"Acc": []}, close_recs={})
        web._enrich_country_overview(report)
        mc = {"country": "Kuwait", "key": "KW", "rows": [dict(account="Acc", has_data=True, spend=100,
                 leads=2, deposits=0, ready=True, in_workbook=True, status={"status": "active", "status_text": "Active"})]}
        amap = dict(countries=[mc], available=[], all_accounts=["Acc"], live={}, fallback={}, splits={}, notes={},
                    dates=[D], date_from=D, date_to=D, span="")
        html = web.templates.env.get_template("report.html").render(
            request=MagicMock(), report=report, mlx_labels={"A": "Synthetic workspace"}, warnings=[], built_at=D,
            upload_errors={}, account_preflight={}, account_preflight_json={}, ref_options={}, ref_options_json={},
            account_alias_map_json={}, draft_status={}, account_map=amap,
            period={"date_from": D, "date_to": D, "span": ""}, sidebar_countries=web._sidebar_countries(report, amap), fb_backend="api")
        context = browser.new_context(viewport={"width": width, "height": 900}, color_scheme=theme)
        page = context.new_page()
        errors, requests = [], []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.add_init_script("localStorage.setItem('acl.theme', " + repr(theme) + ")")

        def handle(route):
            request = route.request
            parsed = urlparse(request.url)
            path = parsed.path
            requests.append((request.method, path))
            if request.resource_type == "document":
                return route.fulfill(content_type="text/html", body=html)
            if path.startswith("/static/"):
                asset = ROOT / "src/web/static" / Path(path).name
                if asset.exists():
                    return route.fulfill(path=str(asset))
            if request.resource_type == "script":
                return route.fulfill(content_type="application/javascript", body="")
            q = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            body = json.loads(request.post_data or "{}") if request.method == "POST" else {}
            rules = {"/rules/overview": lambda: rules_api.overview(q.get("owner", ""), store=store),
                     "/rules/cells": lambda: rules_api.cells(q.get("account", ""), q.get("owner", ""), store=store),
                     "/rules/action-result": lambda: rules_api.record_action_result(body, store=store),
                     "/rules/undo-request": lambda: rules_api.prepare_undo(body, store=store),
                     "/rules/undo-result": lambda: rules_api.record_undo_result(body, store=store)}
            if path in rules:
                data, code = rules[path]()
                return route.fulfill(status=code, json=data)
            if path.startswith("/campaign-commands/"):
                if path.endswith("/availability"):
                    data, code = campaign_commands.availability(), 200
                elif path.endswith("/preview"):
                    data, code = campaign_commands.run_preview(body)
                elif path.endswith("/execute"):
                    data, code = campaign_commands.run_execute(body)
                else:
                    data, code = campaign_commands.get_status(path.rsplit("/", 1)[-1])
                return route.fulfill(status=code, json=data)
            inventory = {"/campaign-inventory/status": web.campaign_inventory_status,
                         "/campaign-inventory/tree": web.campaign_inventory_tree,
                         "/campaign-inventory/children": web.campaign_inventory_children}
            if path in inventory:
                req = SimpleNamespace(query_params=QueryParams(parsed.query))
                with concurrent.futures.ThreadPoolExecutor(1) as pool:
                    response = pool.submit(lambda: asyncio.run(inventory[path](req))).result()
                return route.fulfill(status=response.status_code, content_type="application/json", body=response.body)
            return route.fulfill(json={"ok": True, "files": [], "texts": [], "profiles": [], "rows": []})

        page.route("**/*", handle)
        goto(page, "http://acl.test/" + ("#rules" if surface == "rules" else "#country-kuwait-manage"))
        page.wait_for_function("window.aclRulesPage.state.status === 'ready'")
        if surface == "rules":
            page.locator('.rl-item[data-rule="' + selected + '"]').click()
        if surface == "cell":
            scope = page.locator("#view-country-kuwait-manage .amap-inline")
            scope.locator(".amap-caret").first.click()
            if kind == "pause":
                scope.locator('[data-exp="campaign:' + CAMP_A + '"]').click()
            scope.locator('tr[data-key="' + target + '"] [data-rules-open]').click()
            page.locator("dialog.hc-rinsp[open]").wait_for()
        opened.append((context, page, errors, theme, width, surface))
        return page, world, store, requests

    yield open_page
    for context, page, errors, theme, width, surface in opened:
        if os.environ.get("ACL1822_SHOTS"):
            dest = Path(os.environ["ACL1822_SHOTS"])
            dest.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(dest / f"undo-{surface}-{theme}-{width}.png"))
        context.close()
        assert not errors, errors
    cmd._STORES.clear()


@pytest.mark.parametrize("surface", ["rules", "cell"])
@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [390, 768, 1024, 1280])
def test_full_dashboard_apply_then_undo_each_require_confirm(dashboard, surface, theme, width):
    page, world, store, requests = dashboard(theme, width, surface)
    page.locator("[data-apply]" if surface == "rules" else "[data-rapply]").first.click()
    page.locator("dialog.acm-confirm[open]").wait_for()
    assert world.meta.posts == []
    assert "Switch off 1 object" in page.inner_text("dialog.acm-confirm")
    page.locator('dialog.acm-confirm [data-acm="confirm"]').click()
    page.wait_for_function("!document.querySelector('dialog.acm-confirm[open]')")
    if surface == "cell":
        page.wait_for_function("document.querySelector('.hc-switch[data-object-id=\"2000002\"][aria-checked=\"false\"]')")
        page.locator('#view-country-kuwait-manage tr[data-key="adset:2000002"] [data-rules-open]').click()
    undo = page.locator("[data-undo]" if surface == "rules" else "[data-rundo]").first
    undo.wait_for()
    assert world.meta.posts == [("/" + SET_AUTO, {"status": "PAUSED"})]
    undo.focus()
    page.keyboard.press("Enter")
    page.locator("dialog.acm-confirm[open]").wait_for()
    assert "Switch on 1 object" in page.inner_text("dialog.acm-confirm")
    assert len(world.meta.posts) == 1, "Undo preview must not write"
    page.locator('dialog.acm-confirm [data-acm="cancel"]').click()
    if surface == "cell":
        page.locator('#view-country-kuwait-manage tr[data-key="adset:2000002"] [data-rules-open]').click()
    undo.wait_for(state="visible")
    assert len(world.meta.posts) == 1, "Cancel must leave the applied value and journal unchanged"
    undo.click()
    page.locator("dialog.acm-confirm[open]").wait_for()
    page.locator('dialog.acm-confirm [data-acm="confirm"]').click()
    if surface == "rules":
        page.get_by_role("region", name="Applied actions").get_by_text("Undone", exact=True).wait_for()
    else:
        page.wait_for_function("document.querySelector('.hc-switch[data-object-id=\"2000002\"][aria-checked=\"true\"]')")
    assert world.meta.posts == [("/" + SET_AUTO, {"status": "PAUSED"}), ("/" + SET_AUTO, {"status": "ACTIVE"})]
    assert world.meta.objs[CAMP_A]["status"] == "PAUSED", "Undo must never enable the parent"
    page.wait_for_timeout(100)
    last = next(iter(store.latest_actions().values()))
    assert last["state"] == "undone" and last["op_id"] != last["undo_op_id"]
    assert ("POST", "/rules/undo-result") in requests
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


@pytest.mark.parametrize("surface", ["rules", "cell"])
def test_scale_undo_restores_confirmed_budget_with_another_human_confirm(dashboard, surface):
    page, world, store, _ = dashboard("light", 1280, surface, kind="budget_up")
    page.locator("[data-apply]" if surface == "rules" else "[data-rapply]").first.click()
    page.locator("dialog.acm-confirm[open]").wait_for()
    assert world.meta.posts == []
    page.locator('dialog.acm-confirm [data-acm="confirm"]').click()
    if surface == "cell":
        page.wait_for_selector('#view-country-kuwait-manage tr[data-key="campaign:1000002"]:has-text("$55.00 / day")', timeout=15000)
        page.locator('#view-country-kuwait-manage tr[data-key="campaign:1000002"] [data-rules-open]').click()
    undo = page.locator("[data-undo]" if surface == "rules" else "[data-rundo]").first
    undo.wait_for()
    assert world.meta.posts == [("/" + CAMP_CBO, {"daily_budget": "5500"})]
    undo.click()
    page.locator("dialog.acm-confirm[open]").wait_for()
    assert "55.00" in page.inner_text("dialog.acm-confirm") and "50.00" in page.inner_text("dialog.acm-confirm")
    assert len(world.meta.posts) == 1
    page.locator('dialog.acm-confirm [data-acm="confirm"]').click()
    if surface == "rules":
        page.get_by_role("region", name="Applied actions").get_by_text("Undone", exact=True).wait_for()
    else:
        page.wait_for_selector('#view-country-kuwait-manage tr[data-key="campaign:1000002"]:has-text("$50.00 / day")', timeout=15000)
    assert world.meta.posts == [("/" + CAMP_CBO, {"daily_budget": "5500"}), ("/" + CAMP_CBO, {"daily_budget": "5000"})]
    assert world.meta.objs[CAMP_CBO]["daily_budget"] == "5000"
