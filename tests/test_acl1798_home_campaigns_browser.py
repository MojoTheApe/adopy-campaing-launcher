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
from datetime import datetime, timezone
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
from src.analysis_agent import account_refresh as refresh  # noqa: E402
from src.analysis_agent.account_refresh_state import RefreshStore  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from tests._retry_once import goto, launch_chromium  # noqa: E402
from tests.test_acl1798_campaign_inventory import ad, adset, camp, snapshot  # noqa: E402

D = "2026-10-08"
REPORT = (ROOT / "src" / "web" / "templates" / "report.html").read_text(encoding="utf-8")
#: Монтирование таблицы страны и кнопка «Read campaigns in <Country>» — из настоящего шаблона страницы.
MOUNT_JS = REPORT[REPORT.index("function amapCampaignSlug("): REPORT.index("function amapPollSpend(")]
ACCOUNTS = ["Acc", "Fresh"]
#: Настоящие правила карты аккаунтов из report.html: проверяем, что они не протекают во вложенные таблицы кампаний.
AMAP_CSS = REPORT[REPORT.index("    .amap table { width: 100%"): REPORT.index("    .amap-caret-off {")]


def account_row(account):
    """Current production cell shape: the list text has its own child span."""
    return (f'<tr><td class="amap-acc"><span class="amap-caret-off">·</span>{account}</td>'
            f'<td class="amap-list" data-account="{account}"><span class="nodata">list not read</span></td>'
            '<td class="amap-actions"><button type="button" class="amap-x">Remove mapping</button></td></tr>')


