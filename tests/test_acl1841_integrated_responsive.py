"""Full Home/Manage routes: grouped controls and table actions clear the Builder.

Production templates, CSS, scripts and cached-inventory handlers run in offline
Chromium. All machine data is temporary; no spend read or advertising command runs.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import copy
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from urllib.parse import urlparse

import pytest
from playwright.sync_api import sync_playwright
from starlette.datastructures import QueryParams

from src.analysis_agent import campaign_inventory as ci
from src.analysis_agent import account_spend_api
from src.web import app as web
from tests.test_acl1798_campaign_inventory import D, snapshot
from tests.test_web_drill import _MOCK_REPORT

ROOT = Path(__file__).resolve().parents[1]
ACCOUNT = ("Synthetic account with a long descriptive identity " * 3).strip()
COUNTRY = "Kuwait"


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        instance = pw.chromium.launch(executable_path=os.environ.get("ACL_BROWSER_EXECUTABLE") or None)
        yield instance
        instance.close()


@pytest.fixture
def integrated(browser, monkeypatch, tmp_path):
    monkeypatch.setenv(account_spend_api.BACKEND_ENV, "api")
    monkeypatch.setattr(ci, "_store_path", lambda: tmp_path / "inventory.json")
    monkeypatch.setattr(web, "_inventory_bi", lambda *a: ({"campaigns": {}, "ads": {}}, ""))
    monkeypatch.setattr(web, "_inventory_goal_context", lambda *a: {
        "goal_cpa": None, "benchmark_cpa": None, "goal_note": "Synthetic goal is not set",
        "benchmark_note": "Synthetic benchmark is not set"})
    ci._RUNNING.clear()
    opened = []

    def open_page(width, theme, route, populated=True):
        saved = snapshot(account=ACCOUNT)
        if not populated:
            saved.campaigns = []; saved.adsets = []; saved.ads = []
        ci.put_result(saved)
        report = copy.deepcopy(_MOCK_REPORT)
        country = report["countries"][0]
        country.update(country=COUNTRY, open_recs={ACCOUNT: []}, close_recs={})
        report["my_cm"] = "Synthetic manager"
        web._enrich_country_overview(report)
        row = dict(account=ACCOUNT, has_data=True, spend=1044.82, leads=42, deposits=3,
                   ready=True, in_workbook=True, status={"status": "disabled", "status_text": "Synthetic disabled status"})
        mc = dict(country=COUNTRY, key="KW", rows=[row])
        amap = dict(countries=[mc], available=[], all_accounts=[ACCOUNT], live={}, fallback={},
                    splits={}, notes={}, dates=[D], date_from=D, date_to=D, span="")
        html = web.templates.env.get_template("report.html").render(
            request=MagicMock(), report=report, mlx_labels={"A": "Synthetic workspace"}, warnings=[],
            built_at=D, upload_errors={}, account_preflight={}, account_preflight_json={},
            ref_options={}, ref_options_json={}, account_alias_map_json={}, draft_status={},
            account_map=amap, period={"date_from": D, "date_to": D, "span": ""},
            sidebar_countries=web._sidebar_countries(report, amap), fb_backend="api")
        context = browser.new_context(viewport={"width": width, "height": 900}, color_scheme=theme)
        page = context.new_page()
        errors, writes = [], []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.add_init_script("localStorage.setItem('acl.theme', " + repr(theme) + ")")

        def handle(r):
            path = urlparse(r.request.url).path
            if r.request.resource_type == "document":
                return r.fulfill(content_type="text/html", body=html)
            if path.startswith("/static/"):
                asset = ROOT / "src/web/static" / Path(path).name
                if asset.exists():
                    return r.fulfill(path=str(asset))
            if r.request.resource_type == "script":
                return r.fulfill(content_type="application/javascript", body="")
            if path == "/campaign-commands/availability":
                return r.fulfill(json={"available": True, "max_targets": 20})
            handlers = {"/campaign-inventory/status": web.campaign_inventory_status,
                        "/campaign-inventory/tree": web.campaign_inventory_tree,
                        "/campaign-inventory/children": web.campaign_inventory_children}
            if path in handlers:
                request = SimpleNamespace(query_params=QueryParams(urlparse(r.request.url).query))
                with concurrent.futures.ThreadPoolExecutor(1) as pool:
                    response = pool.submit(lambda: asyncio.run(handlers[path](request))).result()
                return r.fulfill(status=response.status_code, content_type="application/json", body=response.body)
            if path in {"/campaign-inventory/read", "/campaign-commands/preview", "/campaign-commands/execute"}:
                writes.append(path)
                return r.fulfill(status=409, json={"error": "Live actions are not part of this test"})
            return r.fulfill(json={"ok": True, "files": [], "texts": [], "profiles": [], "rows": []})

        page.route("**/*", handle)
        page.goto("http://acl.test/" + ("#country-kuwait-manage" if route == "manage" else ""))
        scope = page.locator("#view-country-kuwait-manage .amap-inline" if route == "manage" else '#view-home .amap-country[data-country-name="Kuwait"]')
        if route == "home":
            scope.locator("summary").first.click()
        page.wait_for_function("document.querySelector('.view-panel.active .amap-campaigns')?._hc?.ready")
        host = scope.locator(".amap-campaigns")
        host.locator("[data-hc=body]:not([hidden])").wait_for(state="attached")
        opened.append((context, page, errors, writes, route, theme, width, populated))
        return page, scope, host

    yield open_page
    for context, page, errors, writes, route, theme, width, populated in opened:
        shot_dir = os.environ.get("ACL1841_SHOTS")
        if shot_dir and width in (390, 1280):
            path = Path(shot_dir); path.mkdir(parents=True, exist_ok=True)
            drawer = "open" if page.locator("body.cbd-open").count() else "closed"
            page.screenshot(path=str(path / f"{route}-{theme}-{width}-{'populated' if populated else 'empty'}-{drawer}.png"))
        context.close()
        assert not writes, writes
        assert not errors, errors


def no_horizontal_page_overflow_or_main_pan(page):
    # The legacy Home country-overview grid is wider than a narrow main pane.
    # Its scrollWidth is not page overflow. Target controls must neither expand
    # the document nor pan that whole pane when keyboard focus moves among them.
    bounds = page.evaluate("""() => {
      const main = document.querySelector('.main-content');
      return {page: document.documentElement.scrollWidth, viewport: innerWidth,
              mainPan: main.scrollLeft};
    }""")
    assert bounds["page"] <= bounds["viewport"] + 1, bounds
    assert abs(bounds["mainPan"]) <= 1, bounds


@pytest.mark.parametrize("width", [1280, 1024, 768, 390])
@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("route", ["home", "manage"])
@pytest.mark.parametrize("inventory", ["populated", "empty"])
def test_full_routes_have_no_toolbar_and_keep_the_table_and_add_form_reachable(integrated, width, theme, route, inventory):
    """Панель инструментов над таблицей аккаунтов удалена 04.11: остались вкладки, таблица аккаунтов и форма добавления."""
    page, scope, host = integrated(width, theme, route, inventory == "populated")
    for gone in (".hc-toolbar", "[data-hc=search]", "[data-hc=delivery]", "[data-hc=expand-all]", "[data-hc=collapse-all]",
                 "[data-hc=preset]", "[data-hc=batch]", ".hc-cols", "[data-jump]"):
        assert host.locator(gone).count() == 0 and scope.locator(gone).count() == 0, gone
    assert scope.locator(".amap-table-scroll").count() == 1 and scope.locator(".amap-add").count() == 1
    assert scope.locator(".amap-add").first.is_visible()
    no_horizontal_page_overflow_or_main_pan(page)


def assert_action_clearance(page, scope):
    scroller = scope.locator(".amap-table-scroll")
    assert scroller.evaluate("e => e.children.length === 1 && e.children[0].tagName === 'TABLE'")
    assert scroller.locator(".hc-toolbar,[data-hc-after],.amap-add").count() == 0
    scroller.focus()
    page.keyboard.press("End")
    for button in scroller.locator(".amap-actions button").all():
        if not button.is_visible():
            continue
        button.focus()
        button.evaluate("e => e.scrollIntoView({block:'center',inline:'nearest'})")
        page.wait_for_timeout(30)
        hit = button.evaluate("""e => {
          const r = e.getBoundingClientRect(), top = document.elementFromPoint(r.x+r.width/2, r.y+r.height/2);
          return {reachable: top === e || e.contains(top), right:r.right,
                  handleLeft:document.querySelector('#cbd-handle').getBoundingClientRect().left};
        }""")
        assert hit["reachable"], hit
        assert hit["right"] <= hit["handleLeft"] + 1, hit


@pytest.mark.parametrize("width", [1280, 1024, 768, 390])
@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("route", ["home", "manage"])
def test_full_routes_keep_the_add_form_and_builder_clear_of_table_actions(integrated, width, theme, route):
    page, scope, host = integrated(width, theme, route)
    assert_action_clearance(page, scope)
    if route == "home":
        container = page.locator("#view-home .amap-head")
        outer = container.bounding_box()
        for control in container.locator("button,input,select").all():
            if not control.is_visible():
                continue
            control.focus()
            rect = control.bounding_box()
            assert outer["x"] - 1 <= rect["x"] and rect["x"] + rect["width"] <= outer["x"] + outer["width"] + 1
    positions = scope.evaluate("""root => {
      const r = s => root.querySelector(s).getBoundingClientRect();
      return {table:r('.amap-table-scroll').bottom, form:r('.amap-add').top};
    }""")
    assert positions["table"] <= positions["form"]
    assert scope.locator("[data-hc-after], .hc-foot, .hc-hist").count() == 0
    no_horizontal_page_overflow_or_main_pan(page)
