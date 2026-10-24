"""Real ACL142 caller code with adopted ACL141 dialogs; synthetic offline data."""
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect, sync_playwright

from tests.test_acl1841_integrated_responsive import integrated

TEMPLATES = Path(__file__).resolve().parents[1] / "src/web/templates"


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        instance = pw.chromium.launch(executable_path=os.environ.get("ACL_BROWSER_EXECUTABLE") or None)
        yield instance
        instance.close()


@pytest.fixture(params=[("light", 390), ("dark", 1280)], ids=["light390", "dark1280"])
def composed(browser, request):
    theme, width = request.param
    source = (TEMPLATES / "report.html").read_text(encoding="utf-8")
    css = (TEMPLATES / "adopy-tokens.css").read_text(encoding="utf-8") + "\n" + re.search(r"<style>(.*?)</style>", source, re.S).group(1)
    ask = source[source.index("function amapAccountModel()"):source.index("async function amapConnect(")]
    send = source[source.index("async function amapSend("):source.index("/* ACL-1620 notes {")]
    actions = source[source.index("async function amapUnlink("):source.index("function amapClose(")]
    component = (TEMPLATES / "_acl_dialog.js").read_text(encoding="utf-8")
    html = f'''<!doctype html><html lang="en" data-theme="{theme}"><head><meta charset="utf-8"><style>{css}</style></head><body>
<button id="open">Connect profile</button><button id="remove">Remove from ACL</button>
<button id="unlink">Take out of Chile</button><input id="inspect" aria-label="Inspect page">
<script>{component}\n{send}\n{actions}\n{ask}
window.answer='pending'; window.replies=[];
document.getElementById('open').onclick=function(){{
  window.answer='pending';
  amapAskAccount(()=>new Promise(resolve=>replies.push(resolve)),true).then(v=>window.answer=v);
}};
document.getElementById('remove').onclick=function(){{amapRemoveFromAcl('Synthetic account',this)}};
document.getElementById('unlink').onclick=function(){{amapUnlink('Synthetic account','country','Chile',this)}};
</script></body></html>'''
    context = browser.new_context(viewport={"width": width, "height": 900}, color_scheme=theme)
    page = context.new_page()
    errors, native, posts = [], [], []
    responses = []
    documents = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("dialog", lambda dialog: (native.append(dialog.message), dialog.dismiss()))

    def route(r):
        if r.request.resource_type == "document":
            documents.append(r.request.url)
            return r.fulfill(content_type="text/html", body=html)
        if r.request.method == "POST":
            posts.append((urlsplit(r.request.url).path, json.loads(r.request.post_data)))
            result = responses.pop(0)
            if result == "network":
                return r.abort()
            return r.fulfill(status=result[0], json=result[1])
        r.fulfill(body="")

    page.route("**/*", route)
    page.goto("http://acl142.test/")
    yield page, posts, responses, documents
    context.close()
    assert not errors, errors
    assert not native, native


def reply(page, result):
    page.evaluate("value => replies.shift()(value)", result)


def found():
    return {"ok": True, "act": "act_111111", "account_name": "Synthetic default", "needs_confirmation": True}


def test_default_confirmation_manual_enter_and_dialog_containment(composed):
    page, posts, _, _ = composed
    page.locator("#open").click()
    dialog = page.get_by_role("dialog", name="Ad account", exact=True)
    field = dialog.get_by_label("Ads Manager link or account number", exact=True)
    expect(field).to_be_disabled()
    expect(dialog.get_by_role("button", name="Cancel")).to_be_focused()
    reply(page, found())
    expect(field).to_have_value("act_111111")
    expect(dialog).to_contain_text("Synthetic default")
    expect(dialog.get_by_role("button", name="Connect", exact=True)).to_be_disabled()
    capture = os.environ.get("ACL142_VISUAL_DIR")
    if capture:
        Path(capture).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(capture) / f"account-dialog-{page.viewport_size['width']}.png"))
    tick = dialog.get_by_role("checkbox")
    tick.check()
    expect(dialog.get_by_role("button", name="Connect", exact=True)).to_be_enabled()
    field.fill("act_222222")
    expect(tick).not_to_be_visible()
    box = dialog.bounding_box()
    assert box["x"] >= 0 and box["x"] + box["width"] <= page.viewport_size["width"]
    assert dialog.evaluate("el => el.scrollWidth <= el.clientWidth + 1")
    field.press("Enter")
    page.wait_for_function("answer === 'act_222222'")
    expect(page.locator("#open")).to_be_focused()
    assert posts == []