class Site:
    """Страница-стенд: HTML шаблона и маршруты приложения; запросы записываются."""

    def __init__(self, web, monkeypatch):
        self.web, self.requests, self.fail_children, self.reads = web, [], 0, []
        self.warm, self.warm_posts = None, []
        self.admissions = []
        monkeypatch.setenv('ACL_AUTO_REFRESH', '0')
        monkeypatch.setattr(refresh, 'dashboard_accounts', lambda: list(ACCOUNTS))
        monkeypatch.setattr(web, '_mlx_batch_refusal', lambda *args: None)

        def unexpected_read(account, trigger):
            self.reads.append((account, trigger))
            raise AssertionError('Cached component tests must not open a profile')

        self.q = refresh.RefreshQueue(unexpected_read,
            store=RefreshStore(Path(ci._store_path()).parent / 'refresh'),
            accounts=refresh.dashboard_accounts)
        # Explicit boundary: exercise real local admission/state routes without
        # starting an external worker. The scheduled-worker scenarios live in2063.
        monkeypatch.setattr(self.q, 'start', lambda: None)
        monkeypatch.setattr(refresh, '_QUEUE', self.q)
        self.client = TestClient(web.app, base_url='http://127.0.0.1:9095')
        monkeypatch.setattr(web, "_inventory_bi", lambda a, c, f, t, s: ({"campaigns": {}, "ads": {}}, ""))
        # цель и бенчмарк — из настроек машины; в тесте их нет, и таблица должна сказать об этом тире с причиной
        monkeypatch.setattr(web, "_inventory_goal_context", lambda c: {
            "country": "Kuwait", "goal_cpa": None, "benchmark_cpa": None,
            "goal_note": "No CPA goal is set for Kuwait in My Goals", "benchmark_note": "No benchmark CPA is set for Kuwait"})

    def html(self, theme="light"):
        from src.web.app import templates

        comp = templates.env.get_template("_campaign_table.html").render()
        rows = "".join(account_row(a) for a in ACCOUNTS)
        return (f"<!doctype html><html data-theme='{theme}'><head><meta charset='utf-8'><style>:root {{--muted:#606c80; --border:#dfe3ea; --bg-subtle:#f7f8fa; --bg-hover:#eef0f4; --text:#202a39; --accent:#2563eb; --bad:#b42318; --good:#216e4e; --warn:#855812; --field-border:#c6cfdc;}}"
                ":root[data-theme='dark'] {{--muted:#a4b3c8; --border:#354459; --bg-subtle:#202b39; --bg-hover:#263446; --text:#edf2fa; --bad:#ffb4aa; --good:#8ad1ad; --warn:#e5bd78; --field-border:#4b5e76;}}"
                "body {{background: var(--bg-subtle); color: var(--text); font-family: system-ui, sans-serif;}}"
                + AMAP_CSS +
                "</style></head><body style='margin:24px'>" + comp +
                '<div class="amap"><details class="amap-country" data-country="Kuwait" open>'
                '<section class="hc hc-scoped amap-campaigns" data-country="Kuwait"><nav class="hc-tabs" data-hc-tabs></nav>'
                '<div class="hc-mount" data-hc-mount></div></section>'
                f'<table class="amap-grid"><thead><tr><th>Account</th><th>Campaign list</th><th></th></tr></thead><tbody>{rows}</tbody></table>'
                '<section class="hc hc-after" data-hc-after></section>'
                "</details></div>"
                "<script>window.AMAP_CAMPAIGN_TABLE = true; function amapPeriod() { return {from: '" + D + "', to: '" + D + "', span: ''}; }\n" + MOUNT_JS + "</script>"
                "</body></html>")

    def handle(self, route):
        req = route.request
        u = urlparse(req.url)
        self.requests.append((req.method, u.path))
        if u.path == "/":
            return route.fulfill(status=200, content_type="text/html", body=self.html())
        if u.path.startswith('/static/'):
            asset = (ROOT / 'src/web' / u.path.lstrip('/')).resolve()
            if asset.is_relative_to(ROOT / 'src/web/static') and asset.is_file():
                return route.fulfill(content_type='application/javascript', body=asset.read_bytes())
        if u.path in {'/account-refresh/page-open', '/account-refresh/request', '/account-refresh/state'}:
            assert req.method == ('GET' if u.path.endswith('/state') else 'POST')
            response = self.client.request(req.method, u.path,
                content=req.post_data_buffer,
                headers={'Content-Type': req.headers.get('content-type', '')})
            self.admissions.append((req.method, u.path, response.status_code))
            return route.fulfill(status=response.status_code,
                content_type='application/json', body=response.content)
        if u.path.startswith("/warm/") and getattr(self, "warm", None):
            W = self.warm
            if u.path == "/warm/state":
                body = {"available": True, "actions": True, "accounts": W["accounts"]}
            elif u.path == "/warm/open":
                self.warm_posts.append(req.post_data)
                return route.fulfill(status=202, content_type="application/json", body=json.dumps({"job_id": "j1", "state": "running"}))
            else:
                body = {"state": "done", "results": W["results"]}
            return route.fulfill(status=200, content_type="application/json", body=json.dumps(body))
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


TABLE = '.hc-table'


def row_of(account):
    return f'tr:has(td[data-account="{account}"])'


def open_acc(pg, account):
    """Раскрыть аккаунт кликом по самой строке (не по кнопке): под ней появляется таблица его кампаний."""
    pg.click(f'{row_of(account)} td.amap-acc', position={"x": 8, "y": 8})
    pg.wait_for_selector(f'{row_of(account)} + tr.hc-acct-child:not([hidden])')



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
        open_acc(pg, "Acc")
        pg.wait_for_selector(TABLE + " tbody tr", timeout=10000)
        yield pg
        browser.close()


def test_one_shared_scroll_sticky_header_and_no_page_overflow(page):
    page.select_option("[data-hc=preset]", "all")
    info = page.evaluate("""() => {
      const sc = document.querySelector('.hc-scroll'), t = document.querySelector('.hc-table');
      const inner = [...t.querySelectorAll('*')].filter(e => ['auto','scroll'].includes(getComputedStyle(e).overflowX) && e !== sc);
      return {doc: document.documentElement.scrollWidth, win: innerWidth, scrollW: sc.scrollWidth, clientW: sc.clientWidth,
              inner: inner.length, headPos: getComputedStyle(t.querySelector('thead th')).position,
              pinned: getComputedStyle(t.querySelector('tbody .p3')).position};
    }""")
    assert info["doc"] <= info["win"], "the page itself must not overflow horizontally"
    assert info["scrollW"] > info["clientW"] and info["inner"] == 0
    assert info["headPos"] == "sticky" and info["pinned"] == "sticky"
    # верхняя полоса двигает тот же контейнер
    page.evaluate("document.querySelector('.hc-top').scrollLeft = 300")
    page.wait_for_timeout(100)
    assert page.evaluate("document.querySelector('.hc-scroll').scrollLeft") == 300
    assert page.errors == []


