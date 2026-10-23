"""ACL-141: real page dialogs and the newly composed Forms/refresh callers.

Only synthetic rows and intercepted requests are used. The component and caller
scripts are read from the actual product, rather than replaced with dialog stubs.
"""
import os
import re
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright


TEMPLATES = Path(__file__).resolve().parents[1] / "src/web/templates"
REPORT = TEMPLATES / "report.html"


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        instance = pw.chromium.launch(executable_path=os.environ.get("ACL_BROWSER_EXECUTABLE") or None)
        yield instance
        instance.close()


@pytest.fixture
def page(browser):
    context = browser.new_context()
    context.route("**/*", lambda route: route.fulfill(content_type="text/html", body=""))
    pg = context.new_page()
    errors, native = [], []
    pg.on("pageerror", lambda error: errors.append(str(error)))
    pg.on("dialog", lambda dialog: (native.append(dialog.message), dialog.dismiss()))
    pg.goto("http://acl141.test/")
    pg.set_content('<button id="opener">Open</button><input id="inspect" aria-label="Inspect page">')
    pg.add_script_tag(content=(TEMPLATES / "_acl_dialog.js").read_text(encoding="utf-8"))
    pg.evaluate("document.getElementById('opener').focus()")
    yield pg
    context.close()
    assert not errors, errors
    assert not native, native


def test_prompt_has_a_label_safe_text_enter_and_restored_focus(page):
    page.evaluate("""() => {
      window.answer = 'pending';
      aclPrompt('Use <b>literal</b> text', {
        title: 'Account <img src=x>', label: 'Ad account name', value: 'Existing name'
      }).then(value => window.answer = value);
    }""")
    dialog = page.get_by_role("dialog", name="Account <img src=x>")
    expect(dialog).to_be_visible()
    expect(dialog).to_contain_text("Use <b>literal</b> text")
    expect(dialog.locator("img, b")).to_have_count(0)
    field = page.get_by_label("Ad account name", exact=True)
    expect(field).to_be_focused()
    assert field.evaluate("el => [el.selectionStart, el.selectionEnd]") == [0, len("Existing name")]
    field.fill("Confirmed name")
    field.press("Enter")
    page.wait_for_function("window.answer === 'Confirmed name'")
    expect(dialog).to_have_count(0)
    expect(page.locator("#opener")).to_be_focused()


def test_danger_confirm_focuses_cancel_keeps_background_inert_and_escape_refuses(page):
    page.evaluate("""() => {
      window.answer = 'pending';
      aclConfirm('Nothing else changes.', {title: 'Remove example?', danger: true})
        .then(value => window.answer = value);
    }""")
    dialog = page.get_by_role("dialog", name="Remove example?")
    expect(dialog.get_by_role("button", name="Cancel")).to_be_focused()
    page.locator("#inspect").evaluate("el => el.focus()")
    expect(dialog.get_by_role("button", name="Cancel")).to_be_focused()
    for _ in range(4):
        page.keyboard.press("Tab")
        # Native dialogs may tab to the browser's chrome (activeElement=body),
        # but background controls must remain inert throughout.
        expect(page.locator("#inspect")).not_to_be_focused()
        expect(page.locator("#opener")).not_to_be_focused()
    dialog.get_by_role("button", name="Cancel").focus()
    page.keyboard.press("Escape")
    page.wait_for_function("window.answer === false")
    expect(dialog).to_have_count(0)
    expect(page.locator("#opener")).to_be_focused()


def test_duplicate_confirm_refuses_and_different_dialog_waits_in_order(page):
    page.evaluate("""() => {
      window.answers = [];
      aclConfirm('First?').then(value => answers.push(['first', value]));
      aclConfirm('First?').then(value => answers.push(['duplicate', value]));
      aclPrompt('', {title: 'Second?', label: 'Number'}).then(value => answers.push(['second', value]));
    }""")
    first = page.get_by_role("dialog", name="First?")
    expect(first).to_be_visible()
    expect(page.locator("dialog")).to_have_count(1)
    page.wait_for_function("window.answers.some(x => x[0] === 'duplicate' && x[1] === false)")
    first.get_by_role("button", name="Continue").click()
    second = page.get_by_role("dialog", name="Second?")
    expect(second).to_be_visible()
    page.keyboard.press("Escape")
    page.wait_for_function("window.answers.length === 3")
    assert page.evaluate("window.answers") == [["duplicate", False], ["first", True], ["second", None]]


