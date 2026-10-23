"""Production connect dialog and recorder with synthetic local-only transport."""
from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlparse

import pytest
from playwright.sync_api import expect, sync_playwright

from src.fb_ads_rpa import click_recorder
from tests.test_acl2038_connect_team_library import TOKEN

ROOT = Path(__file__).resolve().parents[1]
REPORT = (ROOT / "src/web/templates/report.html").read_text(encoding="utf-8")
STYLE = REPORT[REPORT.index("<style>") + len("<style>"):REPORT.index("</style>")]
# The production page inlines this shared include after its compatibility CSS.
STYLE += "\n" + (ROOT / "src/web/templates/adopy-tokens.css").read_text(encoding="utf-8")
START = REPORT.index("window.aclTeamLibrary = (function")
SCRIPT = REPORT[START:REPORT.index("// ── Таргеты: общая библиотека", START)]
CONFIRM = REPORT[REPORT.index("function _safeConfirm("):REPORT.index("// ── Календарь периода для карты аккаунтов")]


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        instance = pw.chromium.launch(executable_path=os.environ.get("ACL_BROWSER_EXECUTABLE") or None)
        yield instance
        instance.close()


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [390, 1280])
def test_masked_connect_retry_cancel_and_disconnect_from_both_library_views(browser, theme, width):
    context = browser.new_context(viewport={"width": width, "height": 900}, color_scheme=theme)
    page = context.new_page()
    state = {"connected": False, "checks": 0}
    requests, console, errors = [], [], []
    page.on("console", lambda message: console.append(message.text))
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("dialog", lambda dialog: dialog.accept())

    def handle(route):
        req = route.request
        path = urlparse(req.url).path
        if req.resource_type == "document":
            connected = json.dumps(state["connected"])
            html = (f'<!doctype html><html data-theme="{theme}"><head><style>{STYLE}</style></head>'
                    '<body><section aria-label="Targeting"><p id="tg-status"></p></section>'
                    '<section aria-label="Ad titles"><p id="at-status"></p></section>'
                    f'<script>{CONFIRM}\n{SCRIPT}\n'
                    f'aclTeamLibrary.decorate(document.getElementById("tg-status"),{{connected:{connected}}});'
                    f'aclTeamLibrary.decorate(document.getElementById("at-status"),{{connected:{connected}}});'
                    '</script></body></html>')
            return route.fulfill(content_type="text/html", body=html)
        if path not in {"/api/team-library/connect", "/api/team-library/disconnect"}:
            return route.abort()
        assert req.method == "POST" and TOKEN not in req.url
        payload = json.loads(req.post_data or "{}")
        requests.append((path, payload))
        if path.endswith("/connect"):
            assert payload == {"token": TOKEN}
            state["checks"] += 1
            if state["checks"] == 1:
                return route.fulfill(status=400, json={"ok": False, "error": "The library token has expired."})
            state["connected"] = True
            return route.fulfill(json={"ok": True, "message": "Connected. 2 targets and 1 ad text added to the team library."})
        assert payload == {}
        state["connected"] = False
        return route.fulfill(json={"ok": True})

    page.route("**/*", handle)
    try:
        page.goto("http://acl.test/")
        expect(page.get_by_role("button", name="Connect team library", exact=True)).to_have_count(2)
        page.get_by_role("region", name="Ad titles").get_by_role("button").click()
        token = page.get_by_label("Library token", exact=True)
        token.fill(TOKEN)
        token.press("Escape")
        expect(page.get_by_role("dialog")).to_have_count(0)
        assert not requests

        page.evaluate(click_recorder._INJECT_JS)
        page.get_by_role("region", name="Targeting").get_by_role("button").click()
        dialog = page.get_by_role("dialog", name="Connect team library")
        assert page.evaluate("getComputedStyle(document.documentElement).colorScheme") == theme
        assert "sans-serif" in page.evaluate("getComputedStyle(document.body).fontFamily")
        assert dialog.locator("input").count() == 1
        token = dialog.get_by_label("Library token", exact=True)
        assert token.get_attribute("type") == "password"
        token.fill(TOKEN)
        token.press("Tab")
        recorded = page.evaluate(click_recorder._DRAIN_JS)
        secret_changes = [a for a in recorded if a.get("type") == "change"]
        assert secret_changes and all(a.get("value") == "<hidden>" for a in secret_changes)
        assert TOKEN not in json.dumps(recorded) + page.content()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        shots = os.environ.get("ACL2038_SHOTS")
        if shots:
            folder = Path(shots); folder.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(folder / f"connect-{theme}-{width}.png"))
        token.focus(); token.press("Enter")
        expect(dialog.get_by_role("status")).to_have_text("The library token has expired.")
        assert token.input_value() == ""
        token.fill(TOKEN)
        dialog.get_by_role("button", name="Connect", exact=True).click()
        expect(dialog.get_by_role("status")).to_contain_text("2 targets and 1 ad text added")
        assert token.input_value() == "" and TOKEN not in page.content()
        dialog.get_by_role("button", name="Done", exact=True).click()
        expect(page.get_by_role("button", name="Disconnect", exact=True)).to_have_count(2)
        page.get_by_role("region", name="Ad titles").get_by_role("button", name="Disconnect").click()
        # The current-page confirmation remains usable before ACL-141 helpers arrive.
        page.locator("#safe-confirm-modal").get_by_role("button", name="Disconnect", exact=True).click()
        expect(page.get_by_role("button", name="Connect team library", exact=True)).to_have_count(2)
        assert [p for p, _ in requests] == ["/api/team-library/connect", "/api/team-library/connect", "/api/team-library/disconnect"]
        assert not errors and TOKEN not in "\n".join(console) + page.content()
    finally:
        context.close()


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [390, 1280])
def test_partial_seed_retry_after_reload_and_confirmed_disconnect_errors(browser, theme, width):
    context = browser.new_context(viewport={"width": width, "height": 900}, color_scheme=theme)
    page = context.new_page()
    state = {"connected": False, "connects": 0, "disconnects": 0}
    errors, requests, native_dialogs = [], [], []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("dialog", lambda dialog: (native_dialogs.append(dialog.type), dialog.dismiss()))

    def route(handler):
        req = handler.request
        path = urlparse(req.url).path
        if req.resource_type == "document":
            connected = json.dumps(state["connected"])
            html = (f'<!doctype html><html data-theme="{theme}"><head><style>{STYLE}</style></head><body>'
                    '<section aria-label="Targeting"><p id="tg-status"></p></section>'
                    '<section aria-label="Ad titles"><p id="at-status"></p></section>'
                    f'<script>{CONFIRM}\n{SCRIPT}\n'
                    f'aclTeamLibrary.decorate(document.getElementById("tg-status"),{{connected:{connected}}});'
                    f'aclTeamLibrary.decorate(document.getElementById("at-status"),{{connected:{connected}}});'
                    '</script></body></html>')
            return handler.fulfill(content_type="text/html", body=html)
        if path not in {"/api/team-library/connect", "/api/team-library/disconnect"}:
            return handler.abort()
        assert req.method == "POST" and TOKEN not in req.url
        body = json.loads(req.post_data or "{}")
        requests.append(path)
        if path.endswith("/connect"):
            assert body == {"token": TOKEN}
            state["connects"] += 1
            state["connected"] = True
            if state["connects"] == 1:
                return handler.fulfill(json={"ok": True, "seeded": False, "seed_error": "Synthetic upload refused.",
                    "message": "Connected, but not everything was added (1 target, 0 ad texts so far). Press Connect again to retry."})
            return handler.fulfill(json={"ok": True, "seeded": True, "message": "Connected. 1 target and 1 ad text added."})
        assert body == {}
        state["disconnects"] += 1
        if state["disconnects"] <= 2:
            return handler.fulfill(status=400, json={"ok": False, "error": "Synthetic settings failure."})
        state["connected"] = False
        return handler.fulfill(json={"ok": True})

    context.route("**/*", route)
    try:
        page.goto("http://acl.test/")
        page.get_by_role("region", name="Targeting").get_by_role("button", name="Connect team library").click()
        dialog = page.get_by_role("dialog", name="Connect team library")
        token = dialog.get_by_label("Library token")
        token.fill(TOKEN)
        dialog.get_by_role("button", name="Connect", exact=True).click()
        expect(dialog.get_by_role("status")).to_contain_text("not everything was added")
        expect(token).to_be_visible(timeout=1500)
        assert token.input_value() == "" and token.get_attribute("type") == "password"
        expect(dialog.get_by_role("button", name="Connect", exact=True)).to_be_enabled()
        assert dialog.get_by_role("button", name="Done", exact=True).count() == 0
        assert TOKEN not in page.content() and page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        shots = os.environ.get("ACL2038_SHOTS")
        if shots:
            folder = Path(shots); folder.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(folder / f"partial-{theme}-{width}.png"))
        # Closing/reloading an incomplete connection must not strand the pending import.
        dialog.get_by_role("button", name="Cancel", exact=True).click()
        page.reload()
        page.get_by_role("region", name="Ad titles").get_by_role("button", name="Add local items", exact=True).click()
        dialog = page.get_by_role("dialog", name="Connect team library")
        dialog.get_by_label("Library token").fill(TOKEN)
        dialog.get_by_role("button", name="Connect", exact=True).click()
        expect(dialog.get_by_role("button", name="Done", exact=True)).to_be_visible()
        dialog.get_by_role("button", name="Done", exact=True).click()
        disconnect = page.get_by_role("region", name="Targeting").get_by_role("button", name="Disconnect", exact=True)
        disconnect.click()
        modal = page.locator("#safe-confirm-modal")
        modal.get_by_role("button", name="Cancel", exact=True).click()
        assert state["disconnects"] == 0
        disconnect.click()
        page.locator("#safe-confirm-modal").get_by_role("button", name="Disconnect", exact=True).click()
        expect(page.get_by_role("alert")).to_have_text("Synthetic settings failure.")
        assert state["connected"] and not native_dialogs
        # The next batch supplies this async contract; no unmerged source is imported.
        page.evaluate("() => { window.aclConfirm = function(){window.confirmCalls=(window.confirmCalls||0)+1;return Promise.resolve(false);}; }")
        assert page.evaluate("window.confirmCalls || 0") == 0
        disconnect.click()
        page.wait_for_function("window.confirmCalls === 1")
        assert state["disconnects"] == 1
        page.evaluate("""() => {
          window.aclConfirm = function(){window.confirmCalls++;return Promise.resolve(true);};
          window.aclInlineError = function(anchor, text){
            if (!anchor.isConnected) throw new Error('Error anchor must remain connected');
            window.inlineCalls = (window.inlineCalls || 0) + 1;
            var message = anchor.querySelector('[role=alert]');
            if (!message) { message = document.createElement('span'); message.setAttribute('role','alert'); anchor.appendChild(message); }
            message.textContent = text;
          };
        }""")
        disconnect.click()
        page.wait_for_function("window.inlineCalls === 1")
        expect(page.get_by_role("alert")).to_have_text("Synthetic settings failure.")
        assert state["connected"] and state["disconnects"] == 2
        disconnect.click()
        expect(page.get_by_role("button", name="Connect team library", exact=True)).to_have_count(2)
        assert state["disconnects"] == 3 and not errors and not native_dialogs and TOKEN not in page.content()
    finally:
        context.close()