def test_expand_shows_children_and_columns_stay_aligned(page):
    before = page.evaluate("[...document.querySelectorAll('.hc-table thead tr.hc-heads th')].map(t => Math.round(t.getBoundingClientRect().left))")
    page.click('[data-exp="campaign:1"]')
    page.wait_for_selector('tr[data-key="adset:11"]')
    assert page.locator('tr[data-key="adset:12"]').count() == 1
    page.click('[data-exp="adset:11"]')
    page.wait_for_selector('tr[data-key="ad:111"]')
    after = page.evaluate("[...document.querySelectorAll('.hc-table thead tr.hc-heads th')].map(t => Math.round(t.getBoundingClientRect().left))")
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
    open_acc(page, "Acc")
    page.wait_for_selector(".hc-table tbody tr")
    page.click('[data-exp="campaign:1"]')
    page.wait_for_selector("text=were not read")
    assert "not the same as having none" in page.inner_text(".hc-table")


def test_unknown_values_are_dashes_not_zeros(page):
    page.select_option("[data-hc=preset]", "all")
    row = page.locator('tr[data-key="campaign:2"]')          # у кампании 2 нет строки Insights и нет BI
    cells = row.locator("td.num").all_inner_texts()
    assert cells and all(c.strip() == "—" for c in cells), cells
    assert "$0" not in row.inner_text()
    # у кампании 1 есть Insights: числа настоящие, а Frequency/Reach не суммируются из детей
    assert "5,000" not in page.locator('tr[data-key="campaign:1"]').inner_text()  # Meta metrics are no longer displayed


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
    posts_before = [r for r in site.requests if r[0] != 'GET' and r[1].startswith('/campaign-commands/')]
    page.locator(".hc-cmd").first.click(force=True)
    page.locator(".hc-switch").first.click(force=True)
    page.wait_for_timeout(200)
    assert [r for r in site.requests if r[0] != 'GET' and r[1].startswith('/campaign-commands/')] == posts_before == []
    assert not site.reads and not site.warm_posts


def test_column_preset_and_visibility_persist(page):
    page.select_option("[data-hc=preset]", "bi")
    assert page.locator(".hc-table thead th", has_text="CPA (FTD)").count() == 1
    assert page.locator(".hc-table thead th", has_text="Impressions").count() == 0
    page.reload()
    open_acc(page, "Acc")
    page.wait_for_selector(".hc-table tbody tr")
    assert page.input_value("[data-hc=preset]") == "bi"
    page.click("[data-hc=cols] > summary")
    page.uncheck('[data-col="b_cr"]')
    assert page.input_value("[data-hc=preset]") == "custom"
    page.reload()
    open_acc(page, "Acc")
    page.wait_for_selector(".hc-table tbody tr")
    assert page.locator(".hc-table thead th", has_text="CR lead").count() == 0
    assert page.input_value("[data-hc=preset]") == "custom"


