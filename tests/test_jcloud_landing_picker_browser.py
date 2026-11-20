"""ACL-2852: реальный account_landings.js в браузере; сервер подменён настоящим хранилищем."""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs

import pytest
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.web import account_landings as store  # noqa: E402

JS = (ROOT / "src/web/static/account_landings.js").read_text(encoding="utf-8")
TEMPLATE = (ROOT / "src/web/templates/report.html").read_text(encoding="utf-8")
# Строка из настоящего шаблона: тест видит ту же разметку, что и билдер.
ROW = TEMPLATE[TEMPLATE.index("<div class=\"cb-preflight-row cb-landings\""):TEMPLATE.index("{# Account config row #}")]
ROW = ROW.replace("{{ acc_label | e }}", "Demo")


def _card(lid, title):
    return {"id": lid, "title": title, "url": f"https://lp.example.com/{title}", "domain": "lp.example.com",
            "path": f"/{title}", "template": "react_x", "language": "en", "status": None, "page_type": "funnel"}


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=os.environ.get("ACL_BROWSER_EXECUTABLE") or None)
        yield b
        b.close()


@pytest.fixture
def page(browser, monkeypatch, tmp_path):
    monkeypatch.setattr(store, "_path", lambda: tmp_path / "account_landings.json")
    searches = []
    pages = {"1": [_card(1, "aaa"), _card(2, "bbb"), _card(3, "ccc"), _card(4, "ddd")]}

    def route(r):
        url = r.request.url
        f = {k: v[0] for k, v in parse_qs(r.request.post_data or "").items()}
        if "multipart" in (r.request.headers.get("content-type") or ""):
            f = dict(re.findall(r'name="([^"]+)"\r\n\r\n(.*?)\r\n--', r.request.post_data or "", re.S))
        if url.endswith("/api/jcloud/landings/search"):
            searches.append(f)
            body = {"ok": True, "state": "ready", "searched_as": "path", "page": 1, "items": pages["1"],
                    "total": 4, "has_more": False, "snapshot_id": "s", "complete": True, "unclassified_count": 0}
        elif url.endswith("/api/account-landings"):
            op = f.get("op", "get")
            if op == "add":
                body = store.add_landing(f["account_label"], json.loads(f["landing"]))
            elif op == "remove":
                body = store.remove_landing(f["account_label"], f["id"])
            elif op == "percent":
                body = store.set_percent(f["account_label"], f["id"], f["percent"])
            else:
                body = {"ok": True, "landings": store.get_landings(f["account_label"])}
        else:
            r.fulfill(content_type="text/html", body="<!doctype html><html><body></body></html>")
            return
        r.fulfill(content_type="application/json", body=json.dumps(body))

    ctx = browser.new_context()
    ctx.route("**/*", route)
    pg = ctx.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.goto("http://acl.test/")
    pg.set_content("<div>" + ROW + "</div>")
    pg.add_script_tag(content=JS)
    pg.wait_for_selector("[data-landings-open]")
    pg.searches = searches
    yield pg
    assert not errors, errors
    ctx.close()


def _pick(page, query, title):
    page.click("[data-landings-open]")
    page.fill("[data-landings-query]", query)
    page.click("[data-landings-go]")
    page.locator(f'[data-landing-id] .cb-landing-title:text-is("{title}")').wait_for()
    page.locator("[data-landing-id]", has_text=title).locator("[data-landing-select]").click()
    page.wait_for_function("document.querySelector('[data-landings-search]').hidden")


def test_generate_is_visible_and_disabled(page):
    gen = page.get_by_role("button", name="Generate")
    assert gen.is_visible() and gen.is_disabled()


def test_pick_one_gets_100_then_two_get_50_50_then_three_33_33_34(page):
    _pick(page, "aaa", "aaa")
    assert page.locator(".cb-landing-pct").evaluate_all("els => els.map(e => e.value)") == ["100"]
    assert page.locator("[data-landings-open]").inner_text() == "+ Add landing"
    _pick(page, "bbb", "bbb")
    assert page.locator(".cb-landing-pct").evaluate_all("els => els.map(e => e.value)") == ["50", "50"]
    _pick(page, "ccc", "ccc")
    assert page.locator(".cb-landing-pct").evaluate_all("els => els.map(e => e.value)") == ["33", "33", "34"]
    # три — потолок: кнопка добавления исчезает
    assert page.locator("[data-landings-open]").is_hidden()


