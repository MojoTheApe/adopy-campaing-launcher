"""ACL-2533 round 3: the background-image picker in a real browser (Chromium), Forms tab and builder panel.

Offline: every /api/* call is answered by the test; no Meta, no Multilogin, no profile.
Covers what the node harness cannot: that the preview really decodes (a JPEG and a PNG), that with no
file nothing is displayed, that the owner's sequence (file first, then Account, Refresh pages, Page)
keeps the file, and that a Save never drops a chosen file silently.
"""
from __future__ import annotations

import base64
import json
import os
import re
import struct
import sys
import zlib
from pathlib import Path

import pytest
from jinja2 import Environment
from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).parent))
from test_builder_review_browser import page as builder_page  # noqa: E402,F401  (the builder fixture)

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "src" / "web" / "templates" / "report.html"
ACCOUNT_PAGES = ROOT / "src" / "web" / "static" / "account_pages.js"
PAGES = [{"id": "123", "name": "First Page"}, {"id": "456", "name": "Second Page"}]


def _png() -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
    raw = b"\x00" + b"\xff\x00\x00" * 4                      # one row, four red pixels
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 4, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAP//////////////////////////////////////////////////////////////"
    "////////////////////////wgALCAABAAEBAREA/8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABPxA=")
FILES = {
    "jpeg": {"name": "WhatsApp Image 2026-11-10 at 18.24.06.jpeg", "mimeType": "image/jpeg", "buffer": JPEG},
    "png": {"name": "cover.png", "mimeType": "image/png", "buffer": _png()},
}


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=os.environ.get("ACL_BROWSER_EXECUTABLE") or None)
        yield b
        b.close()


class Api:
    """Answers the dashboard's /api calls and remembers what was sent."""

    def __init__(self):
        self.pages = PAGES
        self.upload_reply = {"ok": True, "state": "uploaded", "photo_id": "777", "page_id": "123", "sha256": "a" * 64}
        self.uploads: list[dict] = []
        self.saves: list[dict] = []

    def handle(self, route):
        req = route.request
        url = req.url
        if "/api/account-pages" in url:
            return route.fulfill(json={"ok": True, "state": "ready", "pages": self.pages})
        if "/api/form-photo-upload" in url:
            body = req.post_data_buffer or b""
            fields = dict(re.findall(rb'name="(account_label|page_id)"\r\n\r\n([^\r]*)', body))
            self.uploads.append({k.decode(): v.decode() for k, v in fields.items()})
            return route.fulfill(json=self.upload_reply)
        if "/api/form-templates" in url and req.method == "POST":
            data = json.loads(req.post_data or "{}")
            self.saves.append(data)
            return route.fulfill(json={"ok": True, "template": {**data, "id": "tpl000000001"}})
        if "/api/form-templates" in url:
            return route.fulfill(json={"ok": True, "rows": [], "templates": [], "deleted": [],
                                       "library": {"connected": True, "ok": True}, "migration": {"available": False}})
        return route.fulfill(json={})


# ───────────────────────────── Forms tab ─────────────────────────────


def _forms_source():
    src = REPORT.read_text(encoding="utf-8")
    a = src.index('<div id="view-forms"')
    panel = Environment().from_string(src[a: src.index('<div id="view-home"', a)]).render()
    li = src.index("window.cbLayout = (function () {")
    layout = src[li: src.index("\n})();", li)] + "\n})();"
    mark = src.index("// ── Forms: общая библиотека шаблонов Instant Form (ACL-2054)")
    forms = src[mark: src.index("</script>", mark)]
    picker_start = src.index("  window.formCoverPicker = function")
    picker = src[picker_start: src.index("  function coverContext(panel) {", picker_start)]
    return panel, layout, picker, forms


def _open_forms(browser, labels, api=None):
    api = api or Api()
    panel, layout, picker, forms = _forms_source()
    ctx = browser.new_context()
    ctx.route("**/*", api.handle)
    pg = ctx.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.goto("http://acl.test/")
    pg.set_content(f"<body>{panel}</body>")
    pg.evaluate("labels => { window.aclAllAccountLabels = () => labels; window.aclConfirm = () => Promise.resolve(true); }", labels)
    pg.add_script_tag(content="(function(){var window_=window;})();\n" + layout.replace("window.cbLayout = (function", "window.cbLayout = (function", 1))
    pg.add_script_tag(content=picker.replace("  window.formCoverPicker = function", "window.formCoverPicker = function", 1) if False else picker)
    pg.add_script_tag(content=forms)
    pg.evaluate("document.dispatchEvent(new CustomEvent('acl:viewchange', {detail: {view: 'forms'}}))")
    pg.evaluate("document.getElementById('view-forms').classList.add('active')")
    pg.locator("#fm-new-btn").click()
    expect(pg.locator("#fm-editor")).to_be_visible()
    return ctx, pg, api, errors


