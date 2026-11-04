"""Home: оба вида, переключатель, фильтры, «Show more», поиск, поповер плитки (Playwright на настоящих шаблонах)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

pytest.importorskip("playwright.sync_api")
psutil = pytest.importorskip("psutil")
if psutil.virtual_memory().available < 2 * 2**30:
    pytest.skip("browser tests need more than 2 GB of free RAM", allow_module_level=True)

from playwright.sync_api import sync_playwright  # noqa: E402

from src.web import account_links as links  # noqa: E402
from tests import _home_overview_site as site  # noqa: E402
from tests._retry_once import launch_chromium  # noqa: E402

SHOTS = os.environ.get("AOV_SHOTS", "")


@pytest.fixture
def amap(monkeypatch, tmp_path):
    from src.web import app as web

    monkeypatch.setattr(links, "get_local_config_dir", lambda: str(tmp_path))
    monkeypatch.setattr(links, "get_ad_account_map_path", lambda: str(tmp_path / "nope.csv"))
    monkeypatch.setattr("src.web.app.get_ad_account_map_path", lambda: str(tmp_path / "nope.csv"))
    return site.build_amap(web, links, monkeypatch, site.standard_spec())


@pytest.fixture
def open_page(amap):
    with sync_playwright() as pw:
        browser = launch_chromium(pw)

        def make(width=1280, theme="light", storage=None):
            ctx = browser.new_context(viewport={"width": width, "height": 900})
            pg = ctx.new_page()
            errors = []
            pg.on("pageerror", lambda e: errors.append(str(e)))
            pg.errors = errors
            if storage:
                pg.add_init_script("; ".join(f"localStorage.setItem('{k}','{v}')" for k, v in storage.items()))
            html = site.page_html(amap, theme)
            pg.route("http://acl.test/**", lambda route: route.fulfill(status=200, content_type="text/html", body=html))
            pg.goto("http://acl.test/")
            return pg

        yield make
        browser.close()


def view(pg):
    return pg.evaluate("document.getElementById('aov').dataset.view")


def visible(pg, sel):
    return pg.evaluate("(s) => [...document.querySelectorAll(s)].filter(e => e.offsetParent !== null).length", sel)


def test_default_is_expanded_and_countries_with_active_first(open_page):
    pg = open_page()
    assert view(pg) == "expanded"
    names = pg.eval_on_selector_all("#aov-expanded .aov-bh > b", "els => els.map(e => e.textContent)")
    assert names[:3] == ["Chile", "Colombia", "Argentina"], names
    assert names.index("Brazil") > 2 and names.index("Costa Rica") > 2
    assert not pg.errors


def test_narrow_default_is_compact_and_explicit_choice_wins(open_page):
    assert view(open_page(width=390)) == "compact"
    assert view(open_page(width=390, storage={"acl.aov.view": "expanded"})) == "expanded"
    assert view(open_page(width=1280, storage={"acl.aov.view": "compact"})) == "compact"


def test_switch_is_next_to_search_persists_and_keeps_filter(open_page):
    pg = open_page()
    assert pg.evaluate("document.querySelector('.aov-view').previousElementSibling.classList.contains('amap-search')")
    pg.click("[data-aov-filter=issue]")
    pg.click("[data-aov-view=compact]")
    assert view(pg) == "compact"
    assert pg.evaluate("localStorage.getItem('acl.aov.view')") == "compact"
    assert pg.get_attribute("[data-aov-filter=issue]", "aria-pressed") == "true"
    assert pg.evaluate("document.querySelector('.aov-tile.active').classList.contains('dim')")
    assert not pg.evaluate("document.querySelector('.aov-tile.issue').classList.contains('dim')")
    pg.click("[data-aov-view=expanded]")
    assert pg.get_attribute("[data-aov-filter=issue]", "aria-pressed") == "true"
    pg.focus("[data-aov-view=expanded]")
    pg.keyboard.press("ArrowRight")
    assert view(pg) == "compact"


def test_filter_chip_hides_rows_and_second_click_clears(open_page):
    pg = open_page()
    total = visible(pg, "tr.aov-row")
    pg.click("[data-aov-filter=dead]")
    assert visible(pg, "tr.aov-row") == 3
    assert "Showing 3 dead accounts in 2 countries" in pg.inner_text("#aov-live")
    assert visible(pg, ".aov-block") == 2
    pg.click("[data-aov-filter=dead]")
    assert visible(pg, "tr.aov-row") >= total


def test_show_more_threshold_15_and_remembered(open_page):
    pg = open_page()
    assert visible(pg, '[data-aov-country="Chile"] tr.aov-row') == 15
    assert visible(pg, '[data-aov-country="Colombia"] tr.aov-row') == 2
    assert pg.inner_text('[data-aov-country="Chile"] [data-aov-more]').startswith("Show 7 more")
    pg.click('[data-aov-country="Chile"] [data-aov-more]')
    assert visible(pg, '[data-aov-country="Chile"] tr.aov-row') == 22
    assert "Chile" in pg.evaluate("localStorage.getItem('acl.aov.open')")
    assert pg.evaluate("localStorage.getItem('acl.amap.open')") is None
    pg.click('[data-aov-country="Chile"] [data-aov-more]')
    assert visible(pg, '[data-aov-country="Chile"] tr.aov-row') == 15
    pg.click("[data-aov-filter=unknown]")
    assert visible(pg, '[data-aov-country="Chile"] tr.aov-row') == 3
    assert "Chile" not in pg.evaluate("localStorage.getItem('acl.aov.open')")


def test_search_in_both_views_counts_once(open_page):
    pg = open_page()
    pg.fill("#amap-search-all", "dq-21")
    pg.wait_for_function("document.getElementById('amap-search-count').textContent.length > 0")
    assert pg.inner_text("#amap-search-count") == "4 accounts in 2 countries"
    assert visible(pg, ".aov-block") == 2
    pg.click("[data-aov-view=compact]")
    assert visible(pg, ".aov-crow") == 2 and visible(pg, ".aov-tile") == 4
    pg.fill("#amap-search-all", "dq-213")
    pg.wait_for_function("document.getElementById('amap-search-count').textContent.startsWith('1 account ')")
    pg.fill("#amap-search-all", "")
    pg.wait_for_function("document.getElementById('amap-search-count').textContent === ''")
    assert visible(pg, ".aov-crow") == 5


def test_tile_content_by_state(open_page):
    pg = open_page(storage={"acl.aov.view": "compact"})

    def tile(name):
        return pg.inner_text(f'.aov-tile[data-name="{name}"]').split("\n")

    assert tile("Facebook_DQ-214")[1].startswith("$117 · 12 leads")
    assert tile("Facebook_DQ-214")[2].startswith("CPL ")
    br = tile("Facebook_BR1")
    assert br[1] == "not in BM" and br[2].startswith("read ") and br[2].endswith("· 5d")
    dead = tile("Facebook_DQ-212")
    assert dead[1] == "disabled" and dead[2].startswith("$40 · read ")
    assert tile("Facebook_DQ-217")[1:] == ["New", "no spend yet"]
    assert tile("Facebook_CLU0")[1] == "Unknown"
    size = pg.evaluate("(() => { const r = document.querySelector('.aov-tile').getBoundingClientRect(); return [r.width, r.height]; })()")
    assert size == [128, 62]


def test_popover_open_close_and_manage(open_page):
    pg = open_page(storage={"acl.aov.view": "compact"})
    t = pg.locator('.aov-tile[data-name="Facebook_DQ-212"]')
    t.click()
    assert pg.is_visible("#aov-pop")
    text = pg.inner_text("#aov-pop")
    assert "Facebook_DQ-212" in text and "Argentina" in text and "account disabled" in text
    pg.keyboard.press("Escape")
    assert not pg.is_visible("#aov-pop")
    assert pg.evaluate("document.activeElement.dataset.name") == "Facebook_DQ-212"
    t.click()
    pg.mouse.click(5, 5)
    assert not pg.is_visible("#aov-pop")
    t.click()
    t.click()
    assert not pg.is_visible("#aov-pop")


def test_manage_button_uses_sidebar_view(open_page):
    pg = open_page()
    pg.click('[data-aov-country="Chile"] .aov-btn.q')
    assert pg.evaluate("window.__manage") == ["country-chile-manage"]


def test_empty_country_has_add_account(open_page):
    pg = open_page()
    assert "+ Add account" in pg.inner_text('[data-aov-country="Costa Rica"]')


@pytest.mark.parametrize("width", [390, 768, 1280])
@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("mode", ["expanded", "compact"])
def test_no_page_overflow_and_shots(open_page, width, theme, mode):
    pg = open_page(width=width, theme=theme, storage={"acl.aov.view": mode})
    assert pg.evaluate("document.documentElement.scrollWidth <= innerWidth"), (width, theme, mode)
    assert not pg.errors
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        pg.screenshot(path=str(Path(SHOTS) / f"home-{mode}-{theme}-{width}.png"), full_page=True)
