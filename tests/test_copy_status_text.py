"""Строка для группы поставщика аккаунтов под плохим статусом (acl-132 → ACL-1520).

Решение владельца 30.09: под плохим статусом — строка «<имя до " - "> -
Account <Status>» для группы. 03.10 (ACL-1520) Copy text и Copy screenshot
слились в одну кнопку **Copy for group**: картинка, где та же строка — полосой
над снимком. Здесь — разметка кнопки (шаблон и живой Read); сама картинка и
буфер обмена — в test_copy_for_group.py. Ничего не отправляется: человек
вставляет и жмёт Send сам.
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

HINT = "Copy for group — paste it into the provider's group"


def _render(account: str, status: dict, show: bool = True) -> str:
    from src.analysis_agent.money_format import fmt_currency

    env = Environment(loader=FileSystemLoader(str(TPL)), autoescape=True)
    env.filters["fmt_currency"] = fmt_currency
    env.filters["fmt_usd"] = str
    country = {"country": "Kuwait", "key": "KW", "virtual": False,
               "rows": [{"account": account, "has_data": False, "ready": True,
                         "in_workbook": True, "status": status, "to_do": ""}]}
    # ACL-1652 (owner, 07.10): the button is hidden by default; these tests check the
    # markup it has when the flag is on, so they switch it on explicitly.
    html = env.get_template("_account_map_country.html").render(
        SHOW_COPY_FOR_GROUP=show, mc=country, mlx={},
        amap={"countries": [country], "available": [], "all_accounts": [], "live": {},
              "splits": {}, "fallback": {}, "dates": [], "date_from": "2026-09-01",
              "date_to": "2026-09-28", "span": ""})
    cell = html[html.index('<td class="amap-status">'):]
    return cell[: cell.index("</td>")]


def _group(cell: str) -> str:
    m = re.search(r'<button[^>]*class="amap-btn amap-copy-group"[^>]*>', cell)
    assert m, "кнопки Copy for group нет"
    return m.group(0)


@pytest.mark.parametrize("status,line", [
    ("disabled", "FacebookDoc20 — Account Disabled"),
    ("restricted", "FacebookDoc20 — Account Restricted"),
    ("blocked", "FacebookDoc20 — Account Blocked"),
    ("review", "FacebookDoc20 — Account Under Review"),
])
def test_a_problem_status_offers_copy_for_group(status, line):
    cell = _render("FacebookDoc20 - Mios Darwiti", {
        "status": status, "status_text": "x", "at": "2026-09-30 15:43:25", "at_short": "30.09",
        "shot_id": "status_Doc20__20260930-154326", "shot_at": "2026-09-30 15:43:26"})
    btn = _group(cell)
    assert f'data-head="{line} · 2026-09-30 15:43:25"' in btn
    assert 'data-shot="status_Doc20__20260930-154326"' in btn
    assert 'onclick="amapCopyGroup(this)"' in btn and " disabled" not in btn
    assert ">Copy for group</button>" in cell
    assert HINT in cell
    # Одна кнопка вместо двух (ACL-1520).
    assert "Copy text" not in cell and "Copy screenshot" not in cell


@pytest.mark.parametrize("status", ["clear", "unknown"])
def test_a_healthy_or_unread_status_offers_nothing(status):
    cell = _render("FacebookDoc20 - Mios Darwiti", {"status": status, "at": "2026-09-30 15:43:25", "at_short": "30.09"})
    assert "amap-copy-group" not in cell and "provider" not in cell


def test_a_name_without_a_dash_is_used_whole():
    cell = _render("Facebook_1443_61579302961692", {"status": "disabled", "at": "x", "at_short": "30.09",
                                                     "shot_id": "status_x__20260930-100000"})
    assert 'data-head="Facebook_1443_61579302961692 — Account Disabled · x"' in cell


def test_a_name_is_escaped_in_the_attribute():
    cell = _render('Evil"><b>x - Owner', {"status": "disabled", "at": "x", "at_short": "30.09",
                                          "shot_id": "status_x__20260930-100000"})
    assert "<b>x" not in cell, "имя аккаунта вставлено в разметку без экранирования"


def test_without_a_screenshot_the_button_is_disabled_and_says_why():
    """Issue #31 / ACL-1520 п.3: без снимка кнопка выключена и говорит почему."""
    cell = _render("FacebookDoc20 - Mios Darwiti", {"status": "restricted", "at": "x", "at_short": "30.09",
                                                     "shot_id": "", "shot_at": ""})
    btn = _group(cell)
    assert " disabled" in btn and "onclick" not in btn and "data-shot" not in btn
    assert 'title="Screenshots are switched off for safety"' in btn
    assert HINT in cell


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
                body + "".join(f"<script>{s}</script>" for s in scripts)))
        else:
            sent.append(route.request.url)
            route.abort()

    p.route("**/*", serve)
    p.goto("http://localhost/")
    return b, ctx, p, sent


_LIVE_ROW = (
    '<table><tr><td class="amap-live" data-account="FacebookDoc20 - Mios Darwiti"></td>'
    '<td class="amap-status"><span class="amap-st ok">no warning</span></td>'
    '<td><button class="amap-read">Read spend</button></td></tr></table>'
)
_LIVE_STUBS = (
    "var amapReadAllBusy=false;function amapMoney(v,c){return v+' '+c}function amapReadTitle(){return ''}"
    "function amapPaintTree(){}function amapUpdateCountrySummaries(){}function amapSpendFailed(){}"
    "function amapCopyShot(){}function amapCopyGroup(b){window.__group=b.dataset.head}function amapRereadSync(){}"
)