def test_refresh_failure_stays_inline_and_retry_clears_it_without_blocking_inspection(page):
    source = REPORT.read_text(encoding="utf-8")
    start = source.index("async function dashRefresh(ev)")
    end = source.index("// ── Update to a newer ACL release", start)
    page.locator("body").evaluate("el => el.insertAdjacentHTML('beforeend', '<button id=\"dash-refresh-btn\">Refresh</button>')")
    page.add_script_tag(content=source[start:end])
    page.evaluate("""() => {
      window.fetch = () => new Promise(resolve => window.refreshReply = resolve);
      document.getElementById('dash-refresh-btn').onclick = dashRefresh;
    }""")
    refresh = page.get_by_role("button", name="Refresh", exact=True)
    refresh.click()
    page.evaluate("refreshReply({ok: false, json: async () => ({error: 'Synthetic first failure'})})")
    error = page.get_by_role("alert")
    expect(error).to_have_text("Refresh failed: Synthetic first failure")
    assert error.evaluate("el => el.previousElementSibling.id") == "dash-refresh-btn"
    page.get_by_label("Inspect page").fill("I can inspect the page")
    expect(page.locator("dialog")).to_have_count(0)
    refresh.click()
    expect(error).to_have_count(0)
    page.evaluate("refreshReply({ok: false, json: async () => ({error: 'Synthetic second failure'})})")
    expect(page.get_by_role("alert")).to_have_count(1)
    expect(page.get_by_role("alert")).to_have_text("Refresh failed: Synthetic second failure")
    expect(refresh).to_be_enabled()


def _forms(page, scope):
    source = REPORT.read_text(encoding="utf-8")
    panel_start = source.index('<div id="view-forms"')
    panel_end = source.index('<div id="view-home"', panel_start)
    script_start = source.index("// ── Forms: общая библиотека шаблонов Instant Form")
    script_end = source.index("</script>", script_start)
    row = {"id": "aaaaaaaaaaaa", "name": "Synthetic <form>", "locale": "EN_US", "summary": "Name, Email"}
    state = {"rows": [row], "posts": [], "loads": 0}

    def api(route):
        request = route.request
        if request.method == "POST":
            state["posts"].append({"url": request.url, "body": request.post_data_json})
            if request.url.endswith("/delete"):
                state["rows"] = []
            route.fulfill(json={"ok": True})
        else:
            state["loads"] += 1
            route.fulfill(json={"ok": True, "rows": state["rows"], "templates": [], "deleted": [],
                                "library": {"connected": scope == "team", "ok": True}, "migration": {}})

    page.route(re.compile(r"/api/form-templates(?:/|\?|$)"), api)
    page.locator("body").evaluate("(el, html) => el.insertAdjacentHTML('beforeend', html)", source[panel_start:panel_end])
    page.add_script_tag(content=source[script_start:script_end])
    page.evaluate("document.dispatchEvent(new CustomEvent('acl:viewchange', {detail: {view: 'forms'}}))")
    expect(page.locator('[data-fm-del="aaaaaaaaaaaa"]')).to_be_visible()
    return state


@pytest.mark.parametrize("scope", ["local", "team"])
@pytest.mark.parametrize("answer", ["cancel", "escape", "accept"])
def test_forms_delete_uses_named_page_confirmation_and_only_explicit_acceptance_writes(page, scope, answer):
    state = _forms(page, scope)
    delete = page.locator('[data-fm-del="aaaaaaaaaaaa"]')
    delete.click()
    where = "from this computer" if scope == "local" else "for the whole team"
    dialog = page.get_by_role("dialog", name=f"Delete «Synthetic <form>» {where}?")
    expect(dialog).to_be_visible(timeout=2000)
    expect(dialog).to_contain_text("Recently deleted")
    expect(dialog).to_contain_text("Forms already created on Pages are not touched.")
    expect(dialog.get_by_role("button", name="Cancel")).to_be_focused()
    assert state["posts"] == []
    if answer == "escape":
        page.keyboard.press("Escape")
    else:
        dialog.get_by_role("button", name="Delete" if answer == "accept" else "Cancel", exact=True).click()
    expect(dialog).to_have_count(0)
    if answer == "accept":
        expect(page.locator("#fm-msg")).to_contain_text("Deleted. It is in Recently deleted")
        assert len(state["posts"]) == 1
        assert state["posts"][0]["url"].endswith("/api/form-templates/delete")
        assert state["posts"][0]["body"] == {"id": "aaaaaaaaaaaa"}
    else:
        expect(delete).to_be_focused()
        assert state["posts"] == []


def test_forms_delete_double_click_cannot_send_two_deletes(page):
    state = _forms(page, "team")
    page.locator('[data-fm-del="aaaaaaaaaaaa"]').evaluate("el => { el.click(); el.click(); }")
    dialog = page.get_by_role("dialog")
    expect(dialog).to_have_count(1)
    dialog.get_by_role("button", name="Delete", exact=True).click()
    expect(page.locator("#fm-msg")).to_contain_text("Deleted.")
    assert len(state["posts"]) == 1


def test_forms_delete_does_not_send_a_stale_confirmation_after_background_refresh(page):
    state = _forms(page, "team")
    page.locator('[data-fm-del="aaaaaaaaaaaa"]').click()
    dialog = page.get_by_role("dialog")
    expect(dialog).to_be_visible(timeout=2000)
    # A real library refresh replaces the list behind the modal. The confirmation
    # names the old row; accepting it must not act on the changed list.
    state["rows"] = []
    page.evaluate("document.dispatchEvent(new CustomEvent('acl:viewchange', {detail: {view: 'forms'}}))")
    expect(page.locator('[data-fm-del="aaaaaaaaaaaa"]')).to_have_count(0)
    dialog.get_by_role("button", name="Delete", exact=True).click()
    expect(dialog).to_have_count(0)
    assert state["posts"] == []
