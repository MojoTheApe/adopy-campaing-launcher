"""ACL-2533 / ACL-2571: the background-image picker in a real browser (Chromium), Forms tab and builder panel.

Offline: every /api/* call is answered by the test; no Meta, no Multilogin, no profile.
ACL-2571: the picture of a shared template is chosen WITHOUT an account or a Page, kept on this computer
(`/api/form-templates/cover-file`) and uploaded to each Page only when a campaign is published. Covers what
the node harness cannot: that the preview really decodes (a JPEG and a PNG), that with no file nothing is
displayed, that a Save keeps the file first and never drops it silently, and that a stored picture missing on
this computer says so.
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
        self.stage_reply = {"ok": True, "sha256": "a" * 64, "bytes": 100, "mime": "image/jpeg"}
        self.stages = 0
        self.meta_uploads: list[str] = []       # any request to the Meta-bound route (must stay empty)
        self.stored: bytes | None = None        # what GET cover-file serves (None = 404, "on another computer")
        self.saves: list[dict] = []

    def handle(self, route):
        req = route.request
        url = req.url
        if "/api/account-pages" in url:
            return route.fulfill(json={"ok": True, "state": "ready", "pages": self.pages})
        if "/api/form-photo-upload" in url:
            self.meta_uploads.append(url)
            return route.fulfill(json={"ok": False})
        if "/api/form-templates/cover-file" in url:
            if req.method == "POST":
                self.stages += 1
                return route.fulfill(json=self.stage_reply, status=200 if self.stage_reply.get("ok") else 400)
            if self.stored is None:
                return route.fulfill(status=404, body="")
            return route.fulfill(status=200, body=self.stored, content_type="image/jpeg")
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
def test_the_preview_decodes_a_real_file_with_no_account_or_page_and_is_not_displayed_without_one(browser, kind):
    ctx, pg, api, errors = _open_forms(browser, ["Acc", "Other"])
    try:
        img = _preview(pg)
        # Nothing chosen: no broken image with its alt text.
        assert img.evaluate("e => getComputedStyle(e).display") == "none"
        # No account, no Page: the file input is ready all the same.
        expect(_file_input(pg)).to_be_enabled()
        _file_input(pg).set_input_files(FILES[kind])
        expect(img).to_be_visible()
        assert img.evaluate("e => e.complete && e.naturalWidth") > 0, "the preview did not decode"
        expect(_state(pg)).to_have_text("Chosen (not saved yet)")
        # Remove: the preview is gone and the input does not keep the name of a dropped file.
        pg.locator("#fm-cover").get_by_role("button", name="Remove").click()
        assert img.evaluate("e => getComputedStyle(e).display") == "none"
        assert _file_input(pg).input_value() == ""
        assert api.stages == 0 and api.meta_uploads == [], "choosing a file must send nothing"
        assert not errors, errors
    finally:
        ctx.close()


def test_save_keeps_the_picture_on_this_computer_and_saves_only_its_hash(browser):
    ctx, pg, api, errors = _open_forms(browser, ["Acc", "Other"])
    try:
        _fill_valid(pg)
        _file_input(pg).set_input_files(FILES["png"])
        expect(_state(pg)).to_have_text("Chosen (not saved yet)")
        pg.locator("#fm-save-btn").click()
        expect(pg.locator("#fm-editor")).to_be_hidden()
        assert api.stages == 1, "one request keeps the file on this computer"
        assert api.meta_uploads == [], "the picture must not go to a Page at save"
        assert len(api.saves) == 1
        saved = api.saves[0]
        assert saved["cover_local_sha256"] == "a" * 64
        assert not saved.get("cover_photo_id") and not saved.get("cover_page_id")
        assert len(json.dumps(saved)) < 5000, "no image bytes in the template"
        assert not errors, errors
    finally:
        ctx.close()


def test_save_stops_with_a_visible_error_when_the_picture_cannot_be_kept(browser):
    api = Api()
    api.stage_reply = {"ok": False, "message": "The background image must be a JPEG or PNG file."}
    ctx, pg, api, errors = _open_forms(browser, ["Acc", "Other"], api)
    try:
        _fill_valid(pg)
        _file_input(pg).set_input_files(FILES["jpeg"])
        pg.locator("#fm-save-btn").click()
        expect(pg.locator("#fm-editor-msg")).to_contain_text("The template was not saved.")
        expect(pg.locator("#fm-editor-msg")).to_contain_text("must be a JPEG or PNG")
        expect(_state(pg)).to_contain_text("Not saved:")
        assert api.saves == [], "saved without the chosen picture"
        expect(pg.locator("#fm-editor")).to_be_visible()
        expect(pg.locator("#fm-save-btn")).to_be_enabled()
        assert "WhatsApp" in _file_input(pg).evaluate("e => e.files[0] ? e.files[0].name : ''")
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
        assert api.stages == 0 and len(api.saves) == 1
        assert not api.saves[0].get("cover_photo_id") and not api.saves[0].get("cover_local_sha256")
    finally:
        ctx.close()


def _picker_page(browser, api):
    """The picker alone on a blank page, with the real routes of the dashboard answered by `api`."""
    _panel, _layout, picker, _forms = _forms_source()
    ctx = browser.new_context()
    ctx.route("**/*", api.handle)
    pg = ctx.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.goto("http://acl.test/")
    pg.set_content("<body><div id='box'></div></body>")
    pg.add_script_tag(content=picker)
    pg.evaluate("window.p = window.formCoverPicker(document.getElementById('box'), () => ({account: '', pageId: ''}))")
    return ctx, pg, errors


def test_a_stored_picture_shows_from_this_computer_and_a_missing_one_says_so(browser):
    api = Api()
    api.stored = JPEG
    ctx, pg, errors = _picker_page(browser, api)
    try:
        pg.evaluate("window.p.set('', '', '', 'a'.repeat(64), 'tpl000000001')")
        img = pg.locator("#box img").first
        expect(img).to_be_visible()
        assert img.evaluate("e => e.complete && e.naturalWidth") > 0
        assert img.evaluate("e => e.src").startswith("http://acl.test/api/form-templates/cover-file?id=tpl000000001&sha=")
        assert pg.evaluate("window.p.get().cover_local_sha256") == "a" * 64
        # The same template on a computer without the file (the library has only the hash).
        api.stored = None
        pg.evaluate("window.p.set('', '', '', 'a'.repeat(64), 'tpl000000001')")
        expect(pg.locator("#box [aria-live=polite]").first).to_have_text(
            "Background picture is on another computer \u2014 choose it again here")
        assert img.evaluate("e => getComputedStyle(e).display") == "none"
        assert pg.evaluate("window.p.get().cover_local_sha256") == "a" * 64, "the hash stays until the buyer replaces it"
        # Choosing the file again here replaces the note and sends nothing to Meta.
        pg.locator("#box input[type=file]").set_input_files(FILES["jpeg"])
        expect(img).to_be_visible()
        assert pg.evaluate("window.p.get().cover_local_sha256") == "", "a new file waits for the save to get its hash"
        assert api.meta_uploads == []
        assert not errors, errors
    finally:
        ctx.close()


# ───────────────────────────── builder panel ─────────────────────────────


def _builder_panel(page, pages_payload):
    page.route("**/api/account-pages", lambda r: r.fulfill(json=pages_payload))
    page.add_script_tag(path=str(ACCOUNT_PAGES))
    api = Api()
    page.route("**/api/form-photo-upload", api.handle)
    page.route("**/api/form-templates/cover-file*", api.handle)
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


def test_builder_picture_is_chosen_and_kept_with_no_page_resolved(builder_page):
    page = builder_page
    page.route("**/api/account-pages", lambda r: r.fulfill(json={"ok": True, "state": "empty", "pages": [], "message": "No Facebook Pages available"}))
    page.add_script_tag(path=str(ACCOUNT_PAGES))
    api, panel = _builder_panel(page, {"ok": True, "state": "empty", "pages": [], "message": "No Facebook Pages available"})
    file_input = panel.locator(".cb-fc-cover input[type=file]")
    expect(file_input).to_be_enabled()                      # no Account/Page needed to choose the picture
    file_input.set_input_files(FILES["jpeg"])
    assert panel.locator(".cb-fc-cover img").evaluate("e => e.naturalWidth") > 0
    expect(panel.locator(".cb-fc-cover [aria-live=polite]")).to_have_text("Chosen (not saved yet)")
    panel.get_by_role("button", name="Save and select").click()
    page.wait_for_function("window.cbFormTemplates.length === 1")
    assert api.stages == 1 and api.meta_uploads == []
    assert api.saves[0]["cover_local_sha256"] == "a" * 64 and not api.saves[0].get("cover_photo_id")


def test_builder_stage_failure_stops_the_save(builder_page):
    page = builder_page
    page.route("**/api/account-pages", lambda r: r.fulfill(json={"ok": True, "state": "ready", "pages": PAGES}))
    page.add_script_tag(path=str(ACCOUNT_PAGES))
    api, panel = _builder_panel(page, {"ok": True, "state": "ready", "pages": PAGES})
    api.stage_reply = {"ok": False, "message": "The background image is larger than 8 MB."}
    panel.locator(".cb-fc-cover input[type=file]").set_input_files(FILES["png"])
    panel.get_by_role("button", name="Save and select").click()
    msg = panel.locator("[data-fc-msg]")
    expect(msg).to_contain_text("The form was not saved.")
    expect(msg).to_contain_text("larger than 8 MB")
    expect(panel.locator(".cb-fc-cover [aria-live=polite]")).to_contain_text("Not saved:")
    assert api.saves == [], "saved without the chosen picture"
    expect(panel.get_by_role("button", name="Save and select")).to_be_enabled()
