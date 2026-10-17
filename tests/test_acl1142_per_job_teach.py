"""ACL-1142: the teach wait belongs to one job, not to the whole dashboard.

Two accounts build at once; when the robot of one cannot pick Website it waits
for a human click. The other must keep working, the banner must name the right
account, and "continue" must release only the job it was pressed for.
"""
from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import job_cancel, pacing  # noqa: E402
from src.web import publish_jobs as pj  # noqa: E402


@pytest.fixture(autouse=True)
def _clean():
    pacing.release_takeover()
    for key in list(pacing._TEACH_WAITING_JOBS):
        pacing.end_teach_wait(job_id=key)
    pacing._TEACH_RESULTS.clear()
    yield
    pacing.release_takeover()
    for key in list(pacing._TEACH_WAITING_JOBS):
        pacing.end_teach_wait(job_id=key)
    pacing._TEACH_RESULTS.clear()


def _in_job(job_id, fn):
    """Run `fn` the way a job thread does: bound to its job id."""
    box = {}

    def run():
        job_cancel.bind(None, job_id)
        try:
            box["value"] = fn()
        finally:
            job_cancel.unbind()

    t = threading.Thread(target=run)
    t.start()
    t.join(5)
    return box.get("value")


def test_a_wait_in_one_job_leaves_the_other_job_running():
    _in_job("job-A", lambda: pacing.begin_teach_wait("website_click"))
    assert _in_job("job-A", pacing.is_takeover_active) is True
    assert _in_job("job-B", pacing.is_takeover_active) is False
    assert _in_job("job-B", lambda: pacing.get_teach_state()["waiting"]) is False
    assert _in_job("job-A", lambda: pacing.get_teach_state()["step"]) == "website_click"


def test_answering_one_job_does_not_release_the_other():
    _in_job("job-A", lambda: pacing.begin_teach_wait("website_click"))
    _in_job("job-B", lambda: pacing.begin_teach_wait("website_click"))
    pacing.end_teach_wait(job_id="job-A")
    assert pacing.get_teach_state("job-A")["waiting"] is False
    assert pacing.get_teach_state("job-B")["waiting"] is True
    assert _in_job("job-B", pacing.is_takeover_active) is True


def test_the_dashboard_wide_view_lists_every_waiting_job():
    _in_job("job-A", lambda: pacing.begin_teach_wait("website_click"))
    _in_job("job-B", lambda: pacing.begin_teach_wait("website_click"))
    state = pacing.get_teach_state()
    assert state["waiting"] is True
    assert {j["job_id"] for j in state["jobs"]} == {"job-A", "job-B"}


def test_the_manual_pause_button_still_stops_every_job():
    pacing.request_takeover()
    assert _in_job("job-A", pacing.is_takeover_active) is True
    assert _in_job("job-B", pacing.is_takeover_active) is True


def test_a_waiting_thread_wakes_only_for_its_own_job(monkeypatch):
    monkeypatch.setattr(pacing.time, "sleep", lambda s: None)
    _in_job("job-A", lambda: pacing.begin_teach_wait("website_click"))
    beats = []

    def beat():
        beats.append(1)
        if len(beats) == 1:
            pacing.end_teach_wait(job_id="job-OTHER")   # not ours: keep waiting
        else:
            pacing.end_teach_wait(job_id="job-A")

    monkeypatch.setattr(job_cancel, "heartbeat", beat)
    assert _in_job("job-A", lambda: pacing.wait_for_teach_resume(5)) is True
    assert len(beats) == 2


def test_the_job_snapshot_carries_the_teach_state_and_the_account(monkeypatch):
    monkeypatch.setattr(pj, "use_rpa_publish", lambda: True)
    with pj._jobs_lock:
        before = dict(pj._jobs)
        pj._jobs.clear()
    try:
        a = pj.create_job("Doc19", "US", [{"name": "x"}])
        b = pj.create_job("Doc20", "US", [{"name": "y"}])
        _in_job(a, lambda: pacing.begin_teach_wait("website_click"))
        assert pj.get_job(a)["teach"] == {"waiting": True, "step": "website_click", "account_label": "Doc19"}
        assert pj.get_job(b)["teach"] == {"waiting": False, "step": "", "account_label": "Doc20"}
    finally:
        with pj._jobs_lock:
            pj._jobs.clear()
            pj._jobs.update(before)


def _call(coro):
    return asyncio.run(coro)


def test_resume_without_a_job_is_refused_while_two_accounts_wait(monkeypatch):
    from src.web import app as app_mod

    _in_job("job-A", lambda: pacing.begin_teach_wait("website_click"))
    _in_job("job-B", lambda: pacing.begin_teach_wait("website_click"))
    resp = _call(app_mod.rpa_teach_resume(""))
    assert resp.status_code == 409
    assert pacing.get_teach_state("job-A")["waiting"] and pacing.get_teach_state("job-B")["waiting"]


def test_resume_with_a_job_releases_exactly_that_job():
    from src.web import app as app_mod

    _in_job("job-A", lambda: pacing.begin_teach_wait("website_click"))
    _in_job("job-B", lambda: pacing.begin_teach_wait("website_click"))
    resp = _call(app_mod.rpa_teach_resume("job-B"))
    assert resp.status_code == 200
    assert pacing.get_teach_state("job-A")["waiting"] is True
    assert pacing.get_teach_state("job-B")["waiting"] is False


def test_resume_without_a_job_still_works_while_one_account_waits():
    from src.web import app as app_mod

    _in_job("job-A", lambda: pacing.begin_teach_wait("website_click"))
    assert _call(app_mod.rpa_teach_resume("")).status_code == 200
    assert pacing.get_teach_state("job-A")["waiting"] is False


def test_resume_for_a_job_that_is_not_waiting_is_a_400():
    from src.web import app as app_mod

    assert _call(app_mod.rpa_teach_resume("job-Z")).status_code == 400


def test_the_page_banner_is_per_job_and_the_dot_goes_amber():
    html = (ROOT / "src/web/templates/report.html").read_text(encoding="utf-8")
    assert "'rpa-teach-banner-' + jobId" in html
    assert "/rpa-teach/resume?job_id=" in html
    assert "p.teach && p.teach.waiting" in html
    assert "_checkTeachMode" not in html
