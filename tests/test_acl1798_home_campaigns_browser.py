"""ACL-1798 в браузере: настоящий шаблон, настоящие маршруты, фикстурные снимки.

Компонент `_campaign_table.html` отрисован приложением и стоит в блоке страны под списком
аккаунтов (ACL-1800: отдельной секции на Home нет, одна таблица на страну); функции
монтирования берутся из `report.html` как есть. Запросы страницы идут в настоящие обработчики
`/campaign-inventory/*` (хранилище в tmp, книга подменена).
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
REPORT = (ROOT / "src" / "web" / "templates" / "report.html").read_text(encoding="utf-8")
#: Монтирование таблицы страны и кнопка «Read campaigns in <Country>» — из настоящего шаблона страницы.
MOUNT_JS = REPORT[REPORT.index("function amapCampaignSlug("): REPORT.index("function amapPollSpend(")]
ACCOUNTS = ["Acc", "Fresh"]


def account_row(account):
    """Строка списка аккаунтов страны (ячейка с аккаунтом, спенд, кнопка чтения)."""
    return (f'<tr><td class="amap-acc"><span class="amap-caret-off">·</span>{account}</td>'
            f'<td class="num amap-live" data-account="{account}">$0.00</td><td><button class="amap-btn amap-read">Read spend</button></td></tr>')


class Site:
    """Страница-стенд: HTML шаблона и маршруты приложения; запросы записываются."""

    def __init__(self, web, monkeypatch):
        self.web, self.requests, self.fail_children, self.reads = web, [], 0, []
        monkeypatch.setattr(web, "_inventory_bi", lambda a, c, f, t, s: ({"campaigns": {}, "ads": {}}, ""))
        # цель и бенчмарк — из настроек машины; в тесте их нет, и таблица должна сказать об этом тире с причиной
        monkeypatch.setattr(web, "_inventory_goal_context", lambda c: {
            "country": "Kuwait", "goal_cpa": None, "benchmark_cpa": None,
            "goal_note": "No CPA goal is set for Kuwait in My Goals", "benchmark_note": "No benchmark CPA is set for Kuwait"})

    def html(self, theme="light"):
        from src.web.app import templates

        comp = templates.env.get_template("_campaign_table.html").render()
        rows = "".join(account_row(a) for a in ACCOUNTS)
        return (f"<!doctype html><html data-theme='{theme}'><head><meta charset='utf-8'><style>"
                ".amap table.amap-grid td, .amap table.amap-grid th {overflow: hidden; text-overflow: ellipsis; white-space: nowrap;}"
                ".amap td.amap-acc {white-space: normal;}"
                "</style></head><body style='margin:24px'>" + comp +
                '<div class="amap"><details class="amap-country" data-country="Kuwait" open>'
                '<div class="amap-toolbar"><button type="button" class="amap-btn amap-read-campaigns" onclick="amapReadCampaigns(this)">'
                'Read campaigns in Kuwait</button></div>'
                f'<table class="amap-grid"><tbody>{rows}</tbody></table>'
                '<section class="hc hc-scoped amap-campaigns" data-country="Kuwait"><nav class="hc-tabs" data-hc-tabs></nav>'
                '<div class="hc-mount" data-hc-mount></div></section>'
                "</details></div>"
                "<script>window.AMAP_CAMPAIGN_TABLE = true; function amapPeriod() { return {from: '" + D + "', to: '" + D + "', span: ''}; }\n" + MOUNT_JS + "</script>"
                "</body></html>")

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


TABLE = '[data-hc="table"]'


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
        pg.wait_for_selector(TABLE + " tbody tr", timeout=10000)
        yield pg
        browser.close()


def test_one_shared_scroll_sticky_header_and_no_page_overflow(page):
    page.select_option("[data-hc=preset]", "all")
    info = page.evaluate("""() => {
      const sc = document.querySelector('[data-hc=scroll]'), t = document.querySelector('[data-hc=table]');
      const inner = [...t.querySelectorAll('*')].filter(e => ['auto','scroll'].includes(getComputedStyle(e).overflowX) && e !== sc);
      return {doc: document.documentElement.scrollWidth, win: innerWidth, scrollW: sc.scrollWidth, clientW: sc.clientWidth,
              inner: inner.length, headPos: getComputedStyle(t.querySelector('thead th')).position,
              pinned: getComputedStyle(t.querySelector('tbody .p3')).position};
    }""")
    assert info["doc"] <= info["win"], "the page itself must not overflow horizontally"
    assert info["scrollW"] > info["clientW"] and info["inner"] == 0
    assert info["headPos"] == "sticky" and info["pinned"] == "sticky"
    # верхняя полоса двигает тот же контейнер
    page.evaluate("document.querySelector('[data-hc=top]').scrollLeft = 300")
    page.wait_for_timeout(100)
    assert page.evaluate("document.querySelector('[data-hc=scroll]').scrollLeft") == 300
    assert page.errors == []


def test_expand_shows_children_and_columns_stay_aligned(page):
    before = page.evaluate("[...document.querySelectorAll('[data-hc=table] thead tr.hc-heads th')].map(t => Math.round(t.getBoundingClientRect().left))")
    page.click('[data-exp="campaign:1"]')
    page.wait_for_selector('tr[data-key="adset:11"]')
    assert page.locator('tr[data-key="adset:12"]').count() == 1
    page.click('[data-exp="adset:11"]')
    page.wait_for_selector('tr[data-key="ad:111"]')
    after = page.evaluate("[...document.querySelectorAll('[data-hc=table] thead tr.hc-heads th')].map(t => Math.round(t.getBoundingClientRect().left))")
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
    page.wait_for_selector("[data-hc=table] tbody tr")
    page.click('[data-exp="campaign:1"]')
    page.wait_for_selector("text=were not read")
    assert "not the same as having none" in page.inner_text("[data-hc=table]")


def test_unknown_values_are_dashes_not_zeros(page):
    page.select_option("[data-hc=preset]", "all")
    row = page.locator('tr[data-key="campaign:2"]')          # у кампании 2 нет строки Insights и нет BI
    cells = row.locator("td.num").all_inner_texts()
    assert cells and all(c.strip() == "—" for c in cells), cells
    assert "$0" not in row.inner_text()
    # у кампании 1 есть Insights: числа настоящие, а Frequency/Reach не суммируются из детей
    assert "5,000" in page.locator('tr[data-key="campaign:1"]').inner_text()


def test_commands_are_disabled_with_reason_and_send_nothing(page, site):
    page.select_option("[data-hc=preset]", "all")
    page.click('[data-exp="campaign:1"]')
    page.wait_for_selector('tr[data-key="adset:11"]')
    info = page.evaluate("""() => [...document.querySelectorAll('.hc-cmd, .hc-switch')].map(b => ({
        disabled: b.disabled, name: b.getAttribute('name'), title: b.title, id: b.dataset.objectId, level: b.dataset.level,
        account: b.dataset.account, action: b.dataset.action}))""")
    assert len(info) > 20
    for b in info:
        assert b["disabled"] is True and b["name"] is None
        # без помощника команд (этот стенд его не отдаёт) команды выключены с причиной (ACL-1800)
        assert b["title"] in ("ACL-1773",) or b["title"].startswith("Commands unavailable"), b["title"]
        assert b["id"] and b["level"] and b["account"] and b["action"]
    assert {b["title"] for b in info if b["action"] == "copy"} == {"ACL-1773"}
    posts_before = len([r for r in site.requests if r[0] != "GET"])
    page.locator(".hc-cmd").first.click(force=True)
    page.locator(".hc-switch").first.click(force=True)
    page.wait_for_timeout(200)
    assert len([r for r in site.requests if r[0] != "GET"]) == posts_before == 0


def test_column_preset_and_visibility_persist(page):
    page.select_option("[data-hc=preset]", "bi")
    assert page.locator("[data-hc=table] thead th", has_text="CPA (FTD)").count() == 1
    assert page.locator("[data-hc=table] thead th", has_text="Impressions").count() == 0
    page.reload()
    page.wait_for_selector("[data-hc=table] tbody tr")
    assert page.input_value("[data-hc=preset]") == "bi"
    page.click("[data-hc=cols] > summary")
    page.uncheck('[data-col="b_cr"]')
    assert page.input_value("[data-hc=preset]") == "custom"
    page.reload()
    page.wait_for_selector("[data-hc=table] tbody tr")
    assert page.locator("[data-hc=table] thead th", has_text="CR lead").count() == 0
    assert page.input_value("[data-hc=preset]") == "custom"


def test_unread_account_says_so_and_has_its_own_read_button(page):
    text = page.inner_text(TABLE)
    assert "Not read yet" in text and "press Read campaigns" in text             # Fresh: снимка нет, прежний список не рисуется
    assert "IN THE ACCOUNT" not in text.upper()
    assert page.locator('[data-read="Fresh"]').count() == 1 and page.locator('[data-read="Acc"]').count() == 1
    assert page.locator("[data-read-country]").count() == 0                       # кнопка страны — в заголовке страны, не в таблице


def test_one_table_per_country_and_no_home_section(page):
    """Секции на Home нет; таблица одна, аккаунты страны стоят в ней полосами."""
    assert page.locator("#hc-root, #hc-title").count() == 0
    assert page.locator("[data-hc=table]").count() == 1
    assert page.locator("[data-hc=table] tr.hc-band").count() == 2
    accounts = page.evaluate("[...new Set([...document.querySelectorAll('[data-hc=table] tbody tr[data-account]')].map(r => r.dataset.account))]")
    assert accounts == ["Acc"]                                                    # у Fresh снимка нет — строк кампаний нет
    tabs = page.locator(".hc-tabs .hc-tab").all_inner_texts()
    assert tabs == ["Campaigns", "Winning setups", "Landings", "Targets", "Creatives", "Buyers"]


def test_showing_the_block_never_reads_meta(page, site):
    page.wait_for_timeout(300)
    assert [r for r in site.requests if r[0] != "GET"] == []
    assert not [r for r in site.requests if r[1] == "/campaign-inventory/read"]


def test_country_header_button_reads_every_account_one_by_one(page, site):
    page.route("http://acl.test/campaign-inventory/read", lambda route: (
        site.requests.append(("POST", "/campaign-inventory/read")),
        site.reads.append(route.request.post_data or ""),
        route.fulfill(status=200, content_type="application/json", body='{"ok": true}')))
    btn = page.locator(".amap-read-campaigns")
    assert btn.inner_text() == "Read campaigns in Kuwait"
    btn.click()
    for _ in range(40):                                                   # очередь: по одному, опрос статуса у своего сервера
        if len(site.reads) == 2 and not btn.is_disabled():
            break
        page.wait_for_timeout(500)
    assert len(site.reads) == 2
    assert "Acc" in site.reads[0] and "Fresh" in site.reads[1]            # по очереди, в порядке строк страны
    assert btn.inner_text() == "Read campaigns in Kuwait" and not btn.is_disabled()


def test_rpa_backend_shows_the_notice_and_does_nothing(site, monkeypatch):
    monkeypatch.delenv(api.BACKEND_ENV, raising=False)
    with sync_playwright() as pw:
        browser = launch_chromium(pw)
        pg = browser.new_page(viewport={"width": 1280, "height": 600})
        pg.route("http://acl.test/**", site.handle)
        goto(pg, "http://acl.test/")
        pg.wait_for_selector("[data-hc=needs]:not([hidden])")
        assert "Campaign inventory needs the API backend" in pg.inner_text("[data-hc=needs]")
        assert not pg.is_visible("[data-hc=body]")
        pg.wait_for_timeout(300)
        assert [r for r in site.requests if r[1].startswith("/campaign-inventory")] == [("GET", "/campaign-inventory/status")]
        browser.close()


# ───────────────────────── ширина закреплённого блока (ACL-1800) ─────────────────────────

def _pinned(page):
    return page.evaluate("""() => {
      const t = document.querySelector('[data-hc=table]');
      const w = e => Math.round(e.getBoundingClientRect().width);
      const rows = ['campaign:1', 'adset:11', 'ad:111'].map(k => t.querySelector('tr[data-key="' + k + '"]'));
      return {head: w(t.querySelector('thead tr.hc-heads th.p3')),
              names: rows.map(r => r && w(r.querySelector('td.p3'))),
              firstMetricLeft: Math.round(t.querySelector('thead tr.hc-heads th.p3 + th').getBoundingClientRect().left),
              rowMetricLefts: rows.map(r => r && Math.round(r.querySelector('td.p3 + td').getBoundingClientRect().left)),
              stored: localStorage.getItem('acl.hc.pinw.v1'),
              now: document.querySelector('.hc-resize').getAttribute('aria-valuenow')};
    }""")


def test_dragging_the_pinned_boundary_widens_every_row_and_is_remembered(page):
    page.evaluate("localStorage.removeItem('acl.hc.pinw.v1')")
    page.click('[data-exp="campaign:1"]')
    page.wait_for_selector('tr[data-key="adset:11"]')
    page.click('[data-exp="adset:11"]')
    page.wait_for_selector('tr[data-key="ad:111"]')
    before = _pinned(page)
    assert before["head"] == 280 and before["names"] == [280, 280, 280]
    box = page.locator(".hc-resize").bounding_box()
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x + 60, y, steps=4)
    page.mouse.move(x + 120, y, steps=4)
    assert _pinned(page)["stored"] is None                              # пока тянем — ничего не пишем
    page.mouse.up()
    after = _pinned(page)
    assert abs(after["head"] - (before["head"] + 120)) <= 2
    assert after["names"] == [after["head"]] * 3                       # шапка и все уровни — одна и та же ширина
    shift = after["firstMetricLeft"] - before["firstMetricLeft"]
    assert abs(shift - 120) <= 2 and after["rowMetricLefts"] == [after["firstMetricLeft"]] * 3   # колонки метрик строго под заголовками
    assert after["stored"] == str(432 + 120) and after["now"] == after["stored"]
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    # запомнено: после перезагрузки ширина та же
    page.reload()
    page.wait_for_selector("[data-hc=table] tbody tr")
    assert _pinned(page)["head"] == after["head"]
    # клавиатура и двойной щелчок
    page.focus(".hc-resize")
    page.keyboard.press("ArrowRight")
    assert int(page.get_attribute(".hc-resize", "aria-valuenow")) == 432 + 120 + 16
    page.keyboard.press("Home")
    assert _pinned(page)["head"] == 240 - 152
    page.dblclick(".hc-resize")
    assert _pinned(page)["head"] == 280 and _pinned(page)["stored"] == "432"
    assert page.errors == []


def test_names_stay_within_two_lines_and_carry_the_full_name_in_the_tooltip(page):
    info = page.evaluate("""() => {
      const b = document.querySelector('tr[data-key="campaign:1"] td.p3 .hc-name b');
      const cs = getComputedStyle(b);
      return {clamp: cs.webkitLineClamp || cs.lineClamp, title: b.title, text: b.textContent, h: Math.round(b.getBoundingClientRect().height), lh: parseFloat(cs.lineHeight)};
    }""")
    assert info["clamp"] == "2" and info["text"] in info["title"] and info["h"] <= info["lh"] * 2 + 2
