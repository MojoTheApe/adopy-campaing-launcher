"""Copy text for the account provider's WhatsApp group (acl-132).

Решение владельца 30.09: под плохим статусом аккаунта — кнопка Copy text,
копирующая одну строку «<имя до " - "> - Account <Status>», и подсказка «Copy
screenshot and account number to send in the group». Снимок копирует кнопка
из ACL-1430. Ничего не отправляется: человек вставляет и жмёт Send сам.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
TPL = ROOT / "src" / "web" / "templates"


def _render(account: str, status: dict) -> str:
    from src.analysis_agent.money_format import fmt_currency

    env = Environment(loader=FileSystemLoader(str(TPL)), autoescape=True)
    env.filters["fmt_currency"] = fmt_currency
    env.filters["fmt_usd"] = str
    country = {"country": "Kuwait", "key": "KW", "virtual": False,
               "rows": [{"account": account, "has_data": False, "ready": True,
                         "in_workbook": True, "status": status, "to_do": ""}]}
    html = env.get_template("_account_map_country.html").render(
        mc=country, mlx={},
        amap={"countries": [country], "available": [], "all_accounts": [], "live": {},
              "splits": {}, "fallback": {}, "dates": [], "date_from": "2026-09-01",
              "date_to": "2026-09-28", "span": ""})
    cell = html[html.index('<td class="amap-status">'):]
    return cell[: cell.index("</td>")]


@pytest.mark.parametrize("status,line", [
    ("disabled", "FacebookDoc20 - Account Disabled"),
    ("restricted", "FacebookDoc20 - Account Restricted"),
    ("blocked", "FacebookDoc20 - Account Blocked"),
])
def test_a_problem_status_offers_the_line_for_the_group(status, line):
    cell = _render("FacebookDoc20 - Mios Darwiti", {"status": status, "status_text": "x", "at": "2026-09-30 15:43:25", "at_short": "30.09"})
    assert f'data-copy="{line}"' in cell
    assert ">Copy text</button>" in cell
    assert "Copy screenshot and account number to send in the group" in cell


@pytest.mark.parametrize("status", ["clear", "unknown"])
def test_a_healthy_or_unread_status_offers_nothing(status):
    cell = _render("FacebookDoc20 - Mios Darwiti", {"status": status, "at": "2026-09-30 15:43:25", "at_short": "30.09"})
    assert "amap-copy-text" not in cell and "send in the group" not in cell


def test_a_name_without_a_dash_is_used_whole():
    cell = _render("Facebook_1443_61579302961692", {"status": "disabled", "at": "x", "at_short": "30.09"})
    assert 'data-copy="Facebook_1443_61579302961692 - Account Disabled"' in cell


def test_a_name_is_escaped_in_the_attribute():
    cell = _render('Evil"><b>x - Owner', {"status": "disabled", "at": "x", "at_short": "30.09"})
    assert "<b>x" not in cell, "имя аккаунта вставлено в разметку без экранирования"


def test_the_button_puts_the_line_on_the_clipboard():
    page_src = (TPL / "report.html").read_text(encoding="utf-8")
    fn = page_src[page_src.index("async function amapCopyText"):]
    fn = fn[: fn.index("\n}\n") + 2]
    cell = _render("FacebookDoc20 - Mios Darwiti", {"status": "disabled", "at": "x", "at_short": "30.09"})
    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=os.environ.get("ACL_BROWSER_EXECUTABLE") or None)
        ctx = b.new_context()
        p = ctx.new_page()
        p.route("**/*", lambda r: r.fulfill(content_type="text/html",
                body=f"<table><tr>{cell}</td></tr></table><script>window.__copied=null;"
                     "navigator.clipboard.writeText=async t=>{{window.__copied=t}};</script><script>" + fn + "</script>"))
        p.goto("http://localhost/")
        p.locator(".amap-copy-text").click()
        p.wait_for_function("window.__copied !== null")
        assert p.evaluate("window.__copied") == "FacebookDoc20 - Account Disabled"
        assert p.locator(".amap-copy-text").inner_text() == "Copied"
        b.close()


# ── ревью #465: снимка нет, живой Read, двойной клик, ничего не уходит ──

def test_without_a_screenshot_the_hint_points_at_a_disabled_button():
    """Issue #31: без снимка Copy screenshot выключена и говорит почему."""
    cell = _render("FacebookDoc20 - Mios Darwiti", {"status": "restricted", "at": "x", "at_short": "30.09",
                                                     "shot_id": "", "shot_at": ""})
    assert "Copy screenshot and account number to send in the group" in cell
    shot = re.search(r'<button[^>]*class="amap-shot"[^>]*>', cell)
    assert shot, "подсказка указывает на кнопку, которой нет"
    assert " disabled" in shot.group(0)
    assert 'title="No screenshot on the last read — read the account again"' in shot.group(0)


def _js(name: str) -> str:
    src = (TPL / "report.html").read_text(encoding="utf-8")
    start = src.index(name)
    return src[start: src.index("\n}\n", start) + 2]


def _page_with(pw, body: str, scripts: list[str]):
    """Страница без сети: любой другой запрос записывается и обрывается."""
    b = pw.chromium.launch(executable_path=os.environ.get("ACL_BROWSER_EXECUTABLE") or None)
    ctx = b.new_context()
    p = ctx.new_page()
    sent = []

    def serve(route):
        if route.request.url == "http://localhost/":
            route.fulfill(content_type="text/html; charset=utf-8", body=(
                body + "<script>window.__copied=[];"
                "navigator.clipboard.writeText=async t=>{window.__copied.push(t)};</script>"
                + "".join(f"<script>{s}</script>" for s in scripts)))
        else:
            sent.append(route.request.url)
            route.abort()

    p.route("**/*", serve)
    p.goto("http://localhost/")
    return b, ctx, p, sent


