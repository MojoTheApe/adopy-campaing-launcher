"""Настоящие шаблон/Chromium/смена периода, только синтетические источники."""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlparse

import pytest

from src.web import app as web
from tests import ds_routes_harness as H
from tests.test_acl103_source_contract import history_frame, isolated_source  # noqa: F401
from tests.test_acl103_saved_source import saved_comparison_context  # noqa: F401

pw = pytest.importorskip("playwright.sync_api")


def local_refresh_route(calls=None):
    """Real tracked JS; the local RPA backend refuses profile-read admission."""
    from src.analysis_agent import campaign_inventory

    fallback = H.make_route("")
    def route(request):
        path = urlparse(request.request.url).path
        if calls is not None:
            calls.append((request.request.method, path))
        # The maintained conftest layer already serves tracked /static/*.js bytes.
        if path == "/account-refresh/page-open":
            request.fulfill(status=409, json={"ok": False,
                "code": campaign_inventory.NEEDS_API_CODE,
                "error": campaign_inventory.NEEDS_API_MESSAGE})
        elif path == "/account-refresh/state":
            request.fulfill(json={"ok": True, "rows": {}, "accounts": {}, "queued": [],
                "running": None, "sweep": None, "halt": None, "needs_api": True})
        elif path == "/campaign-inventory/status":
            request.fulfill(json={"ok": True, "backend": "rpa", "needs_api": True,
                "message": campaign_inventory.NEEDS_API_MESSAGE, "running": [], "accounts": {}})
        elif path == "/campaign-commands/availability":
            request.fulfill(json={"available": False, "reason": "API backend is disabled", "max_targets": 0})
        elif path == "/warm/state":
            request.fulfill(json={"available": False, "accounts": {}, "actions": False})
        else:
            fallback(request)
    return route


@pytest.mark.parametrize("width,theme", [(390, "dark"), (1365, "light")])
def test_saved_comparison_opens_and_changes_dates_without_an_external_read(browser, saved_comparison_context, tmp_path, width, theme):
    context = browser.new_context(viewport={"width": width, "height": 900}, color_scheme=theme)
    page = context.new_page()
    errors, forbidden, calls, navigations = [], [], [], []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.add_init_script(f"localStorage.setItem('acl.theme', '{theme}')")
    fallback = local_refresh_route(calls)
    def route(request):
        path = urlparse(request.request.url).path
        if path in {"/api/source-vs-account", "/account-refresh/request", "/account-spend/read", "/campaign-inventory/read"}:
            forbidden.append(path)
            request.abort()
        elif request.request.resource_type == "document":
            query = parse_qs(urlparse(request.request.url).query)
            navigations.append(query)
            start = query.get("date_from", ["2026-10-18"])[0]
            end = query.get("date_to", [start])[0]
            request.fulfill(content_type="text/html", body=saved_comparison_context["render"](start, end, full=True))
        else:
            fallback(request)
    page.route("**/*", route)
    try:
        with page.expect_response(lambda r: urlparse(r.url).path == "/static/account_refresh.js") as script:
            with page.expect_response(lambda r: urlparse(r.url).path == "/account-refresh/state"):
                page.goto("http://acl.test/?date_from=2026-10-18&date_to=2026-10-18")
        assert script.value.body() == (H.ROOT / "src/web/static/account_refresh.js").read_bytes()
        assert calls.count(("POST", "/account-refresh/page-open")) == 1
        panel = page.locator("#amap-slot details[data-source-check]")
        assert panel.count() == 1, "API presence alone is not a visible comparison"
        assert not panel.evaluate("e => e.open")
        panel.locator("summary").focus()
        page.keyboard.press("Enter")
        assert panel.evaluate("e => e.open")
        assert "USD 12.50" in panel.inner_text() and "USD 14.00" in panel.inner_text()
        assert panel.locator("[data-source-account]").count() == 1
        assert panel.locator("button, [onclick], [hx-get]").count() == 0
        geometry = panel.evaluate("e => {const r=e.getBoundingClientRect();return {left:r.left,right:r.right,scroll:e.scrollWidth,client:e.clientWidth}}")
        assert geometry["left"] >= 0 and geometry["right"] <= width + 1, geometry
        assert geometry["scroll"] <= geometry["client"] + 1, geometry
        artifacts = Path(os.environ.get("ACL_REVIEW_ARTIFACT_DIR", str(tmp_path)))
        artifacts.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(artifacts / f"saved-check-{width}-{theme}.png"))
        with page.expect_response(lambda r: urlparse(r.url).path == "/account-refresh/state"):
            page.evaluate("amapApply('2026-01-05','2026-01-05','')")
        page.wait_for_url("**date_from=2026-01-05&date_to=2026-01-05*")
        assert navigations[-1]["view_only"] == ["1"]
        assert calls.count(("POST", "/account-refresh/page-open")) == 1
        assert calls.count(("GET", "/account-refresh/state")) >= 2
        panel.locator("summary").click()
        assert "USD 7.00" in panel.inner_text()
        assert "No saved account figure for these dates" in panel.inner_text()
        assert "USD 14.00" not in panel.inner_text() and "999.00" not in panel.inner_text()
        assert not forbidden and not errors and saved_comparison_context["blocked"] == []
    finally:
        context.close()


