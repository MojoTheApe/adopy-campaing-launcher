"""ACL-2328: the staggered start (ACL-2283) holds on the CPU for at most `ACL_START_MAX_HOLD_S`.

Fakes only (same rig as the warm-session tests): no Meta, no Multilogin, no real CPU or memory reads.
"""
from types import SimpleNamespace

import pytest

from src.fb_api import warm
from tests.test_acl1812_warm_sessions import ACT, PID, Rig
from tests.test_acl2283_requeue_under_load import _mem

B = "b" * 8 + "-0000-4000-8000-000000000000"


def _entry(pid=PID):
    return SimpleNamespace(profile_id=pid, folder_id="F", multilogin_instance="A", ad_account_id=ACT)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ACL_PROFILE_RESTART_COOLDOWN_S", "0")
    monkeypatch.delenv(warm.START_MAX_HOLD_ENV, raising=False)
    monkeypatch.delenv(warm.LOAD_CPU_ENV, raising=False)


def test_defaults():
    assert warm.DEFAULT_START_MAX_HOLD_S == 180.0
    assert warm.DEFAULT_LOAD_CPU_PCT == 70.0


def test_machine_held_at_75_percent_starts_within_the_cap(tmp_path):
    rig = Rig(tmp_path)
    rig.reg._cpu = lambda: 75.0
    started_at = []
    real = rig.reg._start_profile

    def start(p, f, i):
        started_at.append(rig.now[0])
        return real(p, f, i)

    rig.reg._start_profile = start
    t0 = rig.now[0]
    views = []
    with warm.job_wait(lambda: False, views.append):
        assert rig.reg.open_one("A", _entry())["ok"]
    waited = started_at[0] - t0
    assert 180 <= waited <= 180 + 2 + 1, waited
    held = [v for v in views if v and v.get("hold") == "machine_busy"]
    assert held and held[0]["cpu_pct"] == 75.0 and held[0]["max_hold_s"] == 180.0
    assert views[-1] is None


def test_max_hold_is_configurable(tmp_path, monkeypatch):
    monkeypatch.setenv(warm.START_MAX_HOLD_ENV, "30")
    rig = Rig(tmp_path)
    rig.reg._cpu = lambda: 99.0
    t0 = rig.now[0]
    with warm.job_wait(lambda: False, lambda v: None):
        assert rig.reg.open_one("A", _entry())["ok"]
    assert 30 <= rig.now[0] - t0 <= 36


def test_above_90_percent_memory_the_start_still_waits_after_the_cap(tmp_path):
    rig = Rig(tmp_path, reg_memory=lambda: _mem(95))
    rig.reg._cpu = lambda: 75.0
    views = []
    with warm.job_wait(lambda: len(rig.sleeps) > 400, views.append):    # 800 s: far beyond the cap
        res = rig.reg.open_one("A", _entry())
    assert res["ok"] is False and not rig.started
    assert all(v is None or v.get("hold") == "memory_low" for v in views)


def test_memory_rising_during_the_cpu_hold_is_not_bypassed_by_the_cap(tmp_path):
    mem = [_mem(70)]
    rig = Rig(tmp_path, reg_memory=lambda: mem[0])
    rig.reg._cpu = lambda: 75.0
    base = rig.reg._sleep

    def sleeping(secs):
        base(secs)
        if len(rig.sleeps) == 50:
            mem[0] = _mem(95)                       # after 100 s of the CPU hold, before the cap

    rig.reg._sleep = sleeping
    with warm.job_wait(lambda: len(rig.sleeps) > 400, lambda v: None):
        res = rig.reg.open_one("A", _entry())
    assert res["ok"] is False and not rig.started


def test_stop_cancels_the_wait_inside_the_hold(tmp_path):
    rig = Rig(tmp_path)
    rig.reg._cpu = lambda: 75.0
    views = []
    with warm.job_wait(lambda: len(rig.sleeps) >= 20, views.append):
        res = rig.reg.open_one("A", _entry())
    assert res["ok"] is False and res["code"] == "cancelled" and not rig.started
    assert views[-1] is None and rig.reg._starting == {}


def test_the_cap_does_not_skip_a_profile_that_is_still_warming_up(tmp_path):
    rig = Rig(tmp_path)
    rig.reg._cpu = lambda: 75.0
    rig.reg._starting[PID] = rig.reg._mono()
    with warm.job_wait(lambda: len(rig.sleeps) > 120, lambda v: None):      # 240 s > cap, < ready cap
        res = rig.reg.open_one("B", _entry(B))
    assert res["ok"] is False and not rig.started


def test_tab_patience_keeps_the_70_percent_threshold(tmp_path, monkeypatch):
    monkeypatch.setenv(warm.START_MAX_HOLD_ENV, "5")
    rig = Rig(tmp_path)
    rig.reg._cpu = lambda: 75.0
    snap = rig.reg.load_snapshot()
    assert snap["cpu_level"] == 70.0 and snap["busy"] is True and "cpu_high" in snap["reasons"]
    rig.reg._cpu = lambda: 69.0
    assert rig.reg.load_snapshot()["busy"] is False
    monkeypatch.setenv(warm.LOAD_CPU_ENV, "60")
    assert rig.reg.load_snapshot()["cpu_level"] == 60.0


def test_every_look_of_the_hold_beats_the_silence_watcher(tmp_path, monkeypatch):
    from src.fb_ads_rpa import run_telemetry as rt
    from src.fb_ads_rpa import sentry_signal
    from src.web import publish_jobs as pj

    job_id = "job-2328-hold"
    with pj._jobs_lock:
        pj._jobs[job_id] = {"job_id": job_id, "status": "running", "account_label": "A", "started_at": "t"}
    beats, sent = [], []
    monkeypatch.setattr(rt, "beat", lambda *a, **k: beats.append(1))
    monkeypatch.setattr(sentry_signal, "report_step_stuck", lambda *a, **kw: sent.append(a) or "evt")
    rig = Rig(tmp_path)
    rig.reg._cpu = lambda: 75.0
    try:
        with warm.job_wait(lambda: False, lambda v: pj._show_resource_wait(job_id, "A", v)):
            assert rig.reg.open_one("A", _entry())["ok"]
        assert len(beats) >= 90 and sent == []          # one beat per 2 s look through the 180 s hold
        assert pj._jobs[job_id]["status"] == "running"
    finally:
        with pj._jobs_lock:
            pj._jobs.pop(job_id, None)
