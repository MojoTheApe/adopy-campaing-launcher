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

pw = pytest.importorskip("playwright.sync_api")


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
    fallback = H.make_route("")
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
        assert "BI + Stats workbook" in info.inner_text() and "unknown" in info.inner_text()
        assert "incomplete metadata" in info.inner_text()
        assert "time zone: unknown" in info.get_attribute("title")
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
        expected = "EUR 13" if currency else "currency unknown 13"
        assert page.evaluate("acl6Metrics(12.5,5,1)[0]") == expected
        page.locator("#amap-slot details.amap-country > summary").first.click()
        assert expected in page.locator("#amap-slot .amap-grid").inner_text()
        if not currency:
            assert page.locator('#home-kpis [data-kpi="spend"] .hk-you').evaluate("e => {const c=document.createElement('canvas').getContext('2d');c.font=getComputedStyle(e).font;return c.measureText('unknown').width <= e.clientWidth}")
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
        assert "unknown" in info.inner_text() and "incomplete metadata" in info.inner_text()
        assert not errors, errors
    finally:
        context.close()