@pytest.fixture(scope="module")
def browser():
    with pw.sync_playwright() as playwright:
        instance = H.browser_launch(playwright)
        yield instance
        instance.close()


@pytest.mark.parametrize("width,theme,currency", [(1365, "light", "EUR"), (1365, "dark", ""), (390, "light", "EUR"), (390, "dark", "")])
def test_real_period_navigation_keeps_source_and_unknown_metadata_visible(browser, monkeypatch, tmp_path, width, theme, currency):
    for prefix in ["BI", "STATS"]:
        for key in ["CURRENCY", "TIMEZONE", "DAY_BOUNDARY"]:
            monkeypatch.delenv(prefix + "_" + key, raising=False)
    monkeypatch.setenv("BI_CURRENCY", currency)
    from src.web import account_links
    missing_map = str(tmp_path / "no-map.csv")
    monkeypatch.setattr(account_links, "get_ad_account_map_path", lambda: missing_map)
    monkeypatch.setattr(web, "get_ad_account_map_path", lambda: missing_map)
    account_links.link("FacebookDoc101", "country", "Mexico")
    report, frame = history_frame(monkeypatch, tmp_path)

    def render(url):
        query = parse_qs(urlparse(url).query)
        start = query.get("date_from", ["2026-01-05"])[0]
        end = query.get("date_to", ["2026-10-18"])[0]
        scoped = web.report_for_period(report, frame, start, end)
        web._enrich_country_overview(scoped)
        account_map = web._build_account_map_view(scoped, frame, start, end)
        return web.templates.env.get_template("report.html").render(
            request=MagicMock(), report=scoped, warnings=[scoped["source_info"]["note"]],
            built_at="2026-10-24", mlx_labels={}, upload_errors={}, account_preflight={},
            account_preflight_json={}, ref_options={}, ref_options_json={},
            account_alias_map_json={}, draft_status={},
            account_map=account_map,
            sidebar_countries=web._sidebar_countries(scoped, account_map), home_kpis=web._home_kpis(scoped),
            period={"date_from": start, "date_to": end, "span": ""})

    context = browser.new_context(viewport={"width": width, "height": 900}, color_scheme=theme)
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.add_init_script(f"localStorage.setItem('acl.theme', '{theme}')")
    fallback = local_refresh_route()
    def route(request):
        if request.request.resource_type == "document":
            request.fulfill(content_type="text/html", body=render(request.request.url))
        else:
            fallback(request)
    page.route("**/*", route)
    try:
        page.goto("http://acl.test/?date_from=2026-01-05&date_to=2026-10-18")
        info = page.locator("#acl-source-info")
        assert info.is_visible()
        assert "BI + Stats workbook" in info.inner_text()
        assert "incomplete metadata" not in info.inner_text()  # ACL-2248: зона/граница дня по умолчанию
        assert "time zone: Asia/Jerusalem (default)" in info.get_attribute("title")
        # ACL-2204: без объявленной валюты — USD; EUR из BI + книга без валюты (USD) — смесь, не сумма.
        if currency:
            assert "currency: unknown" in info.get_attribute("title")
        else:
            assert "currency: unknown" not in info.get_attribute("title")
            assert "currency: USD (default)" in info.get_attribute("title")
        geometry = info.evaluate("e => {const r=e.getBoundingClientRect();return {left:r.left,right:r.right,scroll:e.scrollWidth,client:e.clientWidth,viewport:innerWidth}}")
        assert geometry["left"] >= 0 and geometry["right"] <= width + 1, geometry
        assert geometry["scroll"] <= geometry["client"] + 1, geometry
        assert "numbers from the selected source data" in page.locator(".amap-head").inner_text()
        # Pure-BI selection proves real server cells and the JS twin use the retained unit.
        page.evaluate("amapApply('2026-10-18','2026-10-18','')")
        page.wait_for_url("**date_from=2026-10-18&date_to=2026-10-18*")
        assert "numbers from the BI data" in page.locator(".amap-head").inner_text()
        assert page.evaluate("AMAP_SOURCE") == "the BI data"
        assert page.evaluate("AMAP_BOOK_SOURCE") is False
        assert page.evaluate("AMAP_SHARED_TIP").startswith("The source data")
        expected = "EUR 13" if currency else "$13"
        assert page.evaluate("acl6Metrics(12.5,5,1)[0]") == expected
        # ACL-2204: подпись суммы с Meta без валюты не меняется (только BI / книга получают USD).
        assert page.evaluate("amapMoney(5, '')") == "currency unknown 5.00"
        page.locator("#amap-slot details.amap-country > summary").first.click()
        assert expected in page.locator("#amap-slot .amap-grid").inner_text()
        if not currency:
            assert "currency unknown" not in page.locator("#home-kpis").inner_text()
            assert page.locator('#home-kpis [data-kpi="spend"] .hk-you').inner_text().strip().startswith("$")
        artifacts = Path(os.environ.get("ACL_REVIEW_ARTIFACT_DIR", str(tmp_path)))
        artifacts.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(artifacts / f"source-{width}-{theme}.png"))
        # Настоящий выбор периода перезагружает страницу, не оставляя старую подпись.
        page.evaluate("amapApply('2026-01-05','2026-01-05','')")
        page.wait_for_url("**date_to=2026-01-05*")
        assert "Stats workbook" in info.inner_text() and "BI +" not in info.inner_text()
        assert page.evaluate("AMAP_SOURCE") == "the Stats workbook"
        assert page.evaluate("AMAP_BOOK_SOURCE") is True
        assert page.evaluate("AMAP_SHARED_TIP").startswith("The workbook")
        assert "numbers from the Stats workbook" in page.locator(".amap-head").inner_text()
        assert "2026-01-05" in page.locator(".amap-head").inner_text()
        assert "USD" in info.inner_text() and "incomplete metadata" not in info.inner_text()
        assert "unknown" not in info.inner_text()  # книга без валюты — USD, не unknown
        assert not errors, errors
    finally:
        context.close()


