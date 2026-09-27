# -*- coding: utf-8 -*-
"""Выбор рабочего пространства и Reconnect на странице Accounts (ACL-1335).

Настоящая разметка, стили и обработчики из шаблонов — в офлайновом Chromium,
сервер подменён маршрутами. Проверяется то, что видит человек:

* у входа с несколькими пространствами появляется выбор, и выбрано то, чьё
  имя совпадает с меткой слота (Inhouse / KLD);
* пароль не живёт в поле после запроса, даже когда дальше — выбор;
* строка статуса говорит, как подключён слот: «signed in as … · renews
  automatically» или «automation token»;
* Reconnect есть и у подключённого слота, и форма переподключения не
  исчезает сама, пока новый вход не удался; неудачный вход старое
  подключение не трогает;
* истёкший вход зовёт нажать именно Reconnect.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from jinja2 import Environment, FileSystemLoader
from playwright.sync_api import sync_playwright

TPL = Path(__file__).resolve().parents[1] / "src/web/templates"

WORKSPACES = [
    {"workspace_id": "ws-kld", "name": "KLD Team", "role": "launcher", "can_launch": True},
    {"workspace_id": "ws-in", "name": "Adopy Inhouse", "role": "owner", "can_launch": True},
    {"workspace_id": "ws-view", "name": "Audit", "role": "viewer", "can_launch": False},
]


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=os.environ.get("ACL_BROWSER_EXECUTABLE") or None)
        yield b
        b.close()


def _page(browser, *, status: dict, signin: dict | None = None):
    report = (TPL / "report.html").read_text(encoding="utf-8")
    styles = "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", report, re.S))
    script = report[report.index("var _mlxCache ="):report.index("\nfunction amapHint")]
    shared = report[report.index('<template id="amap-ml-signin-template">'):report.index("</body>")]
    env = Environment(loader=FileSystemLoader(str(TPL)), autoescape=True)
    env.filters["fmt_usd"] = str
    from src.analysis_agent.money_format import fmt_currency
    env.filters["fmt_currency"] = fmt_currency
    countries = [{"country": "Mexico", "key": "MX", "rows": [], "virtual": False}]
    blocks = env.get_template("_account_map_country.html").render(
        mc=countries[0],
        amap={"countries": countries, "available": [], "all_accounts": [], "live": {},
              "dates": [], "date_from": "", "date_to": "", "span": ""},
        mlx={"A": "Inhouse", "B": "KLD"})
    html = (f"<style>{styles}</style>{blocks}{shared}"
            f'<script>var ACL_MLX_LABELS = {{A:"Inhouse",B:"KLD"}};{script}</script>')
    ctx = browser.new_context()
    page = ctx.new_page()
    state = {"status": dict(status), "calls": [], "signin": signin or {}}
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))

    def route(r):
        url = urlsplit(r.request.url)
        if url.path == "/":
            r.fulfill(body=html, content_type="text/html; charset=utf-8")
            return
        data = r.request.post_data_json if r.request.method == "POST" else {}
        state["calls"].append((url.path, data))
        if url.path == "/multilogin-token-status":
            body = state["status"]
        elif url.path == "/multilogin-signin":
            body = state["signin"]
        elif url.path == "/multilogin-signin-workspace":
            ws = next(w for w in WORKSPACES if w["workspace_id"] == data["workspace_id"])
            state["status"] = {"ok": True, "alive": True, "has_token": True, "method": "signin",
                               "email": "buyer@example.test", "workspace": ws["name"], "renews": True}
            body = {"ok": True, "token_saved": True, "method": "signin", "workspace": ws["name"]}
        elif url.path == "/multilogin-search":
            body = {"ok": True, "profiles": []}
        else:
            r.abort()
            return
        r.fulfill(body=json.dumps(body), content_type="application/json")

    ctx.route("**/*", route)
    page.goto("http://acl.test/")
    page.wait_for_function("Object.keys(AMAP_ML_STATE).length === 1")
    return ctx, page, state, errors


SIGNED_IN = {"ok": True, "alive": True, "has_token": True, "method": "signin",
             "email": "buyer@example.test", "workspace": "Adopy Inhouse", "renews": True}


def _sign_in(page):
    page.locator(".amap-ml-email").fill("buyer@example.test")
    page.locator(".amap-ml-pass").fill("synthetic-password")
    page.locator(".amap-ml-signin").get_by_role("button", name="Connect", exact=True).click()


def test_several_workspaces_offer_a_picker_preselected_by_the_slot_label(browser):
    ctx, p, s, errors = _page(browser, status={"ok": True, "alive": False, "has_token": False},
                              signin={"ok": True, "needs_workspace": True, "pending": "nonce-1",
                                      "token_saved": False, "workspaces": WORKSPACES})
    try:
        p.locator(".amap-ml-open").click()
        _sign_in(p)
        p.wait_for_function("!document.querySelector('.amap-ml-pick').hidden")
        assert p.locator(".amap-ml-ws").input_value() == "ws-in", "метка Inhouse → Adopy Inhouse"
        options = p.locator(".amap-ml-ws option").all_inner_texts()
        assert "Audit · viewer · cannot launch profiles" in options
        assert p.locator(".amap-ml-pass").input_value() == "", "пароль пережил запрос"
        assert "pick the one for Inhouse" in p.locator(".amap-msg").inner_text()

        p.get_by_role("button", name="Use workspace").click()
        p.wait_for_function("AMAP_ML_STATE.A && AMAP_ML_STATE.A.alive === true")
        sent = next(d for path, d in s["calls"] if path == "/multilogin-signin-workspace")
        assert sent == {"pending": "nonce-1", "workspace_id": "ws-in", "instance": "A"}
        assert "password" not in json.dumps(sent)
        assert p.locator(".amap-ml-signin").count() == 0
        assert p.locator(".amap-ml-line").inner_text() == (
            "Multilogin Inhouse: connected · signed in as buyer@example.test · Adopy Inhouse"
            " · renews automatically")
        assert not errors
    finally:
        ctx.close()


def test_the_picker_follows_the_slot_that_is_being_connected(browser):
    ctx, p, s, errors = _page(browser, status={"ok": True, "alive": False, "has_token": False},
                              signin={"ok": True, "needs_workspace": True, "pending": "nonce-2",
                                      "token_saved": False, "workspaces": WORKSPACES})
    try:
        p.locator(".amap-inst").select_option("B")
        p.wait_for_function("AMAP_ML_STATE.B !== undefined")
        p.locator(".amap-ml-open").click()
        _sign_in(p)
        p.wait_for_function("!document.querySelector('.amap-ml-pick').hidden")
        assert p.locator(".amap-ml-ws").input_value() == "ws-kld"
        assert not errors
    finally:
        ctx.close()


def test_a_connected_slot_offers_reconnect_and_keeps_the_form_until_it_works(browser):
    ctx, p, s, errors = _page(browser, status=SIGNED_IN,
                              signin={"ok": False, "error": "Sign-in failed [401]: wrong credentials"})
    try:
        assert p.locator(".amap-ml-open").is_hidden()
        assert p.locator(".amap-ml-reconnect").is_visible()
        assert "renews automatically" in p.locator(".amap-ml-line").inner_text()

        p.locator(".amap-ml-reconnect").click()
        assert p.locator(".amap-ml-signin").count() == 1
        # Перерисовка статуса (например, после проверки) форму не уносит.
        p.evaluate("amapMlRefresh('A', true)")
        p.wait_for_function(
            "document.querySelectorAll('.amap-ml-signin').length === 1 && AMAP_ML_STATE.A.alive")
        assert p.locator(".amap-ml-signin").count() == 1

        _sign_in(p)
        p.wait_for_function("document.querySelector('.amap-msg').textContent.includes('wrong credentials')")
        assert p.locator(".amap-ml-line").inner_text().startswith("Multilogin Inhouse: connected")
        assert p.locator(".amap-ml-signin").count() == 1
        assert p.locator(".amap-ml-pass").input_value() == ""
        assert not errors
    finally:
        ctx.close()


def test_a_successful_reconnect_closes_the_form(browser):
    ctx, p, s, errors = _page(browser, status=SIGNED_IN,
                              signin={"ok": True, "token_saved": True, "method": "signin"})
    try:
        p.locator(".amap-ml-reconnect").click()
        _sign_in(p)
        p.wait_for_function("document.querySelectorAll('.amap-ml-signin').length === 0")
        assert "Connected" in p.locator(".amap-msg").inner_text()
        assert not errors
    finally:
        ctx.close()


def test_an_expired_sign_in_asks_for_reconnect(browser):
    expired = {**SIGNED_IN, "alive": False, "reason": "signin_expired",
               "error": "Multilogin sign-in expired — press Reconnect in Accounts."}
    ctx, p, s, errors = _page(browser, status=expired)
    try:
        assert p.locator(".amap-ml-line").inner_text() == (
            "Multilogin Inhouse: sign-in expired — press Reconnect")
        assert p.locator(".amap-ml-reconnect").is_visible()
        assert p.locator(".amap-ml-open").is_hidden()
        assert not errors
    finally:
        ctx.close()


def test_an_automation_token_is_labelled_as_such(browser):
    ctx, p, s, errors = _page(browser, status={"ok": True, "alive": True, "has_token": True,
                                               "method": "automation"})
    try:
        assert p.locator(".amap-ml-line").inner_text() == "Multilogin Inhouse: connected · automation token"
        assert p.locator(".amap-ml-reconnect").is_visible()
        assert not errors
    finally:
        ctx.close()