def _fill_valid(pg):
    pg.fill("#fm-name", "Cover test")
    pg.fill("#fm-headline", "Get more information")
    pg.fill("#fm-intro", "Tell us more.")
    pg.fill("#fm-privacy", "https://example.com/privacy")
    pg.fill("#fm-thanks", "Thank you.")


def _preview(pg, root="#fm-cover"):
    return pg.locator(f"{root} img").first


def _file_input(pg, root="#fm-cover"):
    return pg.locator(f"{root} input[type=file]")


def _state(pg, root="#fm-cover"):
    return pg.locator(f"{root} [aria-live=polite]").first


@pytest.mark.parametrize("kind", ["jpeg", "png"])
def test_the_preview_decodes_a_real_file_and_is_not_displayed_without_one(browser, kind):
    ctx, pg, api, errors = _open_forms(browser, ["Acc"])
    try:
        pg.select_option("#fm-cover-account", "Acc")
        pg.locator("#fm-cover-pages-btn").click()
        pg.select_option("#fm-cover-page", "123")
        img = _preview(pg)
        # Nothing chosen: no broken image with its alt text.
        assert img.evaluate("e => getComputedStyle(e).display") == "none"
        _file_input(pg).set_input_files(FILES[kind])
        expect(img).to_be_visible()
        assert img.evaluate("e => e.complete && e.naturalWidth") > 0, "the preview did not decode"
        expect(_state(pg)).to_have_text("Chosen (not uploaded yet)")
        expect(pg.get_by_role("button", name="Upload to Page")).to_be_enabled()
        # Remove: the preview is gone and the input does not keep the name of a dropped file.
        pg.locator("#fm-cover").get_by_role("button", name="Remove").click()
        assert img.evaluate("e => getComputedStyle(e).display") == "none"
        assert _file_input(pg).input_value() == ""
        assert not errors, errors
    finally:
        ctx.close()


def test_the_owner_sequence_keeps_the_file_and_the_upload_button(browser):
    """No Account/Page -> a file -> Account -> Refresh pages -> Page: the file is still chosen."""
    ctx, pg, api, errors = _open_forms(browser, ["Acc", "Other"])
    try:
        # Page first: with two accounts and nothing chosen the file input waits and says why.
        expect(_file_input(pg)).to_be_disabled()
        expect(pg.locator("#fm-cover")).to_contain_text("Choose an account and a Page first (Refresh pages).")
        # The owner's file got in anyway (an input that is disabled cannot be filled by a user, so force it).
        pg.evaluate("document.querySelector('#fm-cover input[type=file]').disabled = false")
        _file_input(pg).set_input_files(FILES["jpeg"])
        expect(_preview(pg)).to_be_visible()
        pg.select_option("#fm-cover-account", "Acc")
        pg.locator("#fm-cover-pages-btn").click()
        expect(pg.locator("#fm-cover-page option")).to_have_count(3)
        pg.select_option("#fm-cover-page", "123")
        # Still there: the name in the input, a decoded preview, the label, an enabled button.
        assert "WhatsApp" in _file_input(pg).evaluate("e => e.files[0] ? e.files[0].name : ''")
        assert _preview(pg).evaluate("e => e.naturalWidth") > 0
        expect(_state(pg)).to_have_text("Chosen (not uploaded yet)")
        expect(pg.get_by_role("button", name="Upload to Page")).to_be_enabled()
        expect(_file_input(pg)).to_be_enabled()
        pg.get_by_role("button", name="Upload to Page").click()
        expect(_state(pg)).to_contain_text("Uploaded to Page")
        assert api.uploads == [{"account_label": "Acc", "page_id": "123"}]
        assert not errors, errors
    finally:
        ctx.close()


def test_one_obvious_account_and_page_are_preselected_and_the_file_input_is_ready(browser):
    api = Api()
    api.pages = [PAGES[0]]
    ctx, pg, api, errors = _open_forms(browser, ["Only"], api)
    try:
        expect(pg.locator("#fm-cover-account")).to_have_value("Only")
        expect(pg.locator("#fm-cover-page")).to_have_value("123")
        expect(_file_input(pg)).to_be_enabled()
        assert not errors, errors
    finally:
        ctx.close()


