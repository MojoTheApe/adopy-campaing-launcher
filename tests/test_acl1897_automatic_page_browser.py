"""ACL-1897: the actual Automatic option names the Page it will use.

Existing Builder fixtures intercept all requests; no Meta/Multilogin session.
"""
from pathlib import Path
import sys

from playwright.sync_api import expect

sys.path.insert(0, str(Path(__file__).parent))
from test_acl1880_pages_browser import PAGES, browser, install, page  # noqa: E402,F401


def test_automatic_option_names_the_first_page_and_changes_after_refresh(page):
    install(page, {"ok": True, "state": "ready", "pages": PAGES})
    automatic = page.locator('.cb-page-select option[value=""]').first
    expect(automatic).to_have_text("Automatic: First Page (123)")
    assert page.evaluate("cbResolvedPage(document.querySelector('.cb-slot'))") == "123"
    page.route("**/api/account-pages", lambda route: route.fulfill(json={
        "ok": True, "state": "ready", "pages": list(reversed(PAGES))}))
    page.evaluate("cbRefreshPages(document.querySelector('.cb-slot'), true)")
    expect(automatic).to_have_text("Automatic: Second Page (456)")
    assert page.evaluate("cbResolvedPage(document.querySelector('.cb-slot'))") == "456"
    assert page.locator('[name="camp_0_page_id"]').input_value() == "", "Automatic remains a campaign-local empty choice"
