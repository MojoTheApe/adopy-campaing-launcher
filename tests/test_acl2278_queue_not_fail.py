"""ACL-2278: a job waiting for memory or the account gate stays queued instead of failing.

Fakes only (same rig as the warm-session tests): no Meta, no Multilogin, no real memory reads.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.fb_api import warm  # noqa: E402
from src.web import profile_slots as ps  # noqa: E402
from tests.test_acl1812_warm_sessions import Rig, entry  # noqa: E402

A = "a" * 8 + "-0000-4000-8000-000000000000"
B = "b" * 8 + "-0000-4000-8000-000000000000"


def _short_rig(tmp_path, free):
    rig = Rig(tmp_path, reg_memory=lambda: ps.MemoryReading(64000, free[0]))
    return rig


def test_job_waits_for_memory_past_the_old_limit_and_starts_when_it_frees(tmp_path):
    free = [1600]
    rig = _short_rig(tmp_path, free)
    views: list = []
    sleeps = [0]

    def sleeping(secs):
        sleeps[0] += 1
        rig.now[0] += secs
        if rig.now[0] - 1_800_000_000.0 > warm.START_MEMORY_WAIT_S * 3:
            free[0] = 20000              # memory freed long after the old 120 s limit

    rig.reg._sleep = sleeping
    with warm.job_wait(lambda: False, views.append):
        res = rig.reg.open_one("Acc", entry(A))
    assert res["ok"] is True and res["code"] == "started"
    assert rig.now[0] - 1_800_000_000.0 > warm.START_MEMORY_WAIT_S * 3
    shown = [v for v in views if v]
    assert shown and shown[0]["hold"] == "memory_low"
    assert shown[0]["available_mb"] == 1600 and shown[0]["need_mb"] > 1600
    assert views[-1] is None, "the wait must be cleared when it ends"


def test_without_a_job_the_memory_wait_still_gives_up(tmp_path):
    rig = _short_rig(tmp_path, [1600])
    rig.reg._sleep = lambda secs: rig.now.__setitem__(0, rig.now[0] + secs)
    res = rig.reg.open_one("Acc", entry(A))
    assert res["ok"] is False and res["code"] == "memory_low" and "1.6 GB free" in res["message"]


def test_cancelling_a_job_waiting_for_memory_stops_the_wait(tmp_path):
    rig = _short_rig(tmp_path, [1600])
    stop = [False]
    count = [0]

    def sleeping(secs):
        count[0] += 1
        rig.now[0] += secs
        if count[0] > 100:
            stop[0] = True

    rig.reg._sleep = sleeping
    views: list = []
    with warm.job_wait(lambda: stop[0], views.append):
        res = rig.reg.open_one("Acc", entry(A))
    assert res["ok"] is False and res["message"].startswith("Stopped while waiting for memory")
    assert rig.started == [] and views[-1] is None


def test_prepare_request_defers_instead_of_waiting(tmp_path):
    rig = _short_rig(tmp_path, [1600])
    with warm.defer_memory_wait():
        res = rig.reg.open_one("Acc", entry(A))
    assert res["ok"] is False and warm.memory_deferred(res["code"])
    assert rig.sleeps == [] and rig.started == []


def test_job_waits_for_a_busy_gate_past_the_old_limit_and_goes_when_it_frees(tmp_path, monkeypatch):
    monkeypatch.setenv(warm.GATE_WAIT_ENV, "1")          # the old limit: 1 s here
    rig = Rig(tmp_path, limit=1)
    rig.warm_up(A)
    rig.warm_up(B)
    first = rig.checkout(A)
    got: list = []
    views: list = []

    def job():
        with warm.job_wait(lambda: False, views.append):
            got.append(rig.checkout(B))

    t = threading.Thread(target=job)
    t.start()
    time.sleep(1.6)
    assert got == [], "the job must still be queued after the old limit"
    assert any(v and v["hold"] == "account_busy" for v in views)
    first.release()
    t.join(5)
    assert len(got) == 1 and views[-1] is None
    got[0].release()


def test_cancelling_a_job_waiting_for_the_gate(tmp_path, monkeypatch):
    monkeypatch.setenv(warm.GATE_WAIT_ENV, "1")
    rig = Rig(tmp_path, limit=1)
    rig.warm_up(A)
    rig.warm_up(B)
    first = rig.checkout(A)
    stop = [False]
    errors: list = []

    def job():
        with warm.job_wait(lambda: stop[0], lambda v: None):
            try:
                rig.checkout(B)
            except warm.WarmError as exc:
                errors.append(exc)

    t = threading.Thread(target=job)
    t.start()
    time.sleep(0.3)
    stop[0] = True
    t.join(5)
    assert errors and errors[0].code == "cancelled"
    assert rig.gate.active() == [A]
    first.release()


def test_publish_job_shows_the_wait_as_queued_with_numbers_and_resumes():
    from src.web import publish_jobs as pj

    job_id = "job-2278"
    with pj._jobs_lock:
        pj._jobs[job_id] = {"job_id": job_id, "status": "running", "account_label": "Doc124",
                            "started_at": "t"}
    try:
        pj._show_resource_wait(job_id, "Doc124", {"hold": "memory_low", "available_mb": 1600,
                                                  "need_mb": 3900, "reserve_mb": 3072, "profile_mb": 828})
        job = pj._jobs[job_id]
        assert job["status"] == "queued" and job["queue"]["state"] == "waiting_memory"
        assert job["queue"]["available_mb"] == 1600 and job["queue"]["need_mb"] == 3900
        pj._show_resource_wait(job_id, "Doc124", {"hold": "account_busy", "available_mb": 9000})
        assert pj._jobs[job_id]["queue"]["state"] == "waiting_account"
        pj._show_resource_wait(job_id, "Doc124", None)
        assert pj._jobs[job_id]["status"] == "running" and "queue" not in pj._jobs[job_id]
        # Stop pressed while waiting: the status stays "cancelling" when the wait ends.
        pj._show_resource_wait(job_id, "Doc124", {"hold": "memory_low", "available_mb": 1})
        pj._jobs[job_id]["status"] = "cancelling"
        pj._show_resource_wait(job_id, "Doc124", None)
        assert pj._jobs[job_id]["status"] == "cancelling"
    finally:
        with pj._jobs_lock:
            pj._jobs.pop(job_id, None)


def test_waiting_message_lists_finished_windows_that_can_be_closed(monkeypatch):
    from src.web import publish_jobs as pj

    monkeypatch.setattr(pj._slots, "kept_labels", lambda: ["Doc120", "doc124", "Doc121"])
    job_id = "job-2278b"
    with pj._jobs_lock:
        pj._jobs[job_id] = {"job_id": job_id, "status": "running", "account_label": "Doc124",
                            "started_at": "t"}
    try:
        pj._show_resource_wait(job_id, "Doc124", {"hold": "memory_low", "available_mb": 1600})
        assert pj._jobs[job_id]["queue"]["closable_accounts"] == ["Doc120", "Doc121"]
    finally:
        with pj._jobs_lock:
            pj._jobs.pop(job_id, None)


def test_close_window_refuses_an_account_without_a_kept_window(monkeypatch):
    from src.web import publish_jobs as pj

    monkeypatch.setattr(pj._slots, "kept_labels", lambda: ["Doc120"])
    res = pj.close_kept_window("Doc999")
    assert res["ok"] is False and res["code"] == "not_found"


def test_close_window_stops_the_kept_profile_and_frees_the_place(monkeypatch):
    from src.web import publish_jobs as pj

    released, stopped = [], []
    monkeypatch.setattr(pj._slots, "kept_labels", lambda: ["Doc120"])
    monkeypatch.setattr(pj._slots, "release_kept", lambda label: released.append(label) or 1)
    monkeypatch.setattr(pj, "profile_key_for", lambda label: "profile-" + label)
    monkeypatch.setattr(pj, "_stop_idle_profile",
                        lambda label, profile, busy_msg, strict=False: stopped.append(profile) or {"ok": True})
    res = pj.close_kept_window("doc120")
    assert res["ok"] is True and stopped == ["profile-doc120"] and released == ["doc120"]


def test_long_wait_does_not_fire_a_false_step_stuck(monkeypatch):
    from src.fb_ads_rpa import run_telemetry as rt
    from src.fb_ads_rpa import sentry_signal
    from src.web import publish_jobs as pj

    now = {"t": 1000.0}
    monkeypatch.setattr(rt, "_clock", lambda: now["t"])
    monkeypatch.setenv("ACL_STEP_STUCK_S", "300")
    sent, rows = [], []
    monkeypatch.setattr(sentry_signal, "report_step_stuck", lambda *a, **kw: sent.append(a) or "evt")
    monkeypatch.setattr(rt, "record", lambda step, ok=None, **kw: rows.append((step, kw)))
    job_id = "job-2278-stuck"
    with pj._jobs_lock:
        pj._jobs[job_id] = {"job_id": job_id, "status": "running", "account_label": "Doc124",
                            "started_at": "t", "campaign_names": ["C"], "done_count": 0}
    try:
        rt.set_current_job(job_id)
        rt._touch_job(job_id, "open_profile")
        state = {"fired": False, "since": 900.0}
        for _ in range(5):                       # a 10 min wait: a look every 2 minutes
            now["t"] += 120
            pj._show_resource_wait(job_id, "Doc124", {"hold": "memory_low", "available_mb": 1})
        now["t"] += 100
        pj._show_resource_wait(job_id, "Doc124", None)
        assert pj._jobs[job_id]["status"] == "running"
        assert pj._step_stuck_tick(job_id, state, "Doc124") is False and sent == []
        assert [r for r in rows if r[1].get("note") == "resource_wait"], "the wait length is recorded"
    finally:
        with pj._jobs_lock:
            pj._jobs.pop(job_id, None)


def test_kept_labels_skip_windows_that_are_closing():
    q = ps.ProfileSlots()
    q._running["j1"] = {"profile": "p1", "kept": True, "label": "Doc1", "started": 0}
    q._running["j2"] = {"profile": "p2", "kept": True, "label": "Doc2", "started": 0, "closing": True}
    assert q.kept_labels() == ["Doc1"]
