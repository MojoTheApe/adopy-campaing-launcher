"""ACL-1437: the job panel shows campaign problems as the owner's mock-up.

Renders the real dashboard template headless and polls synthetic job
snapshots built from real 2026-10-01 failure strings — no live IO.
"""
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest
from playwright.sync_api import expect, sync_playwright
from test_builder_per_account import _render

from src.fb_ads_rpa import failure_policy as fp
from src.fb_ads_rpa.ads_manager_flow import apply_step_failures
from src.fb_ads_rpa.draft import RpaPublishResult

CAMPAIGN = "CO_bnc_All_Video_FX_Y_35_01.10"
CONVERSION = "conversion_event"
AD_COPY = "ad_copy:1741 - Pro Bancolombia has no text"
SECOND_TEXT = "ad2_second_text:2046 - Story14 second primary text not added (add_control_not_found)"


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=os.environ.get("ACL_BROWSER_EXECUTABLE") or None)
        yield b
        b.close()


def _result(failures, *, success=True):
    r = RpaPublishResult(campaign_name=CAMPAIGN, success=success)
    apply_step_failures(r, failures)
    r.run_id = "cc8fcfa17ae9"
    r.campaign_id = "120200000000001"
    r.screenshot_paths = [r"C:\acl\output\rpa_pages\cc8fcfa17ae9\ad_copy.png"]  # linux-ok: a Windows path as opaque data, never touched on disk
    r.steps_completed.insert(0, "campaign_created")
    return r.to_job_dict()


def _panel(browser, result, *, publishable=False):
    html = _render({"Demo": []})
    context = browser.new_context()
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    job = {"status": "done", "publishable": publishable, "total": 1, "done_count": 1,
           "results": [result], "errors": []}

    def route(r):
        if r.request.resource_type == "document":
            r.fulfill(content_type="text/html", body=html)
        elif urlparse(r.request.url).path.startswith("/publish-jobs/"):
            r.fulfill(json=job)
        else:
            r.fulfill(json={"files": [], "texts": [], "profiles": [], "waiting": False})

    page.route("**/*", route)
    page.goto("http://acl.test/")
    page.evaluate("""() => {
      const p=document.querySelector('.cb-status-panel');
      p.style.display='block';
      cbPollJob('synthetic-job',p,null);
    }""")
    panel = page.locator(".cb-status-panel").first
    expect(panel.locator(".cb-status-title")).to_contain_text("1/1")
    return context, page, panel, errors


def test_two_blocking_and_one_warning_render_as_the_mockup(browser):
    context, page, panel, errors = _panel(browser, _result([CONVERSION, AD_COPY, SECOND_TEXT]))
    try:
        block = panel.locator(".cb-problems")
        expect(block).to_have_count(1)
        assert "cb-problems--blocking" in block.get_attribute("class")
        head = block.locator(".cb-problems-head")
        expect(head).to_have_text(f"⛔ {CAMPAIGN} — do not publish as is")
        expect(block.locator(".cb-problems-sub")).to_have_text(
            "2 problems · 1 warning · draft saved, browser left open")

        items = block.locator("ol > li.cb-problem")
        expect(items).to_have_count(3)
        expect(items.nth(0).locator(".cb-problem-title")).to_have_text("Conversion event — not verified")
        expect(items.nth(0).locator(".cb-problem-impact")).to_have_text(
            "Ads may optimise for the wrong event and waste budget.")
        expect(items.nth(0).locator(".cb-problem-action")).to_have_text(
            '→ Check "Conversion event" on the ad set.')
        expect(items.nth(1).locator(".cb-problem-title")).to_have_text(
            'Ad text — ad 1 "1741 - Pro Bancolombia"')
        expect(items.nth(1).locator(".cb-problem-impact")).to_have_text(
            "Primary text / headline not confirmed.")
        expect(items.nth(1).locator(".cb-problem-action")).to_have_text(
            "→ Open the ad and check the text.")
        expect(items.nth(2).locator(".cb-problem-title")).to_have_text(
            'Second primary text — ad 2 "2046 - Story14"')

        # Red for blocking, amber for the warning.
        sev = [items.nth(i).get_attribute("class") for i in range(3)]
        assert "cb-problem--blocking" in sev[0] and "cb-problem--blocking" in sev[1]
        assert "cb-problem--warning" in sev[2]
        colors = page.evaluate("""() => {
          const css = getComputedStyle(document.documentElement);
          const probe = (v) => { const d=document.createElement('div'); d.style.color=v;
            document.body.appendChild(d); const c=getComputedStyle(d).color; d.remove(); return c; };
          const li = [...document.querySelectorAll('.cb-problem')];
          return {bad: probe('var(--bad)'), warn: probe('var(--warn)'),
                  items: li.map(e => getComputedStyle(e).borderLeftColor)};
        }""")
        assert colors["items"] == [colors["bad"], colors["bad"], colors["warn"]]

        # Technical details: collapsed, with codes, notes, run id, capture path.
        details = block.locator("details.cb-problems-tech")
        expect(details).to_have_count(1)
        assert details.get_attribute("open") is None
        expect(details.locator("summary")).to_contain_text("Technical details")
        tech = details.locator(".cb-problems-tech-body")
        body = tech.text_content()
        for needed in ("conversion_event", "ad_copy", "1741 - Pro Bancolombia has no text",
                       "ad2_second_text", "add_control_not_found", "cc8fcfa17ae9",
                       r"output\rpa_pages\cc8fcfa17ae9\ad_copy.png", "120200000000001"):  # linux-ok: a relative Windows path as opaque data, never touched on disk
            assert needed in body, needed
        # The builder card is off-screen in this harness, so toggle by event.
        details.locator("summary").dispatch_event("click")
        assert details.evaluate("d => d.open") is True

        # No duplicated lists: the old paragraph and the two old lists are gone.
        text = panel.inner_text()
        assert "DO NOT PUBLISH AS IS" not in text
        assert "Do not publish:" not in text
        assert "Warnings:" not in text
        expect(panel.locator(".cb-status-err")).to_have_count(0)
        # Стандартный абзац `error` повторяет список — отдельной строки нет.
        expect(block.locator(".cb-problems-error")).to_have_count(0)
        assert not errors, errors
    finally:
        context.close()