def _live(pw, show: bool | None = None):
    """`show` — флаг SHOW_COPY_FOR_GROUP, который report.html отдаёт странице как
    `window.AMAP_SHOW_COPY_FOR_GROUP`; None — флага нет вовсе (как до присваивания)."""
    flag = "" if show is None else f"window.AMAP_SHOW_COPY_FOR_GROUP = {str(show).lower()};"
    return _page_with(pw, _LIVE_ROW, [_LIVE_STUBS, flag, _js("var AMAP_STATUS_WORDS"), _js("function amapApplyReading")])


@pytest.mark.parametrize("shot", ["status_Doc20__20261002-100000", ""])
def test_a_live_read_that_finds_a_problem_offers_copy_for_group(shot):
    """Флаг включён: после Read ячейка статуса пересобирается, кнопка и подсказка на месте."""
    with sync_playwright() as pw:
        b, _ctx, p, sent = _live(pw, show=True)
        p.evaluate("s => amapApplyReading('FacebookDoc20 - Mios Darwiti', s)",
                   {"state": "ok", "spend": 7, "currency": "USD", "status": "disabled",
                    "status_text": "Account disabled", "finished_at": "2026-10-02 10:00:00", "status_shot": shot,
                    "status_shot_at": "2026-10-02 10:00:01"})
        cell = p.locator(".amap-status")
        assert cell.locator(".amap-st.bad").inner_text() == "disabled"
        assert cell.locator(".amap-st-hint").inner_text() == HINT
        assert cell.locator(".amap-shot").count() == 0 and cell.locator(".amap-copy-text").count() == 0
        btn = cell.locator(".amap-copy-group")
        assert btn.count() == 1 and btn.inner_text() == "Copy for group"
        assert btn.is_disabled() == (not shot)
        if shot:
            btn.click()
            assert p.evaluate("window.__group") == "FacebookDoc20 — Account Disabled · 2026-10-02 10:00:00"
            assert btn.get_attribute("data-shot") == shot
        else:
            assert btn.get_attribute("title") == "Screenshots are switched off for safety"
        assert sent == []
        b.close()


@pytest.mark.parametrize("show", [None, False])
@pytest.mark.parametrize("shot", ["status_Doc20__20261002-100000", ""])
def test_a_live_read_does_not_bring_copy_for_group_back_by_default(shot, show):
    """Владелец 07.10 скрыл кнопку; живой Read не должен возвращать её (ревью PR #667).

    Скрыто и без флага вовсе, и при `false`. Статус и красный значок остаются."""
    with sync_playwright() as pw:
        b, _ctx, p, sent = _live(pw, show=show)
        p.evaluate("s => amapApplyReading('FacebookDoc20 - Mios Darwiti', s)",
                   {"state": "ok", "spend": 7, "currency": "USD", "status": "disabled",
                    "status_text": "Account disabled", "finished_at": "2026-10-02 10:00:00", "status_shot": shot,
                    "status_shot_at": "2026-10-02 10:00:01"})
        cell = p.locator(".amap-status")
        assert cell.locator(".amap-st.bad").inner_text() == "disabled"
        assert cell.locator(".amap-copy-group").count() == 0
        assert cell.locator(".amap-st-hint").count() == 0
        assert sent == []
        b.close()


def test_the_page_hands_the_flag_to_the_script():
    src = (TPL / "report.html").read_text(encoding="utf-8")
    assert "window.AMAP_SHOW_COPY_FOR_GROUP = {{ SHOW_COPY_FOR_GROUP | tojson }};" in src
    assert "window.AMAP_SHOW_COPY_FOR_GROUP === true" in src  # `typeof window` guard keeps Node vm stands working


@pytest.mark.parametrize("status", ["clear", "unknown"])
def test_a_live_read_without_a_problem_offers_nothing(status):
    with sync_playwright() as pw:
        b, _ctx, p, _sent = _live(pw)
        p.evaluate("s => amapApplyReading('FacebookDoc20 - Mios Darwiti', s)",
                   {"state": "ok", "spend": 7, "currency": "USD", "status": status, "finished_at": "10:00"})
        cell = p.locator(".amap-status")
        assert cell.locator(".amap-copy-group").count() == 0
        assert cell.locator(".amap-shot").count() == 0
        assert cell.locator(".amap-st-hint").count() == 0
        b.close()


def test_a_live_unreadable_status_with_a_shot_keeps_copy_screenshot():
    """У «unreadable» нет строки «Account <Status>» — остаётся прежняя кнопка снимка."""
    with sync_playwright() as pw:
        b, _ctx, p, _sent = _live(pw)
        p.evaluate("s => amapApplyReading('FacebookDoc20 - Mios Darwiti', s)",
                   {"state": "ok", "spend": 7, "currency": "USD", "status": "unknown", "finished_at": "10:00",
                    "status_shot": "status_Doc20__20261002-100000", "status_shot_at": "10:00"})
        cell = p.locator(".amap-status")
        assert cell.locator(".amap-shot").count() == 1
        assert cell.locator(".amap-copy-group").count() == 0
        b.close()


def test_a_live_read_escapes_the_account_name():
    """Хвост ячейки после Read собирается строкой — имя аккаунта экранируется."""
    with sync_playwright() as pw:
        b, _ctx, p, _sent = _live(pw, show=True)
        p.evaluate("""() => {
            document.querySelector('.amap-live').dataset.account = 'Evil"><b id=x>y - Owner';
            amapApplyReading('Evil"><b id=x>y - Owner', {state: 'ok', spend: 1, currency: 'USD',
                             status: 'blocked', finished_at: '10:00', status_shot: 'status_x__20261002-100000'});
        }""")
        assert p.locator("#x").count() == 0, "имя аккаунта попало в разметку"
        assert p.locator(".amap-copy-group").get_attribute("data-head") == 'Evil"><b id=x>y — Account Blocked · 10:00'
        b.close()
