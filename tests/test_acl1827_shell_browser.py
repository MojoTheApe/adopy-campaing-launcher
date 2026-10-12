"""ACL-1827: боковая колонка, Home KPI и панель Robot health в настоящем (офлайн) Chromium.

Проверяется проводка: поиск страны, строки ассетов <-> чипы Workbench, все
прежние адреса по-прежнему открываются, панель Robot health открывается и
закрывается и не ломает страницу, нет горизонтальной прокрутки при 1280 px.
Запускать при свободной памяти больше 6 ГБ; живых Meta и Multilogin нет.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

pw_mod = pytest.importorskip("playwright.sync_api")

import ds_routes_harness as H  # noqa: E402
from test_acl1827_shell_parity import render  # noqa: E402

SUMMARY = {"window": 10, "n_runs": 14, "error": "", "accounts": [
    {"account": "Demo account A", "runs": 10, "clean": 7, "last_ok": False, "last_run": "r1", "last_when": "x",
     "last_failed_steps": ["ad_creative_upload"], "last_abort": ""},
    {"account": "Demo account B", "runs": 4, "clean": 4, "last_ok": True, "last_run": "r2", "last_when": "y",
     "last_failed_steps": [], "last_abort": ""}]}


@pytest.fixture(scope="module")
def browser():
    with pw_mod.sync_playwright() as pw:
        b = H.browser_launch(pw)
        yield b
        b.close()


@pytest.fixture(scope="module")
def html():
    return render()


def open_page(browser, html, theme="light", url="http://acl.test/"):
    ctx = browser.new_context(viewport={"width": 1280, "height": 900}, color_scheme=theme)
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.add_init_script(f"try {{ localStorage.setItem('acl.theme', '{theme}') }} catch (e) {{}}")
    page.route("**/*", H.make_route(html))
    page.route("**/robot-health/summary", lambda r: r.fulfill(json=SUMMARY))
    page.goto(url)
    return ctx, page, errors


def no_sideways_scroll(page):
    got = page.evaluate("({sw: document.documentElement.scrollWidth, iw: innerWidth})")
    assert got["sw"] <= got["iw"], got


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_home_shows_six_tiles_above_the_account_map_without_overflow(browser, html, theme):
    ctx, page, errors = open_page(browser, html, theme)
    try:
        tiles = page.locator("#home-kpis .hk-tile")
        assert tiles.count() == 6
        kpi_bottom = page.evaluate("document.getElementById('home-kpis').getBoundingClientRect().bottom")
        amap_top = page.evaluate("document.getElementById('amap-slot').getBoundingClientRect().top")
        assert kpi_bottom <= amap_top + 1
        no_sideways_scroll(page)
        assert not errors, errors
    finally:
        ctx.close()


def test_find_country_filters_the_list_and_restores_it(browser, html):
    ctx, page, errors = open_page(browser, html)
    try:
        visible = "Array.from(document.querySelectorAll('.sidebar-country')).filter(b => !b.hidden).map(b => b.dataset.name)"
        assert sorted(page.evaluate(visible)) == ["chile", "colombia"]
        page.fill("#sb-find-country", "CHI")
        assert page.evaluate(visible) == ["chile"]
        page.fill("#sb-find-country", "zzz")
        assert page.evaluate(visible) == [] and page.locator("#sb-find-empty").is_visible()
        page.fill("#sb-find-country", "")
        assert sorted(page.evaluate(visible)) == ["chile", "colombia"]
        assert not page.locator("#sb-find-empty").is_visible()
        assert not errors, errors
    finally:
        ctx.close()


def test_asset_rows_and_workbench_chips_follow_each_other(browser, html):
    ctx, page, errors = open_page(browser, html)
    try:
        page.click('.sidebar-country-btn[data-country="colombia"]')
        assert page.evaluate("document.querySelector('.view-panel.active').id") == "view-country-colombia-workbench"
        assert page.locator('.sb-asset[data-country="colombia"]').count() == 3
        page.click('.sb-asset[data-country="colombia"][data-asset="Alpha"]')
        assert page.evaluate("document.querySelector('.cw-panel.active .cw-chip[aria-pressed=true]').dataset.cwAsset") == "Alpha"
        assert "active" in page.get_attribute('.sb-asset[data-asset="Alpha"][data-country="colombia"]', "class")
        # выбор чипом в Workbench переносится в колонку
        page.click('.cw-panel.active .cw-chip[data-cw-asset="Beta"]')
        assert "active" in page.get_attribute('.sb-asset[data-asset="Beta"][data-country="colombia"]', "class")
        assert "active" not in page.get_attribute('.sb-asset[data-asset="Alpha"][data-country="colombia"]', "class")
        # строка ассета из другого вида возвращает в Workbench
        page.evaluate("document.querySelector('.sidebar-link[data-view=\"country-colombia-manage\"]').click()")
        page.click('.sb-asset[data-country="colombia"][data-asset=""]')
        assert page.evaluate("document.querySelector('.view-panel.active').id") == "view-country-colombia-workbench"
        assert not errors, errors
    finally:
        ctx.close()


def test_every_old_route_and_deep_link_still_opens(browser, html):
    ctx, page, errors = open_page(browser, html)
    try:
        views = page.evaluate("Array.from(document.querySelectorAll('.sidebar-link[data-view]')).map(l => l.dataset.view)")
        assert {"home", "targets", "ad-texts", "accounts", "country-colombia-workbench", "country-colombia-manage"} <= set(views)
        for v in views:
            H.show(page, v)
            assert page.evaluate("document.querySelector('.view-panel.active').id") == "view-" + v, v
        no_sideways_scroll(page)
        for deep in ("#targets", "#country-colombia-manage", "#country-colombia-workbench", "#country-kw-goals"):
            page.goto("http://acl.test/" + deep)
            page.reload()
            assert page.evaluate("document.querySelector('.view-panel.active') !== null"), deep
        page.goto("http://acl.test/#country-chile-manage")
        page.reload()
        assert page.evaluate("document.querySelector('.view-panel.active').id") == "view-country-chile-manage"
        assert not errors, errors
    finally:
        ctx.close()


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_robot_health_opens_as_a_popover_closes_and_the_page_keeps_working(browser, html, theme):
    ctx, page, errors = open_page(browser, html, theme)
    try:
        assert page.get_attribute("#robot-health-link", "href") == "/robot-health"
        assert not page.locator("#rh-pop").is_visible()
        page.click("#robot-health-link")
        page.wait_for_selector("#rh-pop .rh-row")
        assert page.url.rstrip("/").endswith("acl.test"), "клик перешёл на страницу вместо панели"
        assert page.get_attribute("#robot-health-link", "aria-expanded") == "true"
        text = page.inner_text("#rh-pop")
        assert "Demo account A" in text and "7 / 10 clean" in text and "ad_creative_upload" in text
        box = page.evaluate("(() => { const r = document.getElementById('rh-pop').getBoundingClientRect(); return [r.left, r.right, r.top]; })()")
        assert box[0] >= 0 and box[1] <= 1280, box
        no_sideways_scroll(page)
        # закрытие: кнопка, Escape, клик снаружи
        page.click("#rh-close")
        assert not page.locator("#rh-pop").is_visible()
        page.click("#robot-health-link")
        page.keyboard.press("Escape")
        assert not page.locator("#rh-pop").is_visible()
        page.click("#robot-health-link")
        page.mouse.click(300, 600)
        assert not page.locator("#rh-pop").is_visible()
        assert page.get_attribute("#robot-health-link", "aria-expanded") == "false"
        # страница после панели работает: колонка, тема, период
        H.show(page, "targets")
        assert page.evaluate("document.querySelector('.view-panel.active').id") == "view-targets"
        page.click("#period-btn")
        assert page.get_attribute("#period-btn", "aria-expanded") in ("true", "false")
        assert not errors, errors
    finally:
        ctx.close()


def test_the_popover_says_so_when_the_summary_cannot_be_read(browser, html):
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    page = ctx.new_page()
    try:
        page.route("**/*", H.make_route(html))
        page.route("**/robot-health/summary", lambda r: r.fulfill(status=500, body="boom"))
        page.goto("http://acl.test/")
        page.click("#robot-health-link")
        page.wait_for_selector("#rh-body .rh-msg")
        assert "could not be read" in page.inner_text("#rh-body")
    finally:
        ctx.close()
