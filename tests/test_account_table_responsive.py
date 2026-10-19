"""Offline geometry checks of the shared Home / country Manage account section."""
from pathlib import Path
import os
import re

import pytest
from jinja2 import Environment, FileSystemLoader
from playwright.sync_api import sync_playwright

from src.analysis_agent.metric_columns import metric_values
from src.analysis_agent.money_format import fmt_currency, fmt_usd

TPL = Path(__file__).resolve().parents[1] / "src/web/templates"


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get("ACL_BROWSER_EXECUTABLE") or None)
        yield browser
        browser.close()


def render_section(route, populated=True, campaign_api=False):
    env = Environment(loader=FileSystemLoader(str(TPL)), autoescape=True)
    env.filters.update(fmt_usd=fmt_usd, fmt_currency=fmt_currency)
    env.globals["metric_values"] = metric_values
    env.globals["campaign_table_api"] = lambda: campaign_api
    row = dict(account="Synthetic account with a long descriptive identity " * 3,
               has_data=True, spend=1044.82, leads=42, deposits=3,
               status={"status": "clear"})
    block = env.get_template("_account_map_country.html").render(
        mc=dict(country="Synthetic country", key="TEST", rows=[row] if populated else []),
        amap=dict(live={}, fallback={}, splits={}, notes={}, all_accounts=[],
                  span="day", date_from="2026-10-13"), mlx={"A": "Synthetic workspace"})
    if route == "home":
        return '<div class="amap"><details class="amap-country" open><summary>Synthetic country</summary>' + block + '</details></div>'
    return '<h3 class="amap-inline-title">Accounts in Synthetic country</h3><div class="amap amap-inline">' + block + '</div>'


@pytest.mark.parametrize("width", [1280, 1024, 768, 390])
@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("route", ["home", "manage"])
def test_account_identity_and_actions_remain_reachable(browser, width, theme, route):
    report = (TPL / "report.html").read_text(encoding="utf-8")
    styles = "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", report, re.S))
    page = browser.new_page(viewport={"width": width, "height": 900})
    try:
        # Full production CSS and shared partial; no live handlers or network requests.
        page.set_content(f'<html data-theme="{theme}"><style>{styles}</style><main class="main-content">{render_section(route)}</main></html>')
        scroll = page.locator(".amap-table-scroll")
        assert scroll.count() == 1, "The account table needs its own scroll region"
        assert scroll.get_attribute("tabindex") == "0"
        assert page.locator(".amap-acc").bounding_box()["width"] >= 250
        scroll.focus()
        scroll.evaluate("el => { el.scrollLeft = el.scrollWidth; }")
        page.wait_for_timeout(200)
        page.locator(".amap-read").focus()
        bounds = page.locator(".amap-read").bounding_box()
        region = scroll.bounding_box()
        assert region["x"] <= bounds["x"]
        assert bounds["x"] + bounds["width"] <= region["x"] + region["width"] + 1
        assert page.locator(".amap-actions").evaluate("el => el.scrollWidth <= el.clientWidth")
        # ACL-1798's staged amapCampaignTree appends this exact action to the same
        # cell. A fourth profile action covers its recovery path without live RPA.
        page.locator(".amap-actions").evaluate("""el => {
            el.insertAdjacentHTML('beforeend', '<button type="button" class="amap-btn hc-rc">Read campaigns</button><button type="button" class="amap-btn hc-ro">Open profile</button>');
        }""")
        for button in page.locator(".amap-actions button").all():
            button.focus()
            bounds = button.bounding_box()
            assert region["x"] <= bounds["x"]
            assert bounds["x"] + bounds["width"] <= region["x"] + region["width"] + 1
        assert page.locator(".amap-actions").evaluate("el => el.scrollWidth <= el.clientWidth")
        assert scroll.evaluate("el => el.scrollWidth >= el.clientWidth")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert page.locator(".amap-add").bounding_box()["y"] >= scroll.bounding_box()["y"] + scroll.bounding_box()["height"]
        # Empty inventory retains its explanation without creating a blank scroller.
        page.set_content(f'<html data-theme="{theme}"><style>{styles}</style><main class="main-content">{render_section(route, False)}</main></html>')
        assert page.locator(".amap-table-scroll").count() == 0
        assert "No accounts placed" in page.locator(".amap-empty").inner_text()
    finally:
        page.close()


@pytest.mark.parametrize("width", [1280, 1024, 768, 390])
@pytest.mark.parametrize("route", ["home", "manage"])
def test_scroll_region_holds_only_the_table_beside_the_campaign_sections(browser, width, route):
    """ACL-1800 markup: campaigns section above the table, footer mount below; both stay outside the scroller."""
    report = (TPL / "report.html").read_text(encoding="utf-8")
    styles = "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", report, re.S))
    page = browser.new_page(viewport={"width": width, "height": 900})
    try:
        page.set_content(f'<html data-theme="light"><style>{styles}</style><main class="main-content">{render_section(route, True, True)}</main></html>')
        scroll = page.locator(".amap-table-scroll")
        assert scroll.count() == 1
        assert page.locator(".amap-campaigns").count() == 1 and page.locator("[data-hc-after]").count() == 1
        assert scroll.evaluate("el => el.children.length === 1 && el.children[0].tagName === 'TABLE'")
        assert scroll.locator(".amap-campaigns, [data-hc-after]").count() == 0
        order = page.evaluate("""() => {
            const y = s => document.querySelector(s).getBoundingClientRect().top;
            return [y('.amap-campaigns'), y('.amap-table-scroll'), y('[data-hc-after]')];
        }""")
        assert order[0] <= order[1] <= order[2]
        scroll.focus()
        scroll.evaluate("el => { el.scrollLeft = el.scrollWidth; }")
        page.wait_for_timeout(200)
        page.locator(".amap-read").focus()
        page.wait_for_timeout(300)
        bounds, region = page.locator(".amap-read").bounding_box(), scroll.bounding_box()
        assert region["x"] <= bounds["x"] and bounds["x"] + bounds["width"] <= region["x"] + region["width"] + 1
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    finally:
        page.close()
