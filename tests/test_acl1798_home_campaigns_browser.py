"""ACL-1798 в браузере: настоящий шаблон, настоящие маршруты, фикстурные снимки.

Шаблон `_home_campaigns.html` отрисован приложением; запросы страницы идут в
настоящие обработчики `/campaign-inventory/*` (хранилище в tmp, книга подменена).
Meta и Multilogin не трогаются. Тест пропускается, если свободной памяти меньше 6 ГБ.
"""
from __future__ import annotations

import asyncio
import concurrent.futures as cf
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest
from starlette.datastructures import QueryParams

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

psutil = pytest.importorskip("psutil")
pytest.importorskip("playwright.sync_api")
if psutil.virtual_memory().available < 6 * 2**30:
    pytest.skip("browser tests need more than 6 GB of free RAM", allow_module_level=True)

from playwright.sync_api import sync_playwright  # noqa: E402

from src.analysis_agent import account_spend_api as api  # noqa: E402
from src.analysis_agent import campaign_inventory as ci  # noqa: E402
from tests._retry_once import goto, launch_chromium  # noqa: E402
from tests.test_acl1798_campaign_inventory import ad, adset, camp, snapshot  # noqa: E402

D = "2026-10-08"
AMAP = SimpleNamespace(countries=[
    SimpleNamespace(country="Kuwait", virtual=False, rows=[{"account": "Acc"}, {"account": "Fresh"}]),
])


class Site:
    """Страница-стенд: HTML шаблона и маршруты приложения; запросы записываются."""

    def __init__(self, web, monkeypatch):
        self.web, self.requests, self.fail_children = web, [], 0
        monkeypatch.setattr(web, "_inventory_bi", lambda a, c, f, t, s: ({"campaigns": {}, "ads": {}}, ""))

    def html(self, theme="light"):
        body = self.web.templates.env.get_template("_home_campaigns.html").render(
            amap=AMAP, pd={"date_from": D, "date_to": D, "span": ""})
        return f"<!doctype html><html data-theme='{theme}'><head><meta charset='utf-8'></head><body style='margin:24px'>{body}</body></html>"

    def handle(self, route):
        req = route.request
        u = urlparse(req.url)
        self.requests.append((req.method, u.path))
        if u.path == "/":
            return route.fulfill(status=200, content_type="text/html", body=self.html())
        w = self.web
        fn = {"/campaign-inventory/status": w.campaign_inventory_status,
              "/campaign-inventory/tree": w.campaign_inventory_tree,
              "/campaign-inventory/children": w.campaign_inventory_children}.get(u.path)
        if fn is None:
            return route.fulfill(status=404, body="{}")
        if u.path.endswith("children") and self.fail_children:
            self.fail_children -= 1
            return route.fulfill(status=500, content_type="application/json", body=json.dumps({"ok": False, "error": "boom"}))
        with cf.ThreadPoolExecutor(1) as ex:
            resp = ex.submit(lambda: asyncio.run(fn(SimpleNamespace(query_params=QueryParams(u.query))))).result()
        return route.fulfill(status=resp.status_code, content_type="application/json", body=resp.body.decode())


@pytest.fixture
def site(monkeypatch, tmp_path):
    from src.web import app as web

    monkeypatch.setenv(api.BACKEND_ENV, "api")
    monkeypatch.setattr(ci, "_store_path", lambda: tmp_path / "campaign_inventory.json")
    ci._RUNNING.clear()
    snap = snapshot()
    # у кампании 2 нет ad set в списке (прочитано, пусто); у кампании 1 — ad set 12 без объявлений
    snap.adsets = [a for a in snap.adsets if a["campaign_id"] != "2"]
    snap.ads = [a for a in snap.ads if a["adset_id"] != "21"]
    ci.put_result(snap)
    return Site(web, monkeypatch)


@pytest.fixture
def page(site):
    with sync_playwright() as pw:
        browser = launch_chromium(pw)
        pg = browser.new_page(viewport={"width": 1280, "height": 900})
        pg.route("http://acl.test/**", site.handle)
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.errors = errors
        goto(pg, "http://acl.test/")
        pg.wait_for_selector("#hc-table tbody tr", timeout=10000)
        yield pg
        browser.close()


def test_one_shared_scroll_sticky_header_and_no_page_overflow(page):
    page.select_option("#hc-preset", "all")
    info = page.evaluate("""() => {
      const sc = document.getElementById('hc-scroll'), t = document.getElementById('hc-table');
      const inner = [...t.querySelectorAll('*')].filter(e => ['auto','scroll'].includes(getComputedStyle(e).overflowX) && e !== sc);
      return {doc: document.documentElement.scrollWidth, win: innerWidth, scrollW: sc.scrollWidth, clientW: sc.clientWidth,
              inner: inner.length, headPos: getComputedStyle(t.querySelector('thead th')).position,
              pinned: getComputedStyle(t.querySelector('tbody .p3')).position};
    }""")
    assert info["doc"] <= info["win"], "the page itself must not overflow horizontally"
    assert info["scrollW"] > info["clientW"] and info["inner"] == 0
    assert info["headPos"] == "sticky" and info["pinned"] == "sticky"
    # верхняя полоса двигает тот же контейнер
    page.evaluate("document.getElementById('hc-top').scrollLeft = 300")
    page.wait_for_timeout(100)
    assert page.evaluate("document.getElementById('hc-scroll').scrollLeft") == 300
    assert page.errors == []