def test_unread_account_queues_attention_once_without_a_manual_read_button(page, site):
    # Before attention there is genuinely no cached list. Expanding admits a
    # local queue request; the explicit fixture boundary leaves its worker off.
    assert ci.get_snapshot('Fresh') is None
    assert 'list not read' in page.inner_text(f'{row_of("Fresh")} .hc-snap')
    assert not [q for q in site.q.state()['queued'] if q['account'] == 'Fresh']
    open_acc(page, "Fresh")
    page.wait_for_function("document.querySelector('.amap-campaigns')._hc.state.running.Fresh === true")
    text = page.inner_text(f'{row_of("Fresh")} + tr.hc-acct-child')
    assert 'Reading campaigns for Fresh' in text and 'one profile at a time' in text
    assert "IN THE ACCOUNT" not in text.upper()
    assert page.inner_text(f'{row_of("Fresh")} .hc-snap') == 'updating'
    assert page.locator(f'{row_of("Fresh")} button.hc-rc').is_hidden()
    assert page.locator(f'{row_of("Acc")} button.hc-rc').is_hidden()
    page.click(f'{row_of("Fresh")} td.amap-acc', position={'x': 8, 'y': 8})
    open_acc(page, 'Fresh')
    page.evaluate("() => window.aclAccountRefresh.request('Fresh', 'attention')")
    assert [q for q in site.q.state()['queued'] if q['account'] == 'Fresh'] == [
        {'account': 'Fresh', 'trigger': 'attention'}]
    assert ci.get_snapshot('Fresh') is None and site.reads == [] and site.warm_posts == []
    assert not [r for r in site.requests if r[1] == '/campaign-inventory/read']
    assert page.locator("[data-read-country]").count() == 0


def test_one_tree_the_account_row_is_the_parent_and_there_is_no_separate_table(page):
    """Секции на Home и отдельной таблицы нет; под строкой аккаунта — её кампании и только они."""
    assert page.locator("#hc-root, #hc-title").count() == 0
    assert page.locator(".hc-table:visible").count() == 1                         # раскрыт один аккаунт — одна видимая таблица
    assert page.locator(".hc-table tr.hc-band").count() == 0                      # полос аккаунтов внутри таблицы нет
    accounts = page.evaluate("[...new Set([...document.querySelectorAll('.hc-table tbody tr[data-account]')].map(r => r.dataset.account))]")
    assert accounts == ["Acc"]
    assert page.evaluate("document.querySelector('tr.hc-acct-child').previousElementSibling.querySelector('td.amap-list').dataset.account") == "Acc"
    assert page.get_attribute(f'{row_of("Acc")}', "aria-expanded") == "true" and page.get_attribute(f'{row_of("Fresh")}', "aria-expanded") == "false"
    tabs = page.locator(".hc-tabs .hc-tab").all_inner_texts()
    assert tabs == ["Campaigns", "Winning setups", "Landings", "Targets", "Creatives", "Buyers"]


def test_clicking_the_row_toggles_but_buttons_and_keys_behave(page, site):
    before = len(site.requests)
    acc = page.locator(f'{row_of("Acc")}')
    page.click(f'{row_of("Acc")} td.amap-acc', position={"x": 8, "y": 8})        # второй клик по строке сворачивает
    page.wait_for_selector(f'{row_of("Acc")} + tr.hc-acct-child[hidden]', state="attached")
    assert acc.get_attribute("aria-expanded") == "false"
    page.click(f'{row_of("Acc")} .amap-caret')                                   # стрелка раскрывает
    assert acc.get_attribute("aria-expanded") == "true"
    page.click(f'{row_of("Acc")} button.amap-x', force=True)                     # свои кнопки строки не раскрывают и не сворачивают
    assert acc.get_attribute("aria-expanded") == "true"
    acc.focus()
    page.keyboard.press("Enter")
    assert acc.get_attribute("aria-expanded") == "false"
    page.keyboard.press("Space")
    assert acc.get_attribute("aria-expanded") == "true"
    assert all(method == 'GET' or (method == 'POST' and path == '/account-refresh/request')
               for method, path in site.requests[before:])
    assert not site.reads and not site.warm_posts


def test_showing_the_block_never_reads_meta(page, site):
    page.wait_for_timeout(300)
    assert all(method == 'GET' or (method == 'POST' and path in
               {'/account-refresh/page-open', '/account-refresh/request'})
               for method, path in site.requests)
    assert not site.reads and not site.warm_posts
    assert not [r for r in site.requests if r[1] == "/campaign-inventory/read"]


