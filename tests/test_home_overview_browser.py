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
    assert tile("Facebook_DQ-214")[2] == "1 FTD · CPA $117"          # есть FTD — вместо CPL строка про CPA
    assert tile("Facebook_DQ-213")[2].startswith("CPL ")              # FTD нет — остаётся CPL
    br = tile("Facebook_BR1")
    assert br[1] == "not in BM" and br[2].startswith("not in BM · ") and br[2].endswith("· not refreshed")   # ACL-2419 §4: слово статуса · время чтения · not refreshed
    dead = tile("Facebook_DQ-212")
    assert dead[1] == "disabled" and dead[2].startswith("$40 · disabled · ")
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


def test_filter_and_open_blocks_survive_period_swap(open_page):
    """Смена периода подменяет #amap-slot: чип, вид и раскрытые блоки остаются."""
    pg = open_page()
    pg.click('[data-aov-country="Chile"] [data-aov-more]')
    pg.click("[data-aov-filter=issue]")
    pg.evaluate("""() => {
      const slot = document.getElementById('amap-slot');
      slot.innerHTML = slot.innerHTML;
      document.dispatchEvent(new Event('htmx:afterSwap'));
    }""")
    pg.wait_for_function("document.querySelector('[data-aov-filter=issue]').getAttribute('aria-pressed') === 'true'")
    assert visible(pg, "tr.aov-row") == 6
    pg.click("[data-aov-filter=issue]")
    assert visible(pg, '[data-aov-country="Chile"] tr.aov-row') == 22
    assert not pg.errors


def test_total_bar_sums_country_book_totals(open_page):
    """Итог по всем странам строится из книжных тоталов стран, как и в старой строке Total."""
    pg = open_page()
    text = " ".join(pg.inner_text("#amap-totals").split())
    assert "Spend $0" not in text and "Spend $2" in text, text
    assert "Accounts 28" in text


def test_expanded_table_has_cpa_notes_and_campaign_cells(open_page, amap):
    """ACL-2384: вернулись CPA, заметка аккаунта и ячейка списка кампаний — те же, что в Manage."""
    amap["notes"] = {"Facebook_DQ-214": "call back on Monday"}
    pg = open_page()
    heads = pg.eval_on_selector_all('[data-aov-country="Colombia"] thead th', "els => els.map(e => e.textContent)")
    assert heads == ["State", "Account", "Spend", "Leads", "CPL", "FTDs", "CPA", "Notes"]
    cells = pg.eval_on_selector_all('[data-aov-country="Colombia"] tr.aov-row:first-child td', "els => els.map(e => e.textContent.trim())")
    assert "Facebook_DQ-214" in cells[1] and cells[2] == "$117" and cells[5] == "1" and cells[6] == "$117"
    assert pg.inner_text('[data-note-kind=account][data-account="Facebook_DQ-214"]') == "call back on Monday"
    assert pg.get_attribute('[data-note-kind=account][data-account="Facebook_DQ-214"]', "data-note") == "call back on Monday"
    # итог страны несёт CPA тем же правилом (spend / FTD)
    tot = pg.eval_on_selector('[data-aov-country="Colombia"] tr.tot', "e => e.textContent.replace(/\s+/g, ' ')")
    assert "$117" in tot
    assert not pg.errors


def test_campaign_tree_is_mounted_per_country_from_saved_data(open_page):
    """Дерево кампаний создаётся на видимых блоках и получает аккаунты страны; чтение с Home не запускается."""
    pg = open_page()
    mounts = pg.evaluate("window.__hc")
    by = {m["country"]: m for m in mounts}
    assert "Chile" in by and "Colombia" in by
    assert "Facebook_DQ-214" in by["Colombia"]["accounts"] and "Facebook_DQ-214" in by["Colombia"]["rows"]
    assert pg.locator('[data-aov-country="Colombia"] tr.aov-row').first.get_attribute("aria-expanded") == "false"
    assert pg.locator('[data-aov-country="Colombia"] .amap-caret').count() == 2
    # кнопки чтения дерева на Home спрятаны: открытие карты не касается аккаунтов
    assert pg.evaluate("[...document.querySelectorAll('#aov .hc-rc, #aov .hc-ro')].every(b => getComputedStyle(b).display === 'none')")
    # свёрнутый вид без монтирования: после переключения на Compact хосты других блоков не трогаются повторно
    n = len(mounts)
    pg.click("[data-aov-view=compact]")
    pg.click("[data-aov-view=expanded]")
    assert len(pg.evaluate("window.__hc")) == n


