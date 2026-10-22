# -*- coding: utf-8 -*-
"""ACL-2113: окно профиля остаётся после чистой публикации; простаивающие уступают место.

Владелец: байер работает в окне дальше (проверяет кампании, включает), поэтому
робот его не закрывает. Но каждое оставленное окно держит место в очереди, и два
опубликованных аккаунта запирали всю очередь (22.10, TEST9095: B91 и D65 ждали
Doc122/9403A). Здесь: окна «для удобства» (чистая публикация) уступают место,
простояв N минут; окна «на проверку» не закрываются никогда; открытое окно
берёт следующее задание того же аккаунта. Очередь, часы и остановка профиля —
подставные: ни Multilogin, ни Meta.
"""
from __future__ import annotations

import contextlib
import dataclasses
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.web import profile_slots as ps  # noqa: E402
from src.web import publish_api  # noqa: E402
from src.web import publish_jobs as pj  # noqa: E402

GB = 1024
IDLE_S = 600.0


def _until(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


class Rig:
    """Очередь с потолком 2, ручными часами и подставным закрытием окон."""

    def __init__(self, ceiling=2, evict_ok=True):
        self.now = 1000.0
        self.closed: list[tuple[str, str, str]] = []
        self.evict_ok = evict_ok
        self.settings = dataclasses.replace(ps.load_settings({}), warmup_s=0, ceiling=ceiling,
                                            kept_idle_close_s=IDLE_S)
        self.probe = ps.ProfileUsage(lambda profiles: {p: int(3.5 * GB) for p in profiles},
                                     clock=lambda: self.now, background=False)
        self.slots = self.make_slots(self._evict)

    def make_slots(self, evictor):
        return ps.ProfileSlots(
            memory=lambda: ps.MemoryReading(total_mb=128 * GB, available_mb=120 * GB),
            settings=lambda: self.settings, clock=lambda: self.now, tick_s=0.02, usage=self.probe,
            evictor=evictor)

    def _evict(self, job_id, label, profile):
        self.closed.append((job_id, label, profile))
        return self.evict_ok

    def run_and_keep(self, job_id, profile, label, kind):
        grant = self.slots.acquire(job_id, profile)
        assert grant is not None
        self.slots.keep(job_id, label, kind)
        return grant

    def start(self, job_id, profile):
        box: dict = {"waits": []}

        def go():
            box["grant"] = self.slots.acquire(job_id, profile, on_wait=box["waits"].append)

        t = threading.Thread(target=go, daemon=True)
        t.start()
        box["thread"] = t
        return box


def _running(slots):
    return sorted((r["profile"], r["kept_kind"]) for r in slots.snapshot()["running"])


# ── очередь: окна «для удобства» уступают место ─────────────────────────────

def test_three_clean_publishes_with_a_ceiling_of_two_the_third_starts_after_the_oldest_idle_window_closes():
    rig = Rig()
    rig.run_and_keep("a", "P1", "KLD_A", ps.KEPT_CONVENIENCE)
    rig.now += 60
    rig.run_and_keep("b", "P2", "KLD_B", ps.KEPT_CONVENIENCE)
    rig.now += IDLE_S + 1                                   # оба простаивают дольше N минут
    c = rig.start("c", "P3")
    assert _until(lambda: "grant" in c)
    assert rig.closed == [("a", "KLD_A", "P1")]            # закрыто ровно самое давнее
    assert [x["label"] for x in c["grant"]["closed_for_room"]] == ["KLD_A"]
    assert [p for p, _ in _running(rig.slots)] == ["P2", "P3"]


def test_a_window_that_has_not_idled_long_enough_is_not_closed_and_the_queue_says_why():
    rig = Rig()
    rig.run_and_keep("a", "P1", "KLD_A", ps.KEPT_CONVENIENCE)
    rig.run_and_keep("b", "P2", "KLD_B", ps.KEPT_CONVENIENCE)
    rig.now += IDLE_S - 30
    c = rig.start("c", "P3")
    assert _until(lambda: c["waits"])
    time.sleep(0.15)
    assert "grant" not in c and rig.closed == []
    assert c["waits"][0]["reason"] == "idle_windows"
    assert sorted(c["waits"][0]["convenience_accounts"]) == ["KLD_A", "KLD_B"]
    assert pj._queue_view(c["waits"][0])["state"] == "waiting_idle"
    rig.now += 60                                           # теперь простаивают
    assert _until(lambda: "grant" in c)
    assert [x[1] for x in rig.closed] == ["KLD_A"]


def test_review_windows_are_never_auto_closed_however_long_they_stay():
    rig = Rig()
    rig.run_and_keep("a", "P1", "KLD_A", ps.KEPT_REVIEW)
    rig.run_and_keep("b", "P2", "KLD_B", ps.KEPT_REVIEW)
    rig.now += 24 * 3600
    c = rig.start("c", "P3")
    assert _until(lambda: c["waits"])
    time.sleep(0.15)
    assert "grant" not in c and rig.closed == []
    assert c["waits"][0]["reason"] == "review_windows"
    assert sorted(c["waits"][0]["kept_accounts"]) == ["KLD_A", "KLD_B"]
    assert pj._queue_view(c["waits"][0])["state"] == "waiting_review"


def test_only_the_idle_convenience_window_is_closed_when_a_review_window_is_open_too():
    rig = Rig()
    rig.run_and_keep("a", "P1", "KLD_REVIEW", ps.KEPT_REVIEW)       # самое давнее, но на проверке
    rig.now += 10
    rig.run_and_keep("b", "P2", "KLD_CONV", ps.KEPT_CONVENIENCE)
    rig.now += IDLE_S + 1
    c = rig.start("c", "P3")
    assert _until(lambda: "grant" in c)
    assert rig.closed == [("b", "KLD_CONV", "P2")]
    assert ("P1", ps.KEPT_REVIEW) in _running(rig.slots)


def test_a_failed_close_keeps_the_window_and_does_not_retry_in_a_loop():
    rig = Rig(evict_ok=False)
    rig.run_and_keep("a", "P1", "KLD_A", ps.KEPT_CONVENIENCE)
    rig.run_and_keep("b", "P2", "KLD_B", ps.KEPT_CONVENIENCE)
    rig.now += IDLE_S + 1
    c = rig.start("c", "P3")
    assert _until(lambda: rig.closed)
    time.sleep(0.2)
    assert "grant" not in c and len(rig.closed) == 1
    assert len(_running(rig.slots)) == 2


def test_a_job_for_an_account_whose_window_is_open_reuses_it_without_closing_anything():
    rig = Rig()
    rig.run_and_keep("a", "P1", "KLD_A", ps.KEPT_CONVENIENCE)
    rig.run_and_keep("b", "P2", "KLD_B", ps.KEPT_CONVENIENCE)
    rig.now += IDLE_S + 1
    again = rig.start("a2", "P1")
    assert _until(lambda: "grant" in again)
    assert rig.closed == []
    assert again["grant"]["reused_kept"] == ps.KEPT_CONVENIENCE
    assert [p for p, _ in _running(rig.slots)].count("P1") == 1


def test_a_review_window_stays_review_when_the_next_job_reuses_it():
    rig = Rig()
    rig.run_and_keep("a", "P1", "KLD_A", ps.KEPT_REVIEW)
    again = rig.start("a2", "P1")
    assert _until(lambda: "grant" in again)
    assert again["grant"]["reused_kept"] == ps.KEPT_REVIEW


def test_the_idle_threshold_comes_from_the_machine_env_in_minutes():
    assert ps.load_settings({}).kept_idle_close_s == 600.0
    assert ps.load_settings({"ACL_KEPT_IDLE_CLOSE_MIN": "3"}).kept_idle_close_s == 180.0


def test_touching_a_window_restarts_its_idle_time():
    rig = Rig()
    rig.run_and_keep("a", "P1", "KLD_A", ps.KEPT_CONVENIENCE)
    rig.run_and_keep("b", "P2", "KLD_B", ps.KEPT_CONVENIENCE)
    rig.now += IDLE_S + 1
    rig.slots.touch("kld_a")
    c = rig.start("c", "P3")
    assert _until(lambda: "grant" in c)
    assert [x[1] for x in rig.closed] == ["KLD_B"]


# ── задания: вид оставленного окна и строка в панели ────────────────────────

def _ok_result(name="C1"):
    return {"campaign_name": name, "backend": "api", "success": True, "publishable": True,
            "blocking_failures": [], "readback": {"ok": True}, "error": None}


def _mismatch_result(name="C1"):
    return {"campaign_name": name, "backend": "api", "success": True, "publishable": False,
            "blocking_failures": ["readback mismatch"], "readback": {"ok": False}, "error": None}


class FakePublisher:
    scenario: dict = {}

    def __init__(self, *, progress=None, dry_run=False):
        self.cancel_event = threading.Event()
        self.writes = 0
        self.inherited_profile = False

    def cancel(self):
        self.cancel_event.set()

    def cancelled(self):
        return self.cancel_event.is_set()

    def build_drafts(self, slots, *, account_label, country):
        return [SimpleNamespace(name="C1", errors=[])]

    def open(self, account_label):
        pass

    def publish_draft(self, draft):
        self.writes += 3
        return self.scenario["result"]("C1")

    def finish_job(self, account_label, *, clean, other_job_waits=False):
        return "kept" if clean else ""

    @property
    def started_profile(self):
        return True

    def close(self, *, force=False):
        pass


@pytest.fixture
def jobs(monkeypatch):
    with pj._jobs_lock:
        before = dict(pj._jobs)
        pj._jobs.clear()
    rig = Rig()
    monkeypatch.setattr(pj, "_slots", rig.make_slots(lambda j, l, p: pj._evict_kept_window(j, l, p)))
    monkeypatch.setattr(pj, "use_rpa_publish", lambda: True)
    monkeypatch.setattr(pj, "_fb_backend", lambda: "api")
    monkeypatch.setattr(pj, "_clear_inherited_asyncio_loop", lambda: None)
    monkeypatch.setattr(pj, "keep_display_awake", lambda *a, **k: contextlib.nullcontext())
    monkeypatch.setattr(pj, "_watch_screen_lock", lambda *a, **k: contextlib.nullcontext())
    monkeypatch.setattr(pj, "_start_stuck_watch", lambda *a, **k: None)
    monkeypatch.setattr(pj, "profile_key_for", lambda label: "P-" + label)
    monkeypatch.setattr(publish_api, "ApiPublisher", FakePublisher)
    stops: list[str] = []
    monkeypatch.setattr(pj, "_stop_idle_profile", lambda label, profile, busy_msg="": (
        stops.append(label) or {"ok": True}))

    def run(label, result=_ok_result):
        FakePublisher.scenario = {"result": result}
        job_id = pj.create_job(label, "CL", [{}])
        pj._run_job(job_id, [], account_label=label, country="CL", via_multilogin=True,
                    activate=False, dry_run=False, hand_off=True)
        with pj._jobs_lock:
            return job_id, dict(pj._jobs[job_id])

    run.rig, run.stops = rig, stops
    yield run
    with pj._jobs_lock:
        pj._jobs.clear()
        pj._jobs.update(before)


def _kinds():
    return sorted((r["profile"], r["kept_kind"]) for r in pj._slots.snapshot()["running"])


def test_a_clean_publish_leaves_a_convenience_window_with_an_explaining_line(jobs):
    _, job = jobs("KLD_A")
    assert _kinds() == [("P-KLD_A", ps.KEPT_CONVENIENCE)]
    assert publish_api.PROFILE_CLOSED_NOTICE not in job["creative_notices"]
    assert pj.API_KEPT_NOTICE in job["creative_notices"]


def test_a_readback_mismatch_leaves_a_review_window(jobs):
    jobs("KLD_A", result=_mismatch_result)
    assert _kinds() == [("P-KLD_A", ps.KEPT_REVIEW)]


def test_three_clean_publishes_close_the_oldest_idle_window_and_say_so_in_both_panels(jobs):
    rig = jobs.rig
    first, _ = jobs("KLD_A")
    rig.now += 30
    jobs("KLD_B")
    rig.now += IDLE_S + 1
    _, job3 = jobs("KLD_C")
    assert jobs.stops == ["KLD_A"]                          # закрыто через остановку профиля
    assert "Closed KLD_A to make room" in job3["creative_notices"]
    with pj._jobs_lock:
        old = dict(pj._jobs[first])
    assert old["profile_closed"] is True
    assert any("make room" in n for n in old["creative_notices"])
    assert [p for p, _ in _kinds()] == ["P-KLD_B", "P-KLD_C"]


def test_review_windows_block_the_third_job_and_nothing_is_closed(jobs):
    rig = jobs.rig
    jobs("KLD_A", result=_mismatch_result)
    jobs("KLD_B", result=_mismatch_result)
    rig.now += 24 * 3600
    FakePublisher.scenario = {"result": _ok_result}
    job_id = pj.create_job("KLD_C", "CL", [{}])
    t = threading.Thread(target=pj._run_job, args=(job_id, []), kwargs=dict(
        account_label="KLD_C", country="CL", via_multilogin=True, activate=False,
        dry_run=False, hand_off=True), daemon=True)
    t.start()
    assert _until(lambda: (pj._jobs[job_id].get("queue") or {}).get("hold") == "review_windows")
    time.sleep(0.15)
    assert jobs.stops == []
    with pj._jobs_lock:
        pj._jobs[job_id]["cancel_requested"] = True
    pj._slots.wake()
    t.join(3)


def test_the_next_job_for_an_account_with_an_open_window_reuses_it(jobs):
    jobs("KLD_A")
    _, job = jobs("KLD_A")                                  # тот же аккаунт: окно открыто
    assert job["reused_kept"] == ps.KEPT_CONVENIENCE
    assert jobs.stops == []
    assert _kinds() == [("P-KLD_A", ps.KEPT_CONVENIENCE)]


# ── выключатель ACL-1444 ────────────────────────────────────────────────────

@pytest.fixture
def real_publisher(monkeypatch):
    stops = []
    monkeypatch.setattr(publish_api, "_stop_profile", lambda e, label: stops.append(label))
    from src.fb_api import stop_log
    monkeypatch.setattr(stop_log, "record_stop", lambda pid: None)

    def make(*, started=True):
        pub = publish_api.ApiPublisher()
        pub._opened = SimpleNamespace(started_now=started)
        pub._entry = SimpleNamespace(profile_id="")
        return pub

    make.stops = stops
    return make


def test_by_default_a_clean_publish_keeps_the_profile_open(real_publisher, monkeypatch):
    monkeypatch.delenv("FB_RPA_KEEP_PROFILES_OPEN", raising=False)
    assert real_publisher().finish_job("A", clean=True) == "kept"
    assert real_publisher.stops == []


def test_with_the_switch_off_a_clean_publish_closes_the_profile_as_before(real_publisher, monkeypatch):
    monkeypatch.setenv("FB_RPA_KEEP_PROFILES_OPEN", "0")
    assert real_publisher().finish_job("A", clean=True) == "closed"
    assert real_publisher.stops == ["A"]


def test_an_unclean_job_never_reports_a_convenience_keep(real_publisher, monkeypatch):
    monkeypatch.delenv("FB_RPA_KEEP_PROFILES_OPEN", raising=False)
    assert real_publisher().finish_job("A", clean=False) == ""


def test_a_window_inherited_from_a_previous_job_is_ours_to_keep_or_close(real_publisher, monkeypatch):
    monkeypatch.setenv("FB_RPA_KEEP_PROFILES_OPEN", "0")
    pub = real_publisher(started=False)
    assert pub.started_profile is False
    pub.inherited_profile = True
    assert pub.started_profile is True
    assert pub.finish_job("A", clean=True) == "closed"