def test_expand_shows_children_and_columns_stay_aligned(page):
    before = page.evaluate("[...document.querySelectorAll('#hc-table thead tr.hc-heads th')].map(t => Math.round(t.getBoundingClientRect().left))")
    page.click('[data-exp="campaign:1"]')
    page.wait_for_selector('tr[data-key="adset:11"]')
    assert page.locator('tr[data-key="adset:12"]').count() == 1
    page.click('[data-exp="adset:11"]')
    page.wait_for_selector('tr[data-key="ad:111"]')
    after = page.evaluate("[...document.querySelectorAll('#hc-table thead tr.hc-heads th')].map(t => Math.round(t.getBoundingClientRect().left))")
    assert before == after
    # у ячеек всех уровней одни и те же колонки: левые края метрик строки объявления и кампании совпадают
    lefts = page.evaluate("""() => ['campaign:1','adset:11','ad:111'].map(k =>
        [...document.querySelector('tr[data-key="'+k+'"]').children].map(td => Math.round(td.getBoundingClientRect().left)).join(','))""")
    assert len(set(lefts)) == 1
    # ad-set 12 прочитан, объявлений нет: «пусто», а не «не прочитано»
    page.click('[data-exp="adset:12"]')
    page.wait_for_selector("text=No ads in the saved inventory")
    assert not page.locator("text=were not read").count()


def test_empty_vs_failed_children_and_retry(page, site):
    page.click('[data-exp="campaign:2"]')
    page.wait_for_selector("text=No ad sets in the saved inventory")
    site.fail_children = 1
    page.click('[data-exp="campaign:1"]')
    page.wait_for_selector("text=Could not load ad sets: boom")
    page.click("[data-retry]")
    page.wait_for_selector('tr[data-key="adset:11"]')
    assert page.locator("text=Could not load").count() == 0


def test_partial_not_read_states_are_distinct(page, site, monkeypatch):
    snap = ci.get_snapshot("Acc")
    snap.steps["adsets"] = "failed"
    snap.problems = ["adsets were not read: Meta rate limit reached"]
    ci.put_result(snap)
    page.reload()
    page.wait_for_selector("#hc-table tbody tr")
    page.click('[data-exp="campaign:1"]')
    page.wait_for_selector("text=were not read")
    assert "not the same as having none" in page.inner_text("#hc-table")


def test_unknown_values_are_dashes_not_zeros(page):
    page.select_option("#hc-preset", "all")
    row = page.locator('tr[data-key="campaign:2"]')          # у кампании 2 нет строки Insights и нет BI
    cells = row.locator("td.num").all_inner_texts()
    assert cells and all(c.strip() == "—" for c in cells), cells
    assert "$0" not in row.inner_text()
    # у кампании 1 есть Insights: числа настоящие, а Frequency/Reach не суммируются из детей
    assert "5,000" in page.locator('tr[data-key="campaign:1"]').inner_text()


def test_commands_are_disabled_with_reason_and_send_nothing(page, site):
    page.select_option("#hc-preset", "all")
    page.click('[data-exp="campaign:1"]')
    page.wait_for_selector('tr[data-key="adset:11"]')
    info = page.evaluate("""() => [...document.querySelectorAll('.hc-cmd, .hc-switch')].map(b => ({
        disabled: b.disabled, name: b.getAttribute('name'), title: b.title, id: b.dataset.objectId, level: b.dataset.level,
        account: b.dataset.account, action: b.dataset.action}))""")
    assert len(info) > 20
    for b in info:
        assert b["disabled"] is True and b["name"] is None
        assert b["title"] in ("Coming in ACL-1800", "ACL-1773")
        assert b["id"] and b["level"] and b["account"] and b["action"]
    assert {b["title"] for b in info if b["action"] == "copy"} == {"ACL-1773"}
    posts_before = len([r for r in site.requests if r[0] != "GET"])
    page.locator(".hc-cmd").first.click(force=True)
    page.locator(".hc-switch").first.click(force=True)
    page.wait_for_timeout(200)
    assert len([r for r in site.requests if r[0] != "GET"]) == posts_before == 0


def test_column_preset_and_visibility_persist(page):
    page.select_option("#hc-preset", "bi")
    assert page.locator("#hc-table thead th", has_text="CPA (FTD)").count() == 1
    assert page.locator("#hc-table thead th", has_text="Impressions").count() == 0
    page.reload()
    page.wait_for_selector("#hc-table tbody tr")
    assert page.input_value("#hc-preset") == "bi"
    page.click("#hc-cols > summary")
    page.uncheck('[data-col="b_cr"]')
    assert page.input_value("#hc-preset") == "custom"
    page.reload()
    page.wait_for_selector("#hc-table tbody tr")
    assert page.locator("#hc-table thead th", has_text="CR lead").count() == 0
    assert page.input_value("#hc-preset") == "custom"


def test_unread_account_says_so_and_has_its_own_read_button(page):
    text = page.inner_text("#hc-table")
    assert "Not read yet" in text
    assert page.locator('[data-read="Fresh"]').count() == 1 and page.locator('[data-read-country="Kuwait"]').count() == 1


def test_rpa_backend_shows_the_notice_and_does_nothing(site, monkeypatch):
    monkeypatch.delenv(api.BACKEND_ENV, raising=False)
    with sync_playwright() as pw:
        browser = launch_chromium(pw)
        pg = browser.new_page(viewport={"width": 1280, "height": 600})
        pg.route("http://acl.test/**", site.handle)
        goto(pg, "http://acl.test/")
        pg.wait_for_selector("#hc-needs:not([hidden])")
        assert "Campaign inventory needs the API backend" in pg.inner_text("#hc-needs")
        assert not pg.is_visible("#hc-body")
        pg.wait_for_timeout(300)
        assert [r for r in site.requests if r[1].startswith("/campaign-inventory")] == [("GET", "/campaign-inventory/status")]
        browser.close()