def test_ftd_and_cpa_reach_row_total_and_compact(open_page):
    """Строка с FTD показывает FTD и CPA в таблице, итоге страны и плитке (Chile B95: спенд делится на FTD)."""
    pg = open_page()
    assert pg.eval_on_selector('[data-aov-country="Colombia"] tr.tot', "e => [e.cells[4].textContent.trim(), e.cells[5].textContent.trim()]") == ["1", "$117"]
    pg.click("[data-aov-view=compact]")
    assert pg.get_attribute('.aov-tile[data-name="Facebook_DQ-214"]', "data-cpa") == "$117"
    pg.click('.aov-tile[data-name="Facebook_DQ-214"]')
    assert "CPA" in pg.inner_text("#aov-pop") and "$117" in pg.inner_text("#aov-pop")


def test_close_country_button_in_both_views(open_page):
    """ACL-2384: «Close» (retire country) вернулась в шапку страны и в Compact; зовёт тот же amapClose, пустой виртуальной страны нет."""
    pg = open_page()
    pg.evaluate("window.__closed = []; window.amapClose = function (k, v, l) { window.__closed.push([k, v, l]); }; 0")
    assert pg.locator('#aov-expanded [data-aov-country="Chile"] [data-aov-close]').get_attribute("title").startswith("Retire this country.")
    pg.click('#aov-expanded [data-aov-country="Costa Rica"] [data-aov-close]')
    assert pg.evaluate("window.__closed") == [["country", "Costa Rica", "Costa Rica"]]
    pg.click("[data-aov-view=compact]")
    pg.click('#aov-compact [data-aov-country="Brazil"] [data-aov-close]')
    assert pg.evaluate("window.__closed")[-1] == ["country", "Brazil", "Brazil"]


def test_account_cell_shows_act_id_state_tip_and_no_empty_strip(open_page, amap):
    """ACL-2384: имя и номер кабинета целиком; причина и чтение не выводятся; пустой полосы над шапкой нет."""
    for mc in amap["countries"]:
        for r in mc["rows_aov"]:
            r["act_id"] = "act_1234567890123456"
    pg = open_page()
    cell = pg.locator('#aov-expanded [data-aov-country="Argentina"] tr.aov-row', has_text="Facebook_DQ-212").locator("td.acc")
    assert "act_1234567890123456" in cell.inner_text()
    assert pg.evaluate("(e) => e.scrollWidth <= e.clientWidth + 1", cell.element_handle())
    st = pg.locator('#aov-expanded [data-aov-country="Argentina"] tr.aov-row', has_text="Facebook_DQ-212").locator(".aov-st")
    assert st.get_attribute("data-tip") is None and st.get_attribute("title") is None   # подсказки на State нет (решение владельца)
    # колонок What happened / Read / Campaigns нет
    heads = pg.eval_on_selector_all('#aov-expanded [data-aov-country="Chile"] thead th', "els => els.map(e => e.textContent)")
    assert not {"What happened", "Read", "Campaigns"} & set(heads)
    # хост дерева кампаний не рисуется пустой рамкой
    h = pg.evaluate("(() => { const e = document.querySelector('#aov-expanded .amap-campaigns'); const s = getComputedStyle(e); return [s.borderTopWidth, e.offsetHeight]; })()")
    assert h[0] == "0px" and h[1] < 4, h


def test_state_cell_shows_whole_status_line_in_both_views(open_page, amap):
    """ACL-2511: date, reason and not-refreshed suffix are readable in the State column (no clipping); tile titles carry the line."""
    long_line = "disabled · 10.11 10:00 · ads integrity policy violation · not refreshed"
    rows = [r for mc in amap["countries"] for r in mc["rows_aov"]]
    target = next(r for r in rows if r["state"]["key"] == "dead")
    target["state"]["status_line"] = long_line
    pg = open_page()
    row = pg.locator("#aov-expanded tr.aov-row", has_text=target["account"]).first
    line = row.locator(".aov-stline")
    assert line.inner_text() == long_line
    assert line.get_attribute("title") == long_line
    assert pg.evaluate("(e) => getComputedStyle(e).whiteSpace", line.element_handle()) == "normal"
    # nothing is cut: the text fits the cell horizontally and the cell grows to hold every wrapped line
    assert pg.evaluate(
        "(e) => { const td = e.closest('td'); const a = e.getBoundingClientRect(), b = td.getBoundingClientRect();"
        " return e.scrollWidth <= e.clientWidth + 1 && a.right <= b.right + 1 && a.bottom <= b.bottom + 1 && td.scrollHeight <= td.clientHeight + 1; }",
        line.element_handle())
    # every State line in the expanded view is fully inside its cell
    clipped = pg.evaluate(
        "[...document.querySelectorAll('#aov-expanded .aov-stline')].filter(e => { const td = e.closest('td');"
        " return td.getBoundingClientRect().width > 0 && (e.scrollWidth > e.clientWidth + 1 || td.scrollHeight > td.clientHeight + 1); }).length")
    assert clipped == 0
    pg.click("[data-aov-view=compact]")
    title = pg.evaluate("(n) => [...document.querySelectorAll('#aov-compact .aov-tile')].find(t => t.dataset.name === n).title", target["account"])
    assert long_line in title
    assert not pg.errors