@pytest.mark.parametrize("width,theme,sources,caption", [
    (390, "dark", ["bi"], "The BI data have no rows"),
    (1365, "light", ["bi", "workbook"], "The selected statistics have no rows"),
])
def test_empty_source_notice_keeps_the_real_map_and_wraps_on_screen(browser, width, theme, sources, caption, tmp_path):
    from tests.test_web_build_upload import _minimal_report

    report = _minimal_report()
    web._enrich_country_overview(report)
    account_map = dict(countries=[], available=[], all_accounts=[], live={}, span="",
                       date_from="2026-09-09", date_to="2026-09-09", dates=["2026-09-08"],
                       source_info={"sources": sources})
    html = web.templates.env.get_template("report.html").render(
        request=MagicMock(), report=report, warnings=[], built_at="2026-10-24", mlx_labels={},
        upload_errors={}, account_preflight={}, account_preflight_json={}, ref_options={},
        ref_options_json={}, account_alias_map_json={}, draft_status={}, account_map=account_map,
        period={"date_from": "2026-09-09", "date_to": "2026-09-09", "span": ""})
    context = browser.new_context(viewport={"width": width, "height": 900}, color_scheme=theme)
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.add_init_script(f"localStorage.setItem('acl.theme', '{theme}')")
    fallback = local_refresh_route()
    def route(request):
        if request.request.resource_type == "document":
            request.fulfill(content_type="text/html", body=html)
        else:
            fallback(request)
    page.route("**/*", route)
    try:
        page.goto("http://acl.test/?date_from=2026-09-09&date_to=2026-09-09")
        notice = page.locator('#workbook-period-notice[role="status"]')
        assert notice.is_visible() and caption in notice.inner_text()
        assert "2026-09-09" in notice.inner_text() and "It runs to 2026-09-08" in notice.inner_text()
        assert "The workbook has no rows" not in notice.inner_text()
        assert page.locator("#amap-slot").count() == 1 and page.locator("#amap-slot").is_visible()
        # Later ACL-2063 removes manual Meta reads; the empty source map stays visible.
        assert page.get_by_role("button", name="Read all spend", exact=True).count() == 0
        geometry = notice.evaluate("e => {const r=e.getBoundingClientRect();return {left:r.left,right:r.right,scroll:e.scrollWidth,client:e.clientWidth}}")
        assert geometry["left"] >= 0 and geometry["right"] <= width + 1, geometry
        assert geometry["scroll"] <= geometry["client"] + 1, geometry
        assert notice.bounding_box()["y"] < page.locator(".dashboard-shell").bounding_box()["y"]
        artifacts = Path(os.environ.get("ACL_REVIEW_ARTIFACT_DIR", str(tmp_path)))
        artifacts.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(artifacts / f"empty-source-{width}-{theme}.png"))
        assert not errors, errors
    finally:
        context.close()