def test_save_with_a_pending_file_uploads_it_first_and_saves_the_pair(browser):
    ctx, pg, api, errors = _open_forms(browser, ["Acc", "Other"])
    try:
        pg.select_option("#fm-cover-account", "Acc")
        pg.locator("#fm-cover-pages-btn").click()
        expect(pg.locator("#fm-cover-page option")).to_have_count(3)
        pg.select_option("#fm-cover-page", "123")
        _fill_valid(pg)
        _file_input(pg).set_input_files(FILES["png"])
        expect(_state(pg)).to_have_text("Chosen (not uploaded yet)")
        pg.locator("#fm-save-btn").click()
        expect(pg.locator("#fm-editor")).to_be_hidden()
        assert api.uploads == [{"account_label": "Acc", "page_id": "123"}], "one upload, through the one route"
        assert len(api.saves) == 1
        assert (api.saves[0]["cover_photo_id"], api.saves[0]["cover_page_id"], api.saves[0]["cover_sha256"]) == ("777", "123", "a" * 64)
        assert not errors, errors
    finally:
        ctx.close()


def test_save_stops_with_a_visible_error_when_the_upload_fails(browser):
    api = Api()
    api.upload_reply = {"ok": False, "state": "refused", "message": "Meta refused the background image: Invalid image"}
    ctx, pg, api, errors = _open_forms(browser, ["Acc", "Other"], api)
    try:
        pg.select_option("#fm-cover-account", "Acc")
        pg.locator("#fm-cover-pages-btn").click()
        expect(pg.locator("#fm-cover-page option")).to_have_count(3)
        pg.select_option("#fm-cover-page", "123")
        _fill_valid(pg)
        _file_input(pg).set_input_files(FILES["jpeg"])
        pg.locator("#fm-save-btn").click()
        expect(pg.locator("#fm-editor-msg")).to_contain_text("The template was not saved.")
        expect(pg.locator("#fm-editor-msg")).to_contain_text("Meta refused the background image")
        expect(_state(pg)).to_contain_text("Upload failed: Meta refused the background image")
        assert api.saves == [], "saved without the chosen picture"
        expect(pg.locator("#fm-editor")).to_be_visible()
        expect(pg.locator("#fm-save-btn")).to_be_enabled()
        assert "WhatsApp" in _file_input(pg).evaluate("e => e.files[0] ? e.files[0].name : ''")
        assert not errors, errors
    finally:
        ctx.close()


def test_save_without_a_page_is_a_visible_blocking_error_with_the_fix(browser):
    ctx, pg, api, errors = _open_forms(browser, ["Acc", "Other"])
    try:
        _fill_valid(pg)
        pg.evaluate("document.querySelector('#fm-cover input[type=file]').disabled = false")
        _file_input(pg).set_input_files(FILES["jpeg"])
        pg.locator("#fm-save-btn").click()
        alert = pg.locator("#fm-cover [role=alert]")
        expect(alert).to_be_visible()
        expect(alert).to_contain_text("Refresh pages and choose a Page")
        expect(pg.locator("#fm-editor-msg")).to_contain_text("Refresh pages and choose a Page")
        assert api.saves == [] and api.uploads == [], "nothing may be sent without a Page"
        expect(_state(pg)).to_have_text("Chosen (not uploaded yet)")
        assert not errors, errors
    finally:
        ctx.close()


def test_save_without_a_picture_is_unchanged(browser):
    ctx, pg, api, errors = _open_forms(browser, ["Acc", "Other"])
    try:
        pg.fill("#fm-name", "No cover")
        pg.fill("#fm-headline", "Get more information")
        pg.fill("#fm-privacy", "https://example.com/privacy")
        pg.fill("#fm-thanks", "Thank you.")
        pg.locator("#fm-save-btn").click()
        expect(pg.locator("#fm-editor")).to_be_hidden()
        assert api.uploads == [] and len(api.saves) == 1
        assert not api.saves[0].get("cover_photo_id")
    finally:
        ctx.close()


# ───────────────────────────── builder panel ─────────────────────────────


def _builder_panel(page, pages_payload):
    page.route("**/api/account-pages", lambda r: r.fulfill(json=pages_payload))
    page.add_script_tag(path=str(ACCOUNT_PAGES))
    api = Api()
    page.route("**/api/form-photo-upload", api.handle)
    page.route("**/api/form-templates", api.handle)
    page.evaluate("window.cbFormTemplates = []; window.cbFillTemplateSelects = () => {};")
    page.set_default_timeout(8000)
    page.add_script_tag(content=_forms_source()[1])          # the builder page defines cbLayout earlier; the fixture cuts it off
    # The fixture keeps the ad card collapsed; show the card the way the open card shows it.
    page.evaluate("""() => {
      const b = document.querySelector('.cb-ad-row button[onclick^="cbFormCreateOpen"]');
      for (let e = b; e && e !== document.body; e = e.parentElement) {
        e.hidden = false;
        if (getComputedStyle(e).display === 'none') e.style.display = 'block';
      }
    }""")
    page.locator(".cb-ad-row").first.locator("button", has_text="Create and save form").first.click()
    panel = page.locator(".cb-form-create").first
    expect(panel).to_be_visible()
    panel.locator(".cb-fc-name").fill("Builder cover")
    panel.locator(".cb-fc-headline").fill("Get more information")
    panel.locator(".cb-fc-intro").fill("Tell us more.")
    panel.locator(".cb-fc-privacy").fill("https://example.com/privacy")
    panel.locator(".cb-fc-thanks").fill("Thank you.")
    return api, panel


