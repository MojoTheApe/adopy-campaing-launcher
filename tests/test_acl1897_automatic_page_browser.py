"""ACL-1897: the actual Automatic option names the Page it will use.

Existing Builder fixtures intercept all requests; no Meta/Multilogin session.
"""
from pathlib import Path
import sys

from playwright.sync_api import expect

sys.path.insert(0, str(Path(__file__).parent))
from test_acl1880_pages_browser import PAGES, browser, install, page  # noqa: E402,F401
from test_builder_duplicate_buttons import page as full_page  # noqa: E402,F401


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


def test_rpa_result_shows_the_selected_page_origin_without_a_warning_or_html(full_page):
    p = full_page
    p.route("**/publish-jobs/automatic-page", lambda route: route.fulfill(json={
        "id": "automatic-page", "status": "done", "publishable": True,
        "done_count": 1, "total": 1, "fb_backend": "rpa", "errors": [],
        "results": [{"campaign_name": "C", "success": True, "publishable": True,
            "page_id": "777", "page_name": "Profile <img src=x>", "page_origin": "profile",
            "warnings": [], "blocking_failures": [], "steps_completed": []}]}))
    p.evaluate("""() => {
      const panel = document.createElement('div'); panel.id = 'automatic-result'; document.body.append(panel);
      cbPollJob('automatic-page', panel, document.createElement('button'));
    }""")
    panel = p.locator("#automatic-result")
    expect(panel).to_contain_text("Facebook Page: Profile <img src=x> (777) · from profile")
    assert panel.locator("img").count() == 0
    assert "Warnings:" not in panel.inner_text()
    assert "draft ready" in panel.inner_text()
