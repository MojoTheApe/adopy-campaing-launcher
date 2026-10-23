# -*- coding: utf-8 -*-
"""ACL-1784: API-задание не держит окно «на проверку», когда смотреть нечего.

11.10 на 9095 задание KLD_BFL_B96 упало на плане (страна не нашлась) до
первой записи, а профиль остался «окном на проверку» и занял единственное
место — следующее задание (Facebook_5414) ждало, пока владелец закроет
профиль руками. Здесь: ничего не записали или сверка чистая — место
отпускается; запись была, а сверка не подтвердила — окно держит место, как
раньше. Публикатор и очередь — подставные, ни Meta, ни Multilogin.
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
from tests.profile_lease_fakes import fake_profile  # noqa: E402

GB = 1024


def _slots(ceiling=2):
    """Очередь на много памяти; замер видит каждый профиль открытым (окно руками не закрыто)."""
    settings = dataclasses.replace(ps.load_settings({}), warmup_s=0, ceiling=ceiling)
    probe = ps.ProfileUsage(lambda profiles: {p: int(3.5 * GB) for p in profiles},
                            clock=lambda: 1000.0, background=False)
    return ps.ProfileSlots(memory=lambda: ps.MemoryReading(total_mb=128 * GB, available_mb=120 * GB),
                           settings=lambda: settings, clock=lambda: 1000.0, tick_s=0.02, usage=probe)


def _ok_result(name="C1"):
    return {"campaign_name": name, "backend": "api", "success": True, "publishable": True,
            "blocking_failures": [], "readback": {"ok": True}, "error": None}


def _mismatch_result(name="C1"):
    return {"campaign_name": name, "backend": "api", "success": True, "publishable": False,
            "blocking_failures": ["readback mismatch: campaign status — expected 'PAUSED', got 'ACTIVE'"],
            "readback": {"ok": False}, "error": None}


class FakePublisher:
    """Вместо `ApiPublisher`: сценарий задаёт тест, сам публикатор ничего не открывает."""

    scenario: dict = {}
    last: "FakePublisher | None" = None

    def __init__(self, *, progress=None, dry_run=False):
        self.cancel_event = threading.Event()
        self.writes = 0
        self.finish_calls: list[dict] = []
        self.opened = False
        FakePublisher.last = self

    def cancel(self):
        self.cancel_event.set()

    def cancelled(self):
        return self.cancel_event.is_set()

    def build_drafts(self, slots, *, account_label, country):
        return [SimpleNamespace(name=d["name"], errors=list(d.get("errors") or []))
                for d in self.scenario["drafts"]]

    def open(self, account_label):
        self.opened = True
        if self.scenario.get("open_raises"):
            raise publish_api.ApiPublishError("preflight: the ad account is disabled; nothing was created")

    def publish_draft(self, draft):
        self.writes += int(self.scenario.get("writes", 0))
        if self.scenario.get("publish_raises"):
            raise RuntimeError("the page stopped after a write")
        return self.scenario["result"](draft.name)

    def finish_job(self, account_label, *, clean, other_job_waits=False):
        self.finish_calls.append({"clean": clean, "other": other_job_waits})
        if (clean and not other_job_waits and self.scenario.get("started_now")
                and not self.scenario.get("left_open")):
            return "closed"
        return ""

    @property
    def started_profile(self):
        return bool(self.scenario.get("started_now"))

    def close(self, *, force=False):
        pass


@pytest.fixture
def api_job(monkeypatch):
    with pj._jobs_lock:
        before = dict(pj._jobs)
        pj._jobs.clear()
    slots = _slots()
    monkeypatch.setattr(pj, "_slots", slots)
    monkeypatch.setattr(pj, "use_rpa_publish", lambda: True)
    monkeypatch.setattr(pj, "_fb_backend", lambda: "api")
    monkeypatch.setattr(pj, "_clear_inherited_asyncio_loop", lambda: None)
    monkeypatch.setattr(pj, "keep_display_awake", lambda *a, **k: contextlib.nullcontext())
    monkeypatch.setattr(pj, "_watch_screen_lock", lambda *a, **k: contextlib.nullcontext())
    monkeypatch.setattr(pj, "_start_stuck_watch", lambda *a, **k: None)
    monkeypatch.setattr(pj, "profile_key_for", lambda label: fake_profile(label))
    monkeypatch.setattr(publish_api, "ApiPublisher", FakePublisher)

    def run(scenario, label="KLD_BFL_B96"):
        FakePublisher.scenario = scenario
        job_id = pj.create_job(label, "CL", [{}])
        pj._run_job(job_id, [], account_label=label, country="CL", via_multilogin=True,
                    activate=False, dry_run=False, hand_off=True)
        with pj._jobs_lock:
            job = dict(pj._jobs[job_id])
        return slots, job, FakePublisher.last

    yield run
    with pj._jobs_lock:
        pj._jobs.clear()
        pj._jobs.update(before)


def _kept(slots):
    return [(r["profile"], r["kept"]) for r in slots.snapshot()["running"]]


# ── ничего не записали ───────────────────────────────────────────────────

def test_a_plan_error_leaves_no_review_window_and_frees_the_slot(api_job):
    slots, job, pub = api_job({"drafts": [{"name": "C1", "errors": ["country could not be resolved"]}]})
    assert pub.opened is False and pub.writes == 0
    assert _kept(slots) == []
    assert job["review_window"] is False
    assert pj.API_NO_WRITE_NOTICE in job["creative_notices"]


def test_after_a_plan_error_the_next_queued_job_starts_on_a_cap_of_one(api_job, monkeypatch):
    slots = _slots(ceiling=1)
    monkeypatch.setattr(pj, "_slots", slots)
    api_job({"drafts": [{"name": "C1", "errors": ["country could not be resolved"]}]})
    deadline = time.monotonic() + 2.0
    grant = slots.acquire("next", "Facebook_5414", cancelled=lambda: time.monotonic() > deadline)
    assert grant is not None, "the plan error kept a review window and blocked the queue"
    assert grant["decision"].get("reason") != "review_windows"


def test_a_preflight_stop_before_any_write_closes_a_profile_the_job_started(api_job):
    slots, job, pub = api_job({"drafts": [{"name": "C1"}], "open_raises": True, "started_now": True})
    assert pub.writes == 0
    assert pub.finish_calls == [{"clean": True, "other": False}]
    assert job["profile_closed"] is True
    assert _kept(slots) == []


def test_a_preflight_stop_on_a_profile_the_user_opened_leaves_it_open_but_frees_the_slot(api_job):
    slots, job, pub = api_job({"drafts": [{"name": "C1"}], "open_raises": True, "started_now": False})
    assert not job.get("profile_closed")             # чужой профиль не трогаем
    assert _kept(slots) == []
    assert pj.API_NO_WRITE_NOTICE in job["creative_notices"]


# ── чистая сверка ────────────────────────────────────────────────────────

def test_a_clean_job_on_a_profile_the_user_opened_releases_its_slot(api_job):
    slots, job, pub = api_job({"drafts": [{"name": "C1"}], "writes": 5, "result": _ok_result,
                               "started_now": False})
    assert pub.finish_calls == [{"clean": True, "other": False}]
    assert not job.get("profile_closed")
    assert _kept(slots) == []
    assert job["review_window"] is False
    assert pj.API_CLEAN_NOTICE in job["creative_notices"]


def test_a_clean_job_that_started_the_profile_closes_it_with_the_usual_notice(api_job):
    slots, job, _ = api_job({"drafts": [{"name": "C1"}], "writes": 5, "result": _ok_result,
                             "started_now": True})
    assert job["profile_closed"] is True
    assert _kept(slots) == []
    assert job["creative_notices"] == [publish_api.PROFILE_CLOSED_NOTICE]


# ── запись была, сверка не подтвердила — окно как раньше ─────────────────

def test_a_readback_mismatch_keeps_the_review_window(api_job):
    slots, job, pub = api_job({"drafts": [{"name": "C1"}], "writes": 5, "result": _mismatch_result,
                               "started_now": True})
    assert pub.finish_calls == [{"clean": False, "other": False}]
    assert not job.get("profile_closed")
    assert _kept(slots) == [(fake_profile("KLD_BFL_B96"), True)]
    assert job["review_window"] is True
    assert pj.API_REVIEW_NOTICE in job["creative_notices"]


def test_an_error_after_a_write_keeps_the_review_window(api_job):
    slots, job, _ = api_job({"drafts": [{"name": "C1"}], "writes": 2, "publish_raises": True,
                             "result": _ok_result, "started_now": True})
    assert _kept(slots) == [(fake_profile("KLD_BFL_B96"), True)]
    assert pj.API_REVIEW_NOTICE in job["creative_notices"]


# ── профиль, поднятый заданием, остался открытым: место держим (ревью B1) ──

def test_a_job_started_profile_left_open_without_a_write_keeps_the_slot(api_job):
    slots, job, _ = api_job({"drafts": [{"name": "C1"}], "open_raises": True,
                             "started_now": True, "left_open": True})
    assert not job.get("profile_closed")
    assert _kept(slots) == [(fake_profile("KLD_BFL_B96"), True)]
    assert job["review_window"] is True
    assert pj.API_STILL_OPEN_NOTICE in job["creative_notices"]
    assert pj.API_NO_WRITE_NOTICE not in job["creative_notices"]


def test_a_clean_job_whose_started_profile_stays_open_keeps_the_slot(api_job):
    slots, job, _ = api_job({"drafts": [{"name": "C1"}], "writes": 5, "result": _ok_result,
                             "started_now": True, "left_open": True})
    assert _kept(slots) == [(fake_profile("KLD_BFL_B96"), True)]
    assert pj.API_STILL_OPEN_NOTICE in job["creative_notices"]
    assert pj.API_CLEAN_NOTICE not in job["creative_notices"]


def test_a_user_opened_profile_still_frees_the_slot_when_left_open(api_job):
    slots, job, _ = api_job({"drafts": [{"name": "C1"}], "open_raises": True,
                             "started_now": False, "left_open": True})
    assert _kept(slots) == []
    assert pj.API_NO_WRITE_NOTICE in job["creative_notices"]


@pytest.fixture
def real_publisher(monkeypatch):
    stops = []
    monkeypatch.setattr(publish_api, "_stop_profile", lambda e, label: stops.append(label))
    monkeypatch.setattr(publish_api, "_keep_profiles_open", lambda: False)
    from src.fb_api import stop_log
    monkeypatch.setattr(stop_log, "record_stop", lambda pid: None)

    def make(*, dry_run=False, started=True):
        pub = publish_api.ApiPublisher(dry_run=dry_run)
        pub._opened = SimpleNamespace(started_now=started)
        pub._entry = SimpleNamespace(profile_id="")
        return pub

    make.stops = stops
    return make


def test_real_finish_job_closes_a_job_started_profile_after_a_clean_job(real_publisher):
    pub = real_publisher()
    assert pub.started_profile is True
    assert pub.finish_job("A", clean=True) == "closed"
    assert real_publisher.stops == ["A"]


def test_real_finish_job_leaves_the_profile_open_after_a_stop_before_the_first_write(real_publisher):
    pub = real_publisher()
    pub.cancel()
    assert pub.finish_job("A", clean=True) == ""
    assert pub.started_profile is True and real_publisher.stops == []


def test_real_finish_job_leaves_the_profile_open_when_the_keep_open_switch_is_set(
        real_publisher, monkeypatch):
    monkeypatch.setattr(publish_api, "_keep_profiles_open", lambda: True)
    pub = real_publisher()
    assert pub.finish_job("A", clean=True) == "kept"      # ACL-2113: намеренно оставлено
    assert real_publisher.stops == []


def test_real_finish_job_leaves_the_profile_open_after_a_dry_run(real_publisher):
    pub = real_publisher(dry_run=True)
    assert pub.finish_job("A", clean=True) == ""
    assert real_publisher.stops == []


def test_real_finish_job_leaves_the_profile_open_when_stopping_fails(real_publisher, monkeypatch):
    def boom(entry, label):
        raise RuntimeError("stop failed")
    monkeypatch.setattr(publish_api, "_stop_profile", boom)
    pub = real_publisher()
    assert pub.finish_job("A", clean=True) == ""
    assert pub.started_profile is True


def test_started_profile_is_false_for_a_user_opened_profile(real_publisher):
    assert real_publisher(started=False).started_profile is False
    assert publish_api.ApiPublisher().started_profile is False


def test_the_notices_are_english():
    for text in (pj.API_NO_WRITE_NOTICE, pj.API_CLEAN_NOTICE, pj.API_REVIEW_NOTICE,
                 pj.API_STILL_OPEN_NOTICE):
        assert text.isascii()