def test_warnings_only_reads_ready_to_publish_in_amber(browser):
    result = _result([SECOND_TEXT])
    result["error"] = "Draft ready — browser left open, Meta Publish was not clicked"
    context, page, panel, errors = _panel(browser, result, publishable=True)
    try:
        block = panel.locator(".cb-problems")
        assert "cb-problems--warning" in block.get_attribute("class")
        expect(block.locator(".cb-problems-head")).to_have_text(
            f"⚠️ {CAMPAIGN} — ready to publish (with warnings)")
        expect(block.locator(".cb-problems-sub")).to_have_text(
            "1 warning · draft saved, browser left open")
        expect(block.locator("li.cb-problem")).to_have_count(1)
        assert not errors, errors
    finally:
        context.close()


def test_a_run_that_built_nothing_says_not_built(browser):
    r = RpaPublishResult(campaign_name=CAMPAIGN, success=False)
    r.blocking_failures.append("account_blocked")
    r.error = "account_blocked: Verification required to advertise"
    context, page, panel, errors = _panel(browser, r.to_job_dict())
    try:
        block = panel.locator(".cb-problems")
        expect(block.locator(".cb-problems-head")).to_have_text(f"❌ {CAMPAIGN} — not built")
        expect(block.locator(".cb-problems-sub")).to_have_text(
            "1 problem · no complete draft, check Ads Manager")
        expect(block.locator(".cb-problem-title")).to_have_text("Ad account restricted by Meta")
        # Ревью F2: текст из `error`, которого нет в списке, — в Technical details.
        expect(block.locator(".cb-problems-tech-body .cb-problems-error")).to_have_text(
            "error: account_blocked: Verification required to advertise")
        assert not errors, errors
    finally:
        context.close()


def test_an_old_record_without_the_list_renders_the_old_text(browser):
    old = _result([CONVERSION, AD_COPY, SECOND_TEXT])
    old.pop("problems")
    old.pop("run_id")
    context, page, panel, errors = _panel(browser, old)
    try:
        expect(panel.locator(".cb-problems")).to_have_count(0)
        expect(panel.locator(".cb-status-err").first).to_contain_text("DO NOT PUBLISH AS IS")
        expect(panel.locator(".cb-status-err").nth(1)).to_contain_text(
            "Do not publish: conversion_event")
        expect(panel.locator(".cb-status-steps")).to_contain_text("Steps:")
        assert not errors, errors
    finally:
        context.close()


def test_problems_payload_matches_the_policy():
    """The browser fixtures come from the real producer, not a hand-made copy."""
    items = _result([CONVERSION, AD_COPY, SECOND_TEXT])["problems"]
    assert [i["severity"] for i in items] == [fp.BLOCKING, fp.BLOCKING, fp.WARNING]


def test_error_text_beyond_the_list_is_kept_in_technical_details(browser):
    # Ревью F2: «Recorded flow failed at …» перед абзацем DO NOT PUBLISH и
    # заметка хэндоффа после него; на карточке остаётся только первое.
    result = _result([CONVERSION])
    result["error"] = ("Recorded flow failed at: ad_set_budget - timeout 30000ms | "
                       + result["error"]
                       + "\nBrowser left open for manual review; this draft is NOT publishable.")
    context, page, panel, errors = _panel(browser, result)
    try:
        err = panel.locator(".cb-problems-tech-body .cb-problems-error")
        expect(err).to_have_text("error: Recorded flow failed at: ad_set_budget - timeout 30000ms")
        assert not errors, errors
    finally:
        context.close()


def test_teach_mode_marker_survives_the_problem_list(browser):
    # Ревью F3: метка «ждёт клика Website» видна и в новой отрисовке.
    result = _result([CONVERSION])
    result["steps_completed"].append("teach_mode_website_waiting")
    context, page, panel, errors = _panel(browser, result)
    try:
        expect(panel.locator(".cb-problems .cb-problems-teach")).to_have_text(
            "⏸ waiting for a Website click")
        assert not errors, errors
    finally:
        context.close()