def test_country_has_no_manual_bulk_read_control(page, site):
    from src.web.app import templates
    rendered = templates.env.get_template('_account_map_country.html').render(
        mc={'country':'Kuwait','key':'Kuwait','rows':[]},amap={'span':'all','countries':[]},mlx={})
    assert 'amapReadCampaigns(this)' not in rendered and 'amapReadAll(this)' not in rendered
    assert not [r for r in site.requests if r[1] == '/campaign-inventory/read']


def test_rpa_backend_shows_the_notice_and_does_nothing(site, monkeypatch):
    monkeypatch.delenv(api.BACKEND_ENV, raising=False)
    with sync_playwright() as pw:
        browser = launch_chromium(pw)
        pg = browser.new_page(viewport={"width": 1280, "height": 600})
        pg.route("http://acl.test/**", site.handle)
        goto(pg, "http://acl.test/")
        pg.wait_for_selector("[data-hc=body]:not([hidden])")
        open_acc(pg, "Acc")
        pg.wait_for_selector('.hc-table tbody tr')
        assert pg.locator('[data-hc=needs]').is_hidden()
        assert all(method == 'GET' or (method == 'POST' and path in
                   {'/account-refresh/page-open', '/account-refresh/request'})
                   for method, path in site.requests)
        assert site.admissions and all(status == 409 for method, _, status in site.admissions if method == 'POST')
        assert site.q.state()['queued'] == [] and site.reads == []
        browser.close()


# ───────────────────────── ширина закреплённого блока (ACL-1800) ─────────────────────────