def test_builder_slot_with_an_automatic_page_uploads_for_the_resolved_page(builder_page):
    page = builder_page
    page.route("**/api/account-pages", lambda r: r.fulfill(json={"ok": True, "state": "ready", "pages": PAGES}))
    page.add_script_tag(path=str(ACCOUNT_PAGES))
    page.wait_for_function("document.querySelector('[data-page-note]').textContent.includes('First Page')")
    # The slot's own Page field is empty (Automatic); the resolved Page is what the picture goes to.
    assert page.locator('[name="camp_0_page_id"]').input_value() == ""
    api, panel = _builder_panel(page, {"ok": True, "state": "ready", "pages": PAGES})
    expect(panel.locator(".cb-fc-cover input[type=file]")).to_be_enabled()
    panel.locator(".cb-fc-cover input[type=file]").set_input_files(FILES["jpeg"])
    assert panel.locator(".cb-fc-cover img").evaluate("e => e.naturalWidth") > 0
    expect(panel.locator(".cb-fc-cover [aria-live=polite]")).to_have_text("Chosen (not uploaded yet)")
    panel.get_by_role("button", name="Save and select").click()
    page.wait_for_function("window.cbFormTemplates.length === 1")
    assert api.uploads == [{"account_label": "Demo", "page_id": "123"}]
    assert (api.saves[0]["cover_photo_id"], api.saves[0]["cover_page_id"]) == ("777", "123")


def test_builder_pending_file_and_upload_failure_stops_the_save(builder_page):
    page = builder_page
    page.route("**/api/account-pages", lambda r: r.fulfill(json={"ok": True, "state": "ready", "pages": PAGES}))
    page.add_script_tag(path=str(ACCOUNT_PAGES))
    page.wait_for_function("document.querySelector('[data-page-note]').textContent.includes('First Page')")
    api, panel = _builder_panel(page, {"ok": True, "state": "ready", "pages": PAGES})
    api.upload_reply = {"ok": False, "state": "refused", "message": "Meta refused the background image: Invalid image"}
    page.unroute("**/api/form-photo-upload")
    page.route("**/api/form-photo-upload", api.handle)
    panel.locator(".cb-fc-cover input[type=file]").set_input_files(FILES["png"])
    panel.get_by_role("button", name="Save and select").click()
    msg = panel.locator("[data-fc-msg]")
    expect(msg).to_contain_text("The form was not saved.")
    expect(msg).to_contain_text("Meta refused the background image")
    expect(panel.locator(".cb-fc-cover [aria-live=polite]")).to_contain_text("Upload failed:")
    assert api.saves == [], "saved without the chosen picture"
    expect(panel.get_by_role("button", name="Save and select")).to_be_enabled()


def test_builder_slot_without_a_page_waits_and_blocks_the_save_with_the_fix(builder_page):
    page = builder_page
    page.route("**/api/account-pages", lambda r: r.fulfill(json={"ok": True, "state": "empty", "pages": [], "message": "No Facebook Pages available"}))
    page.add_script_tag(path=str(ACCOUNT_PAGES))
    api, panel = _builder_panel(page, {"ok": True, "state": "empty", "pages": [], "message": "No Facebook Pages available"})
    expect(panel.locator(".cb-fc-cover input[type=file]")).to_be_disabled()
    expect(panel.locator(".cb-fc-cover")).to_contain_text("Choose an account and a Page first (Refresh pages).")
    panel.locator(".cb-fc-cover input[type=file]").evaluate("e => { e.disabled = false; }")
    panel.locator(".cb-fc-cover input[type=file]").set_input_files(FILES["jpeg"])
    panel.get_by_role("button", name="Save and select").click()
    alert = panel.locator(".cb-fc-cover [role=alert]")
    expect(alert).to_be_visible()
    expect(alert).to_contain_text("Refresh pages and choose a Page")
    expect(panel.locator("[data-fc-msg]")).to_contain_text("Refresh pages and choose a Page")
    assert api.saves == [] and api.uploads == []