def test_a_second_click_does_not_leave_the_button_on_copied():
    cell = _render("FacebookDoc20 - Mios Darwiti", {"status": "disabled", "at": "x", "at_short": "30.09"})
    with sync_playwright() as pw:
        b, _ctx, p, _sent = _page_with(pw, f"<table><tr>{cell}</td></tr></table>",
                                       [_js("async function amapCopyText")])
        btn = p.locator(".amap-copy-text")
        btn.click()
        p.wait_for_function("window.__copied.length === 1")
        btn.click()
        p.wait_for_function("window.__copied.length === 2")
        assert btn.inner_text() == "Copied"
        p.wait_for_function("document.querySelector('.amap-copy-text').textContent === 'Copy text'",
                            timeout=4000)
        p.wait_for_timeout(1700)
        assert btn.inner_text() == "Copy text", "старый таймер вернул «Copied»"
        b.close()


def test_copy_text_sends_and_stores_nothing():
    """Issue #31: ничего не отправляется и не сохраняется — только буфер обмена."""
    cell = _render("FacebookDoc20 - Mios Darwiti", {"status": "blocked", "at": "x", "at_short": "30.09"})
    with sync_playwright() as pw:
        b, ctx, p, sent = _page_with(pw, f"<table><tr>{cell}</td></tr></table>",
                                     [_js("async function amapCopyText")])
        p.locator(".amap-copy-text").click()
        p.wait_for_function("window.__copied.length === 1")
        p.wait_for_timeout(300)
        assert p.evaluate("window.__copied") == ["FacebookDoc20 - Account Blocked"]
        assert sent == [], f"копирование ушло в сеть: {sent}"
        assert p.evaluate("localStorage.length + sessionStorage.length") == 0
        assert ctx.cookies() == []
        b.close()


_LIVE_ROW = (
    '<table><tr><td class="amap-live" data-account="FacebookDoc20 - Mios Darwiti"></td>'
    '<td class="amap-status"><span class="amap-st ok">no warning</span></td>'
    '<td><button class="amap-read">Read spend</button></td></tr></table>'
)
_LIVE_STUBS = (
    "var amapReadAllBusy=false;function amapMoney(v,c){return v+' '+c}function amapReadTitle(){return ''}"
    "function amapPaintTree(){}function amapUpdateCountrySummaries(){}function amapSpendFailed(){}"
    "function amapCopyShot(){}"
)


def _live(pw):
    return _page_with(pw, _LIVE_ROW, [
        _LIVE_STUBS, _js("async function amapCopyText"), _js("var AMAP_STATUS_WORDS"),
        _js("function amapApplyReading")])


@pytest.mark.parametrize("shot", ["status_Doc20__20261002-100000", ""])
def test_a_live_read_that_finds_a_problem_offers_copy_text(shot):
    """После Read ячейка статуса пересобирается — Copy text и подсказка не пропадают."""
    with sync_playwright() as pw:
        b, _ctx, p, sent = _live(pw)
        p.evaluate("s => amapApplyReading('FacebookDoc20 - Mios Darwiti', s)",
                   {"state": "ok", "spend": 7, "currency": "USD", "status": "disabled",
                    "status_text": "Account disabled", "finished_at": "10:00", "status_shot": shot,
                    "status_shot_at": "10:00"})
        cell = p.locator(".amap-status")
        assert cell.locator(".amap-st.bad").inner_text() == "disabled"
        assert cell.locator(".amap-st-hint").inner_text() == \
            "Copy screenshot and account number to send in the group"
        shot_btn = cell.locator(".amap-shot")
        assert shot_btn.count() == 1, "подсказка указывает на кнопку, которой нет"
        assert shot_btn.is_disabled() == (not shot)
        if not shot:
            assert shot_btn.get_attribute("title") == "No screenshot on the last read — read the account again"
        cell.locator(".amap-copy-text").click()
        p.wait_for_function("window.__copied.length === 1")
        assert p.evaluate("window.__copied") == ["FacebookDoc20 - Account Disabled"]
        assert sent == []
        b.close()


@pytest.mark.parametrize("status", ["clear", "unknown"])
def test_a_live_read_without_a_problem_offers_nothing(status):
    with sync_playwright() as pw:
        b, _ctx, p, _sent = _live(pw)
        p.evaluate("s => amapApplyReading('FacebookDoc20 - Mios Darwiti', s)",
                   {"state": "ok", "spend": 7, "currency": "USD", "status": status, "finished_at": "10:00"})
        cell = p.locator(".amap-status")
        assert cell.locator(".amap-copy-text").count() == 0
        assert cell.locator(".amap-shot").count() == 0
        assert cell.locator(".amap-st-hint").count() == 0
        b.close()


def test_a_live_read_escapes_the_account_name():
    """Хвост ячейки после Read собирается строкой — имя аккаунта экранируется."""
    with sync_playwright() as pw:
        b, _ctx, p, _sent = _live(pw)
        p.evaluate("""() => {
            document.querySelector('.amap-live').dataset.account = 'Evil"><b id=x>y - Owner';
            amapApplyReading('Evil"><b id=x>y - Owner', {state: 'ok', spend: 1, currency: 'USD',
                             status: 'blocked', finished_at: '10:00'});
        }""")
        assert p.locator("#x").count() == 0, "имя аккаунта попало в разметку"
        assert p.locator(".amap-copy-text").get_attribute("data-copy") == 'Evil"><b id=x>y - Account Blocked'
        b.close()