def _pinned(page):
    return page.evaluate("""() => {
      const t = document.querySelector('.hc-table');
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
    open_acc(page, "Acc")
    page.wait_for_selector(".hc-table tbody tr")
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


# ───────────────────────── сообщения под строкой (ACL-1800) ─────────────────────────

RAW_ERROR = ("Not sent: Facebook_5414_61579429945735 the profile could not be opened with automation: HTTPSConnectionPool(host='launcher.mlx.yt', "
             "port=45001): Max retries exceeded with url: /api/v2/profile/f/automation_type (Caused by NewConnectionError('<urllib3.connection."
             "HTTPSConnection object at 0x000001>: Failed to establish a new connection: [WinError 10061] No connection could be made because "
             "the target machine actively refused it'))")


def _inject_error(page):
    page.evaluate("""(raw) => {
      const inst = document.querySelector('.amap-campaigns')._hc;
      inst.state.notes['campaign:1'] = window.acmCommands
        ? window.acmCommands.explain(raw, 'bad', '') : {text: raw, raw: null, tone: 'bad'};
      inst.render();
    }""", RAW_ERROR)


def test_a_failed_command_is_one_compact_sub_row_and_never_blows_the_row_up(page, site):
    import os
    from pathlib import Path

    shots = os.environ.get("ACL_SHOTS_DIR", "")
    page.evaluate("document.querySelector('[data-hc=preset]').value = 'all'")
    page.add_script_tag(url="http://acl.test/static/acm_commands.js")
    page.wait_for_function("!!window.acmCommands")
    # Persist a real terminal attempt before attention. A recent failure must
    # stay failed until Retry; it cannot coexist with queued work for Fresh.
    stamp = datetime.now(timezone.utc)
    failed_at = stamp.astimezone().strftime('%Y-%m-%d %H:%M:%S')
    ci.put_result(ci.Snapshot(account='Fresh', state='failed', finished_at=failed_at,
        finished_at_utc=stamp.isoformat(), error=RAW_ERROR.replace('Not sent: ', '')))
    page.evaluate('() => window.aclAccountRefresh.load()')
    open_acc(page, "Fresh")
    page.wait_for_selector('tr[data-msg="acct:Fresh:read"]')
    assert not [q for q in site.q.state()['queued'] if q['account'] == 'Fresh']
    assert site.q.state()['running'] != 'Fresh'
    _inject_error(page)
    row = page.locator('tr[data-key="campaign:1"]')
    sub = page.locator('tr[data-msg="row:campaign:1"]')
    assert row.bounding_box()["height"] <= 90                                              # строка осталась обычной высоты
    assert sub.bounding_box()["height"] <= 40                                              # сообщение — одна строка
    status = row.locator("td.p2")
    assert "WinError" not in status.inner_text() and "Multilogin" not in status.inner_text()
    assert status.locator(".hc-mk.bad").count() == 1 and "Multilogin" in status.locator(".hc-mk").get_attribute("title")
    table_w = page.evaluate("document.querySelector('.hc-table').getBoundingClientRect().width")
    assert page.evaluate("document.querySelector('tr[data-msg=\"row:campaign:1\"] td').getBoundingClientRect().width") >= table_w - 2   # на всю ширину
    assert page.evaluate("document.querySelector('tr[data-msg=\"row:campaign:1\"]').previousElementSibling.dataset.key") == "campaign:1"
    text = sub.locator(".hc-rm-text")
    assert text.inner_text() == "Multilogin isn't running — start it and try again"
    assert page.evaluate("getComputedStyle(document.querySelector('.hc-rm-text')).whiteSpace") == "nowrap"
    assert page.locator(".hc-rm-raw").count() == 0
    if shots:
        for theme in ("light", "dark"):
            page.evaluate("t => document.documentElement.setAttribute('data-theme', t)", theme)
            page.locator(".amap-campaigns").screenshot(path=str(Path(shots) / f"row-error-collapsed-{theme}.png"))
    sub.locator(".hc-rm-btn").click()
    raw = page.locator(".hc-rm-raw")
    assert raw.count() == 1 and "WinError 10061" in raw.inner_text() and "launcher.mlx.yt" in raw.inner_text()
    assert page.evaluate("getComputedStyle(document.querySelector('.hc-rm-raw')).userSelect") == "text"
    assert row.bounding_box()["height"] <= 90                                              # раскрытые детали строку не растягивают
    assert page.evaluate("document.querySelector('.hc-rm-raw').getBoundingClientRect().height") <= 160
    assert page.locator('.hc-rm-btn[aria-expanded="true"]').count() == 1
    if shots:
        for theme in ("light", "dark"):
            page.evaluate("t => document.documentElement.setAttribute('data-theme', t)", theme)
            page.locator(".amap-campaigns").screenshot(path=str(Path(shots) / f"row-error-details-{theme}.png"))
    sub.locator(".hc-rm-btn").click()
    assert page.locator(".hc-rm-raw").count() == 0
    # сбой чтения аккаунта: одна строка в полосе и Details
    band = page.locator('tr[data-msg="acct:Fresh:read"]')
    assert f'The last read failed ({failed_at})' in band.inner_text() and band.bounding_box()["height"] <= 40
    assert "WinError" not in band.inner_text()
    band.locator(".hc-rm-btn").click()
    assert "WinError 10061" in page.locator('tr[data-msg="acct:Fresh:read"] .hc-rm-raw').inner_text()
    retry = page.locator(f'{row_of("Fresh")} button.hc-rc')
    assert retry.is_visible() and retry.is_enabled() and retry.inner_text() == 'Retry'
    assert page.locator(f'{row_of("Fresh")} button.hc-rc:visible').count() == 1
    assert ci.get_attempt('Fresh')['error'] == RAW_ERROR.replace('Not sent: ', '')
    assert site.reads == [] and site.warm_posts == []
    page.evaluate("t => document.documentElement.setAttribute('data-theme', t)", "light")
    assert page.errors == []


# ───────────────────────── выравнивание шапки таблицы страны (ACL-1800) ─────────────────────────

def test_the_toolbar_lines_up_and_the_expanded_tree_does_not_overflow_the_page(page):
    import os
    from pathlib import Path

    left = page.evaluate("""() => {
      const L = sel => { const e = document.querySelector(sel); return e ? Math.round(e.getBoundingClientRect().left * 10) / 10 : null; };
      const R = sel => { const e = document.querySelector(sel); return e ? Math.round(e.getBoundingClientRect().right * 10) / 10 : null; };
      const textLeft = sel => { const e = document.querySelector(sel); const r = document.createRange(); r.selectNodeContents(e); return Math.round(r.getBoundingClientRect().left * 10) / 10; };
      return {toolbar: L('.hc-toolbar'), tabs: textLeft('.hc-tabs .hc-tab'), section: L('.amap-campaigns'), grid: L('table.amap-grid'), gridR: R('table.amap-grid'),
              frame: L('.hc-frame'), frameR: R('.hc-frame'), doc: document.documentElement.scrollWidth, win: innerWidth};
    }""")
    assert abs(left["toolbar"] - left["tabs"]) <= 2 and left["toolbar"] - left["section"] >= 8
    assert left["frame"] >= left["grid"] - 1 and left["frameR"] <= left["gridR"] + 1      # таблица кампаний лежит внутри списка аккаунтов
    assert left["doc"] <= left["win"]
    assert page.evaluate("getComputedStyle(document.querySelector('.hc-table thead th')).position") == "sticky"
    page.select_option("[data-hc=preset]", "all")
    assert page.evaluate("const s = document.querySelector('.hc-scroll'); s.scrollWidth > s.clientWidth")   # прокрутка внутри раскрытого аккаунта
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    shots = os.environ.get("ACL_SHOTS_DIR", "")
    if shots:
        page.select_option("[data-hc=preset]", "optimization")
        open_acc(page, "Fresh")
        for theme in ("light", "dark"):
            page.evaluate("t => document.documentElement.setAttribute('data-theme', t)", theme)
            page.locator(".amap").screenshot(path=str(Path(shots) / f"account-tree-{theme}.png"))
        page.evaluate("t => document.documentElement.setAttribute('data-theme', t)", "light")


# ───────────────────────── прокрутка вправо: никаких наложений посередине (ACL-1800) ─────────────────────────

def _rgba(css):
    import re
    nums = [float(x) for x in re.findall(r"[\d.]+", css)]
    return (nums + [1.0])[:4] if len(nums) == 3 else nums[:4]


def test_scrolled_right_there_is_no_overlay_and_only_pinned_cells_are_sticky(page):
    import os
    from io import BytesIO
    from pathlib import Path

    from PIL import Image

    shots = os.environ.get("ACL_SHOTS_DIR", "")
    page.select_option("[data-hc=preset]", "all")
    page.mouse.move(2, 2)
    for theme in ("light", "dark"):
        page.evaluate("t => document.documentElement.setAttribute('data-theme', t)", theme)
        page.evaluate("const s = document.querySelector('.hc-scroll'); s.scrollLeft = s.scrollWidth")
        page.wait_for_timeout(250)
        info = page.evaluate("""() => {
          const sc = document.querySelector('.hc-scroll'), fr = document.querySelector('.hc-frame');
          const allowedPinned = e => e.matches('thead th, td.p0, td.p1, td.p2, td.p3');
          const sticky = [...sc.querySelectorAll('*')].filter(e => ['sticky', 'absolute', 'fixed'].includes(getComputedStyle(e).position));
          const other = sticky.filter(e => !allowedPinned(e) && !e.matches('.hc-band-in, .hc-rm-raw, .hc-resize, thead th > span'));
          const partial = sticky.filter(e => e.matches('.hc-band-in, .hc-rm-raw')).filter(e => e.getBoundingClientRect().width < sc.clientWidth - 2);
          const pinnedBg = [...sc.querySelectorAll('td.p0, td.p1, td.p2, td.p3, thead th.p0, thead th.p1, thead th.p2, thead th.p3, th.hc-pin-top')].map(e => getComputedStyle(e).backgroundColor);
          const cs = (pe) => { const c = getComputedStyle(fr, pe); return {content: c.content, position: c.position, events: c.pointerEvents, right: c.right}; };
          const pin = parseFloat(getComputedStyle(document.querySelector('.hc-scoped')).getPropertyValue('--hc-pin-w'));
          const row = document.querySelector('tr[data-key="campaign:1"]').getBoundingClientRect(), frr = fr.getBoundingClientRect();
          return {other: other.map(e => e.className || e.tagName), partial: partial.map(e => e.className), pinnedBg, before: cs('::before'), after: cs('::after'),
                  moreLeft: fr.dataset.moreLeft, moreRight: fr.dataset.moreRight, x: frr.left + pin + 7, y: row.top + 3};
        }""")
        assert info["other"] == [], info["other"]                      # sticky/absolute — только закреплённые ячейки, ручка и полосы на всю ширину
        assert info["partial"] == []                                   # никакой sticky-элемент не занимает часть области прокрутки
        assert info["pinnedBg"] and all(_rgba(c)[3] == 1.0 for c in info["pinnedBg"]), info["pinnedBg"]   # закреплённый блок непрозрачный
        assert info["before"]["content"] in ("none", "normal")         # левой тени-полосы больше нет
        assert info["moreLeft"] == "1" and info["moreRight"] == "0"    # в конце: слева есть что прятать, справа — нет
        if info["after"]["content"] not in ("none", "normal"):
            assert info["after"]["events"] == "none" and info["after"]["right"] == "0px"
        # пиксели под бывшей полосой — обычный фон ячейки
        cell_bg = page.evaluate("([x, y]) => { const t = document.elementFromPoint(x, y); const td = t && t.closest('td'); return td ? getComputedStyle(td).backgroundColor : ''; }",
                                [info["x"], info["y"]])
        assert cell_bg
        want = _rgba(cell_bg)[:3]
        def pixel(dx):
            png = page.screenshot(clip={"x": info["x"] + dx, "y": info["y"], "width": 1, "height": 1})
            return Image.open(BytesIO(png)).convert("RGB").getpixel((0, 0))
        assert any(all(abs(a - b) <= 2 for a, b in zip(pixel(dx), want)) for dx in (0, 3, 5)), (theme, want, pixel(0))
        if shots:
            page.locator(".amap-campaigns").screenshot(path=str(Path(shots) / f"scrolled-right-{theme}.png"))
    page.evaluate("t => document.documentElement.setAttribute('data-theme', t)", "light")


def test_profile_chip_and_open_profile_button_with_and_without_the_warm_api(site, monkeypatch):
    """Чип состояния и «Open profile» на строке аккаунта — только когда есть API тёплых сессий (ACL-1812); открытие — только по нажатию."""
    with sync_playwright() as pw:
        browser = launch_chromium(pw)
        for with_api in (False, True):
            site.warm = ({"accounts": {"Acc": {"state": "closed", "text": "Closed", "title": "Not running"},
                                       "Fresh": {"state": "by_hand", "text": "Opened by hand", "title": "by hand"}},
                          "results": {"Acc": {"state": "ok", "ok": True}}} if with_api else None)
            pg = browser.new_page(viewport={"width": 1280, "height": 900})
            pg.route("http://acl.test/**", site.handle)
            goto(pg, "http://acl.test/")
            pg.wait_for_selector(row_of("Acc"))
            pg.wait_for_timeout(500)
            if not with_api:
                assert pg.locator(".amap-pstate:visible").count() == 0 and pg.locator(".hc-ro:visible").count() == 0
                assert pg.locator(f'{row_of("Acc")} button.hc-rc').count() == 1
            else:
                assert pg.locator(f'{row_of("Acc")} .amap-pstate').inner_text() == "Closed"
                assert pg.locator(f'{row_of("Fresh")} .amap-pstate').inner_text() == "Opened by hand"
                assert pg.locator(f'{row_of("Fresh")} .hc-ro').is_disabled() and not pg.locator(f'{row_of("Acc")} .hc-ro').is_disabled()
                assert site.warm_posts == []                                  # само по себе ничего не открывается
                pg.click(f'{row_of("Acc")} .hc-ro')
                pg.wait_for_function("() => !document.querySelector('.hc-ro').textContent.includes('Opening')", timeout=15000)
                assert site.warm_posts == ['{"accounts":["Acc"]}']
                assert pg.get_attribute(row_of("Acc"), "aria-expanded") == "false"      # кнопка строку не раскрыла
            pg.close()
        browser.close()
