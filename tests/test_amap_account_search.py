"""Общий поиск аккаунтов над странами на Home (план 04.11, §4).

Разметка поля берётся из `_account_map.html`, строки стран — из `_account_map_country.html`, скрипт — из `report.html`
(куски вырезаются как есть), страница собирается офлайн в Chromium без сети.
"""
from pathlib import Path
import os
import re

import pytest
from jinja2 import Environment, FileSystemLoader
from playwright.sync_api import sync_playwright

from src.analysis_agent.metric_columns import metric_values
from src.analysis_agent.money_format import fmt_currency, fmt_usd

TPL = Path(__file__).resolve().parents[1] / "src/web/templates"
REPORT = (TPL / "report.html").read_text(encoding="utf-8")
MAP = (TPL / "_account_map.html").read_text(encoding="utf-8")
COUNTRY = (TPL / "_account_map_country.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get("ACL_BROWSER_EXECUTABLE") or None)
        yield browser
        browser.close()


def _row(account, alias=""):
    return dict(account=account, workbook_alias=alias, has_data=True, spend=10.0, leads=1, deposits=0, status={"status": "clear"})


def _country(env, name, rows):
    return env.get_template("_account_map_country.html").render(
        mc=dict(country=name, key=name, rows=rows),
        amap=dict(live={}, fallback={}, splits={}, notes={}, all_accounts=[], span="day", date_from="2026-10-13"), mlx={})


def _search_markup():
    start = MAP.index('<div class="amap-search-row"')
    end = MAP.index('id="amap-search-empty" hidden></div>') + len('id="amap-search-empty" hidden></div>')
    return MAP[start:end]


def _script():
    start = REPORT.index("var AMAP_OPEN_KEY")
    end = REPORT.index("document.addEventListener('acl:viewchange', function () { setTimeout(function () { amapSearchInit();")
    return REPORT[start:end]


def _page(browser, width=1280, theme="light"):
    env = Environment(loader=FileSystemLoader(str(TPL)), autoescape=True)
    env.filters.update(fmt_usd=fmt_usd, fmt_currency=fmt_currency)
    env.globals["metric_values"] = metric_values
    env.globals["campaign_table_api"] = lambda: False
    styles = "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", REPORT, re.S))
    countries = {
        "Chile": [_row("KLD_BFL_B95", "FacebookMKTOBFLB95"), _row("Facebook_5265_61579362958966")],
        "Peru": [_row("KLD_BFL_B88", "FacebookMKTOBFLB88")],
        "Kuwait": [_row("Doc120")],
    }
    body = "".join(
        f'<details class="amap-country" data-country="{n}"><summary class="amap-row">{n}</summary>{_country(env, n, r)}</details>'
        for n, r in countries.items())
    page = browser.new_page(viewport={"width": width, "height": 900})
    html = (f'<html data-theme="{theme}"><style>{styles}</style><main class="main-content"><div class="amap">'
            f'{_search_markup()}{body}</div></main><script>{_script()}\n'
            "document.addEventListener('acl:viewchange', function () {});</script></html>")
    page.route("http://acl.test/**", lambda route: route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html))
    page.goto("http://acl.test/")                                              # не about:blank: localStorage нужен настоящий origin
    page.evaluate("() => { amapWatchOpen(); amapSearchInit(); }")
    return page


def _type(page, text):
    page.fill("#amap-search-all", text)
    page.wait_for_timeout(200)


def _visible_countries(page):
    return page.evaluate("() => [...document.querySelectorAll('details.amap-country')].filter(d => !d.hidden).map(d => d.dataset.country)")


def test_rows_carry_the_account_and_workbook_names_lowercased():
    env = Environment(loader=FileSystemLoader(str(TPL)), autoescape=True)
    env.filters.update(fmt_usd=fmt_usd, fmt_currency=fmt_currency)
    env.globals["metric_values"] = metric_values
    env.globals["campaign_table_api"] = lambda: False
    html = _country(env, "Chile", [_row("KLD_BFL_B95", "FacebookMKTOBFLB95")])
    assert 'data-search="kld_bfl_b95 facebookmktobflb95"' in html
    assert 'id="amap-search-all"' in _search_markup() and 'placeholder="Find account or profile"' in _search_markup()
    assert 'aria-live="polite"' in _search_markup() and 'role="search"' in _search_markup()


