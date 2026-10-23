"""Two teach banners and campaign dots on the real dashboard, offline.

Status and progress come from the same synthetic job snapshot. No publisher,
profile, employee data or Meta request is started by these browser tests.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import expect

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_builder_per_account import _render
from test_campaign_build_dot_browser import add, browser, names
from test_drawer_review_browser import fields
from src.web.publish_progress import job_progress


@pytest.fixture
def teach_ui(browser, tmp_path):
    context = browser.new_context()
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    html = _render({"Demo": [], "Other": []})
    state = {"jobs": {}, "resumed": []}
    telemetry = tmp_path / "no-telemetry.jsonl"

    def route(request):
        url = urlparse(request.request.url)
        path = url.path
        if request.request.resource_type == "document":
            request.fulfill(content_type="text/html", body=html)
            return
        data = {"files": [], "texts": [], "profiles": [], "waiting": False}
        if path == "/preview-publish":
            form = fields(request.request)
            data = {"plan": [{"Campaign Name": form[f"camp_{i}_name"],
                              "errors": [], "warnings": [], "_ads_raw": []}
                             for i in range(int(form["camp_count"]))]}
        elif path == "/rpa-teach/resume":
            assert request.request.method == "POST"
            job_id = parse_qs(url.query)["job_id"][0]
            state["resumed"].append(job_id)
            state["jobs"][job_id]["teach"]["waiting"] = False
            data = {"ok": True}
        elif path.startswith("/publish-jobs/"):
            job = state["jobs"][path.split("/")[2]]
            data = job_progress(job, path=telemetry) if path.endswith("/progress") else job
        request.fulfill(json=data)

    page.route("**/*", route)
    page.goto("http://acl.test/")
    page.evaluate("cbDrawer.open()")
    add(page, account=0)
    add(page, account=1)
    page.evaluate("""() => document.querySelectorAll('form.open-account-form').forEach(f => {
      f.querySelector('[name$="_asset"]').value = 'Gold';
      f.dispatchEvent(new Event('input', {bubbles:true}));
    })""")
    # Builder names/checks are debounced. Track the settled campaign identities,
    # just as a real publish does, rather than the initial provisional name.
    page.wait_for_function("""() => Array.from(document.querySelectorAll('.cb-slot')).every(s =>
      s.querySelector('.cb-name-hidden').value.includes('_Gol_') &&
      s._drawerResult && s._drawerResult.state === 'ok')""")
    now = datetime.now(timezone.utc).isoformat()
    for i, account in enumerate(("Demo", "Other")):
        job_id = f"teach-job-{i}"
        state["jobs"][job_id] = {
            "job_id": job_id, "account_label": account,
            "status": "running", "created_at": now, "started_at": now,
            "campaign_names": names(page, i), "results": [], "errors": [],
            "total": 1, "done_count": 0,
            "teach": {"waiting": True, "step": "website_click", "account_label": account},
        }
    # Use the actual status poller and progress/dot tracker. Only their network
    # responses are synthetic, so one banner cannot borrow the other job's data.
    page.evaluate("""jobs => Object.values(jobs).forEach(job => {
      const panel = document.createElement('div');
      document.body.appendChild(panel);
      cbPollJob(job.job_id, panel, null, null, null, true);
      cbBuild.watch(job.job_id, job.account_label, job.campaign_names);
    })""", state["jobs"])
    yield page, state
    assert not errors, errors
    context.close()


def banner(page, job_id):
    return page.locator(f'[data-job-id="{job_id}"]')


def dot(page, job):
    # The accounts deliberately have the same campaign name. Scope by account
    # too, so a dot in the other account cannot make this assertion pass.
    account = page.locator(".cbd-acc").filter(
        has=page.locator(".cbd-acc-name", has_text=job["account_label"]))
    return account.locator(".cbd-camp").filter(
        has=page.locator(".cbd-camp-name", has_text=job["campaign_names"][0])).locator(".cbd-ind")


def assert_dot(page, job, state, *, moving):
    expect(dot(page, job)).to_have_class("cbd-ind cbd-ind-build-" + state)
    expect(dot(page, job).locator("span")).to_have_css(
        "animation-name", "cbd-build-pulse" if moving else "none")
    variable = {"run": "--muted", "attn": "--build-orange", "bad": "--build-orange",
                "ok": "--good"}[state]
    assert dot(page, job).evaluate("""(e, variable) => {
      const sample = document.createElement('i');
      sample.style.color = 'var(' + variable + ')'; e.appendChild(sample);
      const expected = getComputedStyle(sample).color;
      sample.remove(); return getComputedStyle(e).color === expected;
    }""", variable)


def test_each_banner_resumes_only_its_account_and_the_dots_follow(teach_ui):
    page, state = teach_ui
    a, b = [state["jobs"][f"teach-job-{i}"] for i in range(2)]
    assert a["campaign_names"] == b["campaign_names"]
    for job_id, account in [("teach-job-0", "Demo"), ("teach-job-1", "Other")]:
        expect(banner(page, job_id)).to_contain_text("We need your click — " + account)
    assert_dot(page, a, "attn", moving=True)
    assert_dot(page, b, "attn", moving=True)

    banner(page, "teach-job-1").get_by_role("button", name="I clicked Website — continue").click()
    expect(banner(page, "teach-job-1")).to_have_count(0)
    expect(banner(page, "teach-job-0")).to_be_visible()
    assert state["resumed"] == ["teach-job-1"]
    assert state["jobs"]["teach-job-0"]["teach"]["waiting"] is True
    page.evaluate("cbBuild.refresh()")
    assert_dot(page, a, "attn", moving=True)
    assert_dot(page, b, "run", moving=True)

    banner(page, "teach-job-0").get_by_role("button", name="I clicked Website — continue").click()
    expect(banner(page, "teach-job-0")).to_have_count(0)
    assert state["resumed"] == ["teach-job-1", "teach-job-0"]
    for job_id, result in [("teach-job-0", {"success": True, "publishable": True}),
                           ("teach-job-1", {"success": False, "blocking_failures": ["ad_copy"]})]:
        job = state["jobs"][job_id]
        job["status"] = "done"
        job["results"] = [{"campaign_name": job["campaign_names"][0], **result}]
    page.evaluate("cbBuild.refresh()")
    assert_dot(page, a, "ok", moving=False)
    assert_dot(page, b, "bad", moving=False)


def test_cancelled_job_removes_its_banner_while_the_other_still_waits(teach_ui):
    page, state = teach_ui
    expect(banner(page, "teach-job-0")).to_be_visible()
    expect(banner(page, "teach-job-1")).to_be_visible()
    job = state["jobs"]["teach-job-0"]
    job["status"] = "cancelled"
    job["teach"]["waiting"] = False
    page.evaluate("cbBuild.refresh()")
    expect(banner(page, "teach-job-0")).to_have_count(0)
    expect(banner(page, "teach-job-1")).to_be_visible()
    assert state["resumed"] == []
    assert_dot(page, job, "bad", moving=False)
    assert_dot(page, state["jobs"]["teach-job-1"], "attn", moving=True)


def test_reduced_motion_keeps_the_teach_dot_orange_without_pulsing(teach_ui):
    page, state = teach_ui
    page.emulate_media(reduced_motion="reduce")
    for job in state["jobs"].values():
        assert_dot(page, job, "attn", moving=False)
        span = dot(page, job).locator("span")
        assert span.evaluate("e => getComputedStyle(e).boxShadow") != "none"
        assert span.evaluate("e => getComputedStyle(e).backgroundColor") != "rgba(0, 0, 0, 0)"