def test_failed_empty_reread_escape_and_late_response_keep_real_confirmation(composed):
    page, posts, _, _ = composed
    page.locator("#open").click()
    reply(page, {"ok": False, "error": "Synthetic unavailable"})
    dialog = page.get_by_role("dialog", name="Ad account", exact=True)
    expect(dialog).to_contain_text("leave this empty to connect now")
    dialog.get_by_role("button", name="Connect without a number").click()
    page.wait_for_function("answer === ''")
    page.locator("#open").click()
    reply(page, found())
    dialog.get_by_role("checkbox").check()
    dialog.get_by_role("button", name="Read from the open account").click()
    expect(dialog.get_by_label("Ads Manager link or account number")).to_be_disabled()
    expect(dialog.get_by_role("checkbox")).to_be_disabled()
    reply(page, {"ok": False, "error": "Synthetic failed reread"})
    expect(dialog.get_by_role("checkbox")).not_to_be_checked()
    expect(dialog.get_by_role("button", name="Connect", exact=True)).to_be_disabled()
    page.keyboard.press("Escape")
    page.wait_for_function("answer === null")
    expect(page.locator("#open")).to_be_focused()
    page.locator("#open").click()
    page.evaluate("() => {window.duplicate='pending';amapAskAccount(()=>Promise.resolve({}),true).then(v=>window.duplicate=v)}")
    page.wait_for_function("duplicate === null")
    expect(page.locator("dialog")).to_have_count(1)
    page.keyboard.press("Escape")
    reply(page, found())
    expect(page.locator("dialog")).to_have_count(0)
    assert page.evaluate("answer") is None
    assert posts == []


def test_full_removal_cancel_escape_inline_failure_and_explicit_retry(composed):
    page, posts, responses, documents = composed
    button = page.locator("#remove")
    for answer in ("cancel", "escape"):
        button.click()
        dialog = page.get_by_role("dialog", name="Remove Synthetic account from ACL on this computer?")
        expect(dialog).to_contain_text("shared sheet")
        expect(dialog).to_contain_text("Multilogin profile")
        expect(dialog.get_by_role("button", name="Cancel")).to_be_focused()
        if answer == "cancel":
            dialog.get_by_role("button", name="Cancel").click()
        else:
            page.keyboard.press("Escape")
        expect(button).to_be_focused()
        assert posts == []
    responses.append((500, {"ok": False, "error": "Synthetic placement write failed"}))
    button.click()
    page.get_by_role("dialog").get_by_role("button", name="Remove from ACL", exact=True).click()
    expect(page.get_by_role("alert")).to_have_text("Synthetic placement write failed")
    assert page.get_by_role("alert").evaluate("el => el.previousElementSibling.id") == "remove"
    page.get_by_label("Inspect page").fill("Page stays usable")
    responses.append((200, {"ok": True}))
    button.click()
    with page.expect_navigation(wait_until="load"):
        page.get_by_role("dialog").get_by_role("button", name="Remove from ACL", exact=True).click()
    assert len(documents) == 2
    assert posts == [("/account-map-remove", {"account_name": "Synthetic account", "also_unlink": True})] * 2


def test_country_unlink_retains_spend_refusal_and_uses_visible_inline_errors(composed):
    page, posts, responses, _ = composed
    button = page.locator("#unlink")
    responses.append((409, {"ok": False, "needs_confirm": True, "error": "Synthetic account spent today"}))
    button.click()
    dialog = page.get_by_role("dialog")
    expect(dialog).to_contain_text("moves to Available accounts")
    dialog.get_by_role("button", name="Take out", exact=True).click()
    second = page.get_by_role("dialog", name="Take it out anyway?")
    expect(second).to_be_visible()
    second.get_by_role("button", name="Cancel").click()
    expect(button).to_be_enabled()
    expect(button).to_be_focused()
    assert len(posts) == 1 and "confirm" not in posts[0][1]
    for failure in [(500, {"ok": False, "error": "Synthetic unlink error"}), "network"]:
        responses.append(failure)
        button.click()
        page.get_by_role("dialog").get_by_role("button", name="Take out", exact=True).click()
        expect(page.get_by_role("alert")).to_be_visible()
        expect(button).to_be_enabled()
    assert all(path == "/account-links" and body == {"action": "unlink", "kind": "country", "value": "Chile", "account": "Synthetic account"} for path, body in posts)


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("route", ["home", "manage"])
def test_both_removal_controls_are_reachable_in_actual_narrow_account_rows(integrated, theme, route):
    page, scope, _ = integrated(390, theme, route)
    for selector in ("button.amap-x:not(.amap-x-acl)", "button.amap-x-acl"):
        button = scope.locator(selector).first
        expect(button).to_be_visible()
        button.scroll_into_view_if_needed()
        assert button.evaluate("""el => {
          const r=el.getBoundingClientRect();
          const top=document.elementFromPoint(r.x+r.width/2,r.y+r.height/2);
          return r.x>=0 && r.right<=innerWidth && top && (top===el || el.contains(top));
        }""")
        assert ",this)" in button.get_attribute("onclick")
    capture = os.environ.get("ACL142_VISUAL_DIR")
    if capture:
        Path(capture).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(capture) / f"actual-row-{route}-{theme}-390.png"))