def test_search_filters_rows_across_countries_and_counts(browser):
    page = _page(browser)
    try:
        _type(page, "b95")
        assert _visible_countries(page) == ["Chile"]
        assert page.evaluate("() => [...document.querySelectorAll('tr[data-search]')].filter(t => !t.hidden).length") == 1
        assert page.inner_text("#amap-search-count") == "1 account in 1 country"
        assert page.locator("tr[data-search]:not([hidden]) td.amap-acc.amap-found").count() == 1
        _type(page, "5265")
        assert _visible_countries(page) == ["Chile"] and page.inner_text("#amap-search-count") == "1 account in 1 country"
        _type(page, "kld bfl")                                                     # слова — через И
        assert _visible_countries(page) == ["Chile", "Peru"] and page.inner_text("#amap-search-count") == "2 accounts in 2 countries"
    finally:
        page.close()


def test_matching_country_opens_without_touching_the_remembered_state(browser):
    page = _page(browser)
    try:
        page.evaluate("() => localStorage.setItem('acl.amap.open', JSON.stringify(['Kuwait']))")
        page.evaluate("() => { document.querySelector('[data-country=Kuwait]').open = true; }")
        page.wait_for_timeout(100)
        _type(page, "b88")
        assert page.evaluate("() => document.querySelector('[data-country=Peru]').open") is True
        page.wait_for_timeout(100)
        assert page.evaluate("() => localStorage.getItem('acl.amap.open')") == '["Kuwait"]'
        page.focus("#amap-search-all")
        page.keyboard.press("Escape")                                              # Esc очищает и возвращает прежнее состояние
        page.wait_for_timeout(200)
        assert page.input_value("#amap-search-all") == ""
        assert _visible_countries(page) == ["Chile", "Peru", "Kuwait"]
        assert page.evaluate("() => document.querySelector('[data-country=Peru]').open") is False
        assert page.evaluate("() => document.querySelector('[data-country=Kuwait]').open") is True
        assert page.evaluate("() => localStorage.getItem('acl.amap.open')") == '["Kuwait"]'
        assert page.inner_text("#amap-search-count") == ""
    finally:
        page.close()


def test_no_match_shows_the_empty_state_and_hides_every_country(browser):
    page = _page(browser)
    try:
        _type(page, "zzz")
        assert _visible_countries(page) == []
        assert page.is_visible("#amap-search-empty") and page.inner_text("#amap-search-empty") == "No account matches “zzz”."
        _type(page, "")
        assert page.is_hidden("#amap-search-empty") and len(_visible_countries(page)) == 3
    finally:
        page.close()


def test_the_account_child_row_hides_with_its_account(browser):
    page = _page(browser)
    try:
        page.evaluate("""() => {
          const tr = document.querySelector('[data-country=Chile] tr[data-search]');
          tr.classList.add('amap-open');
          const child = document.createElement('tr'); child.className = 'hc-acct-child'; child.innerHTML = '<td>tree</td>';
          tr.after(child);
        }""")
        _type(page, "5265")
        assert page.evaluate("() => document.querySelector('.hc-acct-child').hidden") is True
        _type(page, "b95")
        assert page.evaluate("() => document.querySelector('.hc-acct-child').hidden") is False
    finally:
        page.close()


@pytest.mark.parametrize("width", [1280, 768, 390])
def test_the_field_fits_the_width_without_page_overflow(browser, width):
    page = _page(browser, width)
    try:
        box = page.locator(".amap-search").bounding_box()
        assert box["x"] >= 0 and box["x"] + box["width"] <= width and box["height"] == pytest.approx(36, abs=1)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    finally:
        page.close()
