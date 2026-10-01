"""One Connect button opens the found profile, then connects it (ACL-1457).

Раньше у найденного профиля было две кнопки, Open и Connect, и нажимать их надо
было именно в этом порядке: номер кабинета знает только профиль, открытый
автоматизацией. Владелец 01.10: одна кнопка, которая делает обе вещи.

Проверяется настоящий код страницы в офлайн-Chromium: порядок запросов к
серверу и то, что неудачное открытие не отменяет подключение.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import sync_playwright

REPORT = Path(__file__).resolve().parents[1] / "src/web/templates/report.html"


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=os.environ.get("ACL_BROWSER_EXECUTABLE") or None)
        yield b
        b.close()


def _run(browser, *, open_ok: bool, prompts: list[str]):
    page_src = REPORT.read_text(encoding="utf-8")
    script = page_src[page_src.index("var _mlxCache ="):page_src.index("function amapAddThisCountry")]
    html = """<!doctype html><html><body>
<div class="amap-add" data-amap-country="Kuwait">
  <select class="amap-inst"><option value="A" selected>A</option></select>
  <span class="amap-msg"></span><div class="amap-hits"></div>
</div>
<script>
var ACL_MLX_LABELS = {A: 'Inhouse', B: 'KLD'};
function aclEscHtml(s) { return String(s).replace(/[&<>"']/g, function (c) { return '&#' + c.charCodeAt(0) + ';'; }); }
var SENT = [];
function amapSend(payload) { SENT.push(payload); }
var PROMPTS = %s;
window.prompt = function () { return PROMPTS.length ? PROMPTS.shift() : null; };
%s
</script></body></html>""" % (json.dumps(prompts), script)
    calls: list[str] = []
    ctx = browser.new_context()
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))

    def route(r):
        path = urlsplit(r.request.url).path
        if path == "/":
            r.fulfill(body=html, content_type="text/html"); return
        calls.append(path)
        if path == "/mlx-profile-open":
            body = {"ok": open_ok, "message": "opened" if open_ok else "profile_open_by_hand"}
        elif path == "/mlx-profile-details":
            body = {"ok": True, "folder_id": "f1", "ad_account_id": "", "suggested_name": "FacebookTest1"}
        elif path == "/account-probe/ad-account":
            body = {"ok": open_ok, "act": "act_111" if open_ok else "", "error": "" if open_ok else "not open"}
        elif path == "/account-map-patch":
            body = {"ok": True}
        else:
            r.abort(); return
        r.fulfill(body=json.dumps(body), content_type="application/json")

    ctx.route("**/*", route)
    page.goto("http://acl.test/")
    page.evaluate("""() => {
      const box = document.querySelector('.amap-add');
      amapRenderHits(box, box.querySelector('.amap-hits'),
                     [{name: 'Test 1', profile_id: 'p1', folder_id: 'f1', _details: {ad_account_id: ''}}], 'A');
    }""")
    buttons = [b.inner_text() for b in page.locator(".amap-hit button").all()]
    page.locator(".amap-hit button", has_text="Connect").click()
    page.wait_for_function("SENT.length > 0 || document.querySelector('.amap-msg').className.includes('bad')", timeout=5000)
    sent = page.evaluate("SENT")
    ctx.close()
    return buttons, calls, sent, errors


def test_a_found_profile_has_one_button(browser):
    buttons, _calls, _sent, errors = _run(browser, open_ok=True, prompts=["FacebookTest1", "act_111"])
    assert buttons == ["Connect"], f"кнопки у найденного профиля: {buttons}"
    assert not errors


def test_connect_opens_the_profile_before_reading_its_account(browser):
    _b, calls, sent, errors = _run(browser, open_ok=True, prompts=["FacebookTest1", "act_111"])
    assert calls.index("/mlx-profile-open") < calls.index("/account-probe/ad-account") < calls.index("/account-map-patch")
    assert sent and sent[0]["account"] == "FacebookTest1" and sent[0]["value"] == "Kuwait"
    assert not errors


def test_a_failed_open_does_not_cancel_connecting(browser):
    """Окно открыто руками — робот его не перезапускает, но подключить можно."""
    _b, calls, sent, errors = _run(browser, open_ok=False, prompts=["FacebookTest1", "act_222"])
    assert "/mlx-profile-open" in calls
    assert "/account-map-patch" in calls, "неудачное открытие отменило подключение"
    assert sent and sent[0]["account"] == "FacebookTest1"
    assert not errors


def test_the_fallback_prompt_no_longer_points_to_an_open_button():
    page = REPORT.read_text(encoding="utf-8")
    assert "Press Open next to the profile" not in page
    assert "press Connect again" in page