def test_remove_recalculates_and_brings_the_button_back(page):
    for q in ("aaa", "bbb", "ccc"):
        _pick(page, q, q)
    page.locator(".cb-landing-remove").nth(1).click()
    page.wait_for_function("document.querySelectorAll('.cb-landing-pct').length === 2")
    assert page.locator(".cb-landing-pct").evaluate_all("els => els.map(e => e.value)") == ["50", "50"]
    assert page.locator("[data-landings-open]").is_visible()


def test_editing_percent_is_saved_and_warns_when_not_100(page):
    _pick(page, "aaa", "aaa")
    _pick(page, "bbb", "bbb")
    page.locator(".cb-landing-pct").first.fill("70")
    page.locator(".cb-landing-pct").first.dispatch_event("change")
    page.wait_for_function("document.querySelector('[data-landings-status]').textContent.includes('120%')")
    assert store.get_landings("Demo")[0]["percent"] == 70


def test_chosen_landings_survive_a_reload(page):
    _pick(page, "aaa", "aaa")
    page.reload()
    page.set_content("<div>" + ROW + "</div>")
    page.add_script_tag(content=JS)
    page.wait_for_selector(".cb-landing-chosen")
    assert page.locator(".cb-landing-chosen-name").inner_text() == "aaa"


def test_every_row_has_an_open_in_new_tab_link_and_hover_shows_preview(page):
    page.click("[data-landings-open]")
    page.fill("[data-landings-query]", "x")
    page.click("[data-landings-go]")
    row = page.locator("[data-landing-id]").first
    row.wait_for()
    link = row.locator("a")
    assert link.get_attribute("target") == "_blank" and "noopener" in link.get_attribute("rel")
    assert link.get_attribute("href") == "https://lp.example.com/aaa"
    row.hover()
    page.locator("[data-landings-preview] iframe").wait_for()
    frame = page.locator("[data-landings-preview] iframe")
    assert frame.get_attribute("src") == "https://lp.example.com/aaa"
    assert frame.get_attribute("sandbox") == "allow-scripts"
    page.locator("[data-landings-query]").hover()
    page.wait_for_function("document.querySelector('[data-landings-preview]').hidden")


def test_use_landing_from_stats_reveals_the_builder_and_searches(page):
    # Каркас страны: ссылка в Manage, свёрнутый билдер, аккордеон аккаунта и блок Account.
    page.set_content(
        '<a class="sidebar-link" data-view="country-mx-manage" href="#" onclick="window.went=1;return false">Manage</a>'
        '<details class="manage-builder" id="manage-builder-mx"><div class="cb-acc-row">'
        '<div class="cb-acc-header" data-acc-id="Demo"></div>'
        '<div class="cb-acc-body" id="cb-body-Demo"><div class="cb-preflight cb-preflight-collapsed">' + ROW + '</div></div>'
        '</div></details>')
    page.add_script_tag(content="window.cbToggleAcc = function (id) { "
                                "document.getElementById('cb-body-' + id).classList.add('cb-acc-open'); };")
    page.add_script_tag(content=JS)
    assert page.evaluate("cbUseLandingFromStats('mx', 'aipl_en_1_lp')") is True
    assert page.evaluate("window.went") == 1
    assert page.evaluate("document.getElementById('manage-builder-mx').open") is True
    assert page.locator(".cb-acc-body.cb-acc-open").count() == 1
    assert page.locator(".cb-preflight-collapsed").count() == 0
    page.locator("[data-landing-id]").first.wait_for()
    assert page.locator("[data-landings-query]").input_value() == "aipl_en_1_lp"
    assert page.evaluate("cbUseLandingFromStats('zz', 'x')") is False


def test_use_landing_opens_the_picker_prefilled_and_searches(page):
    assert page.evaluate("cbUseLanding('Demo', 'aipl_en_1_lp')") is True
    page.locator("[data-landing-id]").first.wait_for()
    assert page.locator("[data-landings-query]").input_value() == "aipl_en_1_lp"
    assert page.searches[-1]["query"] == "aipl_en_1_lp"
    assert page.evaluate("cbUseLanding('Other', 'x')") is False
