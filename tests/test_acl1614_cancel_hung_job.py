# -*- coding: utf-8 -*-
"""ACL-1614: Stop останавливает задание, даже когда робот завис внутри шага.

05.10 задание 1ad447f9 стояло в `foreign_windows._clear_marks` → `page.evaluate`;
отмена закрыла соединение Playwright из чужого потока, поток задания закрутился
в sync-цикле с GIL в руках, дешборд тормозил по 10-30 с на запрос, а задание
осталось `cancelling` и держало место. Теперь отмена только сигналит, длинные
ожидания проверяют сигнал (`JobCancelled`), а поток, не ответивший за срок,
бросается: задание `cancelled`, место свободно, проснувшись, поток ничего не пишет.
"""
from __future__ import annotations

import contextlib
import json
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import job_cancel  # noqa: E402
from src.fb_ads_rpa import run_telemetry as rt  # noqa: E402
from src.fb_ads_rpa.draft import RpaPublishResult  # noqa: E402
from src.web import profile_slots as ps  # noqa: E402
from src.web import publish_jobs as pj  # noqa: E402


def _until(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


class FakePublisher:
    """Публикатор без браузера: поведение `publish_draft` задаёт тест."""

    behaviour = None          # callable(publisher, draft) -> RpaPublishResult
    instances: list = []

    def __init__(self, *, dry_run=False, hand_off=False, **kw):
        self.cancel_event = threading.Event()
        self._cancelled = False
        self.closed_in: list[str] = []
        self.draft_names = ["CAMP"]
        type(self).instances.append(self)

    def cancel(self):
        self._cancelled = True
        self.cancel_event.set()

    def build_drafts(self, slots, **kw):
        return [SimpleNamespace(name=n, errors=[]) for n in self.draft_names]

    def publish_draft(self, draft):
        return type(self).behaviour(self, draft)

    def close(self, force=False):
        self.closed_in.append(threading.current_thread().name)


@pytest.fixture
def env(monkeypatch):
    FakePublisher.instances = []
    FakePublisher.behaviour = None
    probe = ps.ProfileUsage(lambda profiles: {}, clock=time.monotonic, background=False)
    slots = ps.ProfileSlots(
        memory=lambda: ps.MemoryReading(total_mb=32 * 1024, available_mb=64 * 1024),
        settings=lambda: ps.SlotSettings(override=1, warmup_s=0),
        tick_s=0.02, usage=probe)
    monkeypatch.setattr(pj, "_slots", slots)
    monkeypatch.setattr(pj, "RpaPublisher", FakePublisher)
    monkeypatch.setattr(pj, "use_rpa_publish", lambda: True)
    monkeypatch.setattr(pj, "_clear_inherited_asyncio_loop", lambda: None)
    monkeypatch.setattr(pj, "keep_display_awake", lambda *a, **k: contextlib.nullcontext())
    monkeypatch.setattr(pj, "_watch_screen_lock", lambda *a, **k: contextlib.nullcontext())
    monkeypatch.setattr(pj, "_write_draft_status", lambda *a, **k: None)
    monkeypatch.setattr(pj, "profile_key_for", lambda label: label.rstrip("b"))  # Doc19b — тот же профиль
    monkeypatch.setattr(pj, "_start_stuck_watch", lambda *a, **k: None)
    monkeypatch.setenv("ACL_CANCEL_DEADLINE_S", "0.3")
    threads: list[threading.Thread] = []
    made: list[str] = []
    wake_events: list[threading.Event] = []

    def start(label: str) -> str:
        job_id = pj.create_job(label, "CL", [{}], total=1, campaign_names=["CAMP"])
        made.append(job_id)
        t = threading.Thread(target=pj._run_job, args=(job_id, [{}]), kwargs={
            "account_label": label, "country": "CL", "via_multilogin": True,
            "activate": False, "dry_run": False, "hand_off": True,
        }, daemon=True, name=f"publish-{job_id[:8]}")
        threads.append(t)
        t.start()
        return job_id

    def gate() -> threading.Event:
        ev = threading.Event()
        wake_events.append(ev)
        return ev

    yield SimpleNamespace(start=start, slots=slots, gate=gate, threads=threads)
    for ev in wake_events:
        ev.set()
    for t in threads:
        t.join(timeout=5)
    with pj._jobs_lock:
        for job_id in made:
            pj._jobs.pop(job_id, None)
    for job_id in made:
        pj._job_ended.pop(job_id, None)
        rt._ABANDONED.discard(job_id)


def _running_ids(slots) -> list[str]:
    return [r["job_id"] for r in slots.snapshot()["running"]]


def _ok_result(draft):
    return RpaPublishResult(campaign_name=draft.name, success=True)


# ── зависший шаг: срок, место, проснувшийся поток ──────────────────────────

def test_a_job_stuck_in_a_step_is_cancelled_at_the_deadline_and_frees_its_slot(env):
    entered, wake = threading.Event(), env.gate()

    def stuck(pub, draft):
        entered.set()
        wake.wait(20)                  # один вызов CDP, который не возвращается
        return _ok_result(draft)

    FakePublisher.behaviour = stuck
    job_id = env.start("Doc19")
    assert entered.wait(5)
    assert _running_ids(env.slots) == [job_id]

    t0 = time.monotonic()
    assert pj.cancel_job(job_id)["ok"] is True
    assert _until(lambda: pj.get_job(job_id)["status"] == "cancelled", timeout=3)
    assert time.monotonic() - t0 < 3

    snap = pj.get_job(job_id)
    assert snap["abandoned"] is True
    assert any("Stopped by the user" in e and "stuck in" in e
               and "Multilogin window was left open" in e for e in snap["errors"])
    assert _running_ids(env.slots) == [], "the slot must be free so the queue moves on"
    assert FakePublisher.instances[0].closed_in == [], "nobody closes the connection under a stuck call"


def test_a_stuck_job_names_the_last_step_it_logged(env):
    entered, wake = threading.Event(), env.gate()

    def stuck(pub, draft):
        rt.record("simple_create", True, note="after_continue")
        entered.set()
        wake.wait(20)
        return _ok_result(draft)

    FakePublisher.behaviour = stuck
    job_id = env.start("Doc19")
    assert entered.wait(5)
    pj.cancel_job(job_id)
    assert _until(lambda: pj.get_job(job_id)["status"] == "cancelled", timeout=3)
    assert "stuck in simple create;" in " ".join(pj.get_job(job_id)["errors"])
    assert pj.get_job(job_id)["stuck_step"] == "simple create"


def test_the_next_job_on_the_profile_starts_after_the_abandon(env):
    entered, wake = threading.Event(), env.gate()
    second_in = threading.Event()

    def behave(pub, draft):
        if not entered.is_set():
            entered.set()
            wake.wait(20)
            return _ok_result(draft)
        second_in.set()
        return _ok_result(draft)

    FakePublisher.behaviour = behave
    first = env.start("Doc19")
    assert entered.wait(5)
    second = env.start("Doc19b")       # тот же профиль, другой аккаунт: ждёт первое
    assert _until(lambda: pj.get_job(second)["status"] == "queued")
    pj.cancel_job(first)
    assert second_in.wait(5), "the queue must move on after the stuck job was abandoned"
    assert _until(lambda: pj.get_job(second)["status"] == "done")


def test_the_abandoned_thread_waking_up_writes_and_frees_nothing(env):
    entered, wake, woke = threading.Event(), env.gate(), threading.Event()

    def stuck(pub, draft):
        entered.set()
        wake.wait(20)
        rt.record("late_step", True)         # телеметрия брошенного задания
        woke.set()
        return _ok_result(draft)

    FakePublisher.behaviour = stuck
    first = env.start("Doc19")
    assert entered.wait(5)
    pj.cancel_job(first)
    assert _until(lambda: pj.get_job(first)["status"] == "cancelled", timeout=3)

    # Место отдано второму заданию другого аккаунта.
    grant = env.slots.acquire("other-job", "KLD", cancelled=lambda: False)
    assert grant is not None and _running_ids(env.slots) == ["other-job"]
    before = pj.get_job(first)

    wake.set()
    assert woke.wait(5)
    assert _until(lambda: not env.threads[0].is_alive())

    after = pj.get_job(first)
    assert after["status"] == "cancelled"
    assert after["results"] == before["results"] == []
    assert after["errors"] == before["errors"]
    assert after["done_count"] == 0 and after["finished_at"] == before["finished_at"]
    assert _running_ids(env.slots) == ["other-job"], "a woken thread must not touch the slot again"
    rows = rt.load() if hasattr(rt, "load") else []
    assert not [r for r in rows if r.get("step") == "late_step"], "no telemetry into the finished job"
    env.slots.release("other-job")


def test_a_thread_that_ends_in_time_is_not_abandoned(env):
    FakePublisher.behaviour = lambda pub, draft: _ok_result(draft)
    job_id = env.start("Doc19")
    assert _until(lambda: pj.get_job(job_id)["status"] == "done")
    assert pj.cancel_job(job_id)["ok"] is False
    assert not pj.get_job(job_id).get("abandoned")


# ── кооперативная отмена ───────────────────────────────────────────────────

def test_cancel_inside_a_wait_helper_raises_job_cancelled_and_ends_normally(env):
    from src.fb_ads_rpa import meta_dom as md

    entered = threading.Event()

    def polling(pub, draft):
        entered.set()
        while True:                    # цикл ожидания, как в meta_dom
            md._sleep_h(0.01)

    FakePublisher.behaviour = polling
    job_id = env.start("Doc19")
    assert entered.wait(5)
    t0 = time.monotonic()
    pj.cancel_job(job_id)
    assert _until(lambda: pj.get_job(job_id)["status"] == "cancelled", timeout=3)
    assert time.monotonic() - t0 < 2, "a cooperative cancel needs far less than the deadline"
    snap = pj.get_job(job_id)
    assert not snap.get("abandoned"), "the thread ended by itself, nobody abandoned it"
    assert snap["errors"] == ["Stopped by the user"]
    pub = FakePublisher.instances[0]
    # Соединение закрыл сам поток задания, а не поток отмены.
    assert pub.closed_in and all(n.startswith("publish-") for n in pub.closed_in)
    assert _until(lambda: _running_ids(env.slots) == [] or env.slots.snapshot()["running"][0]["kept"])


def test_job_cancel_check_raises_only_for_a_bound_set_event():
    job_cancel.check()                               # не привязан — ничего
    ev = threading.Event()
    job_cancel.bind(ev)
    try:
        job_cancel.check()                           # не установлен — ничего
        ev.set()
        with pytest.raises(job_cancel.JobCancelled):
            job_cancel.check()
        # BaseException: `except Exception` в мягких отказах шагов её не глотает.
        assert not issubclass(job_cancel.JobCancelled, Exception)
    finally:
        job_cancel.unbind()
    job_cancel.check()


def test_the_check_is_cheap_for_a_normal_run():
    ev = threading.Event()
    job_cancel.bind(ev)
    try:
        t0 = time.perf_counter()
        for _ in range(100_000):
            job_cancel.check()
        per_call = (time.perf_counter() - t0) / 100_000
    finally:
        job_cancel.unbind()
    assert per_call < 20e-6


def test_the_flow_modules_check_for_cancel_in_their_waits():
    import inspect

    from src.fb_ads_rpa import ads_manager_flow, crop_step, foreign_windows, meta_dom, pacing, page_liveness, simple_create

    for mod in (ads_manager_flow, simple_create, crop_step):
        assert "job_cancel.check()" in inspect.getsource(mod)
    assert "_job_cancel.check()" in inspect.getsource(meta_dom._sleep_h)
    assert "job_cancel.check()" in inspect.getsource(pacing.pause_short)
    assert "job_cancel.check()" in inspect.getsource(foreign_windows.before_step)
    assert "job_cancel.check()" in inspect.getsource(page_liveness.answers)


# ── Stop никогда не закрывает соединение из чужого потока ───────────────────

def test_publisher_cancel_only_signals_and_never_closes():
    from src.fb_ads_rpa.rpa_publisher import RpaPublisher

    conn = SimpleNamespace(closed=0)
    conn.close = lambda **kw: setattr(conn, "closed", conn.closed + 1)
    pub = RpaPublisher.__new__(RpaPublisher)
    pub._cancelled = False
    pub.cancel_event = threading.Event()
    pub._conn = conn
    pub._entry = object()
    pub._provider = SimpleNamespace(close=lambda *a, **k: conn.__setattr__("closed", conn.closed + 1))

    t = threading.Thread(target=pub.cancel, name="cancel-thread")
    t.start()
    t.join(5)
    assert pub._cancelled is True and pub.cancel_event.is_set()
    assert conn.closed == 0 and pub._conn is conn and pub._entry is not None


def test_cancel_job_never_closes_from_the_cancel_thread(env):
    entered, wake = threading.Event(), env.gate()

    def stuck(pub, draft):
        entered.set()
        wake.wait(20)
        return _ok_result(draft)

    FakePublisher.behaviour = stuck
    job_id = env.start("Doc19")
    assert entered.wait(5)
    pj.cancel_job(job_id)
    assert _until(lambda: pj.get_job(job_id)["status"] == "cancelled", timeout=3)
    time.sleep(0.1)
    assert FakePublisher.instances[0].closed_in == []
    wake.set()
    assert _until(lambda: not env.threads[0].is_alive())
    # Проснувшись, поток закрывает соединение сам, в своём потоке.
    assert all(n.startswith("publish-") for n in FakePublisher.instances[0].closed_in)


# ── то, что должно работать как раньше ──────────────────────────────────────

def test_cancel_while_waiting_for_a_slot_still_works_and_is_not_an_abandon(env):
    entered, wake = threading.Event(), env.gate()

    def hold(pub, draft):
        entered.set()
        wake.wait(20)
        return _ok_result(draft)

    FakePublisher.behaviour = hold
    first = env.start("Doc19")
    assert entered.wait(5)
    second = env.start("KLD")                  # места нет (override=1)
    assert _until(lambda: pj.get_job(second)["status"] == "queued")
    pj.cancel_job(second)
    assert _until(lambda: pj.get_job(second)["status"] == "cancelled", timeout=2)
    assert not pj.get_job(second).get("abandoned")
    assert _running_ids(env.slots) == [first], "the first job keeps its slot"


def test_a_window_left_open_after_a_normal_run_keeps_its_slot(env):
    FakePublisher.behaviour = lambda pub, draft: RpaPublishResult(
        campaign_name=draft.name, success=False, error="boom")
    job_id = env.start("Doc19")
    assert _until(lambda: pj.get_job(job_id)["status"] == "done")
    assert _until(lambda: [r["kept"] for r in env.slots.snapshot()["running"]] == [True])
    assert _running_ids(env.slots) == [job_id]


def test_abandoning_a_finished_job_does_nothing(env):
    FakePublisher.behaviour = lambda pub, draft: _ok_result(draft)
    job_id = env.start("Doc19")
    assert _until(lambda: pj.get_job(job_id)["status"] == "done")
    assert pj._abandon_job(job_id) is False
    assert pj.get_job(job_id)["status"] == "done"


# ── step_stuck ──────────────────────────────────────────────────────────────

def test_step_stuck_fires_once_after_the_idle_threshold(monkeypatch):
    now = {"t": 1000.0}
    monkeypatch.setattr(rt, "_clock", lambda: now["t"])
    monkeypatch.setenv("ACL_STEP_STUCK_S", "300")
    sent, rows = [], []
    from src.fb_ads_rpa import sentry_signal

    monkeypatch.setattr(sentry_signal, "report_step_stuck",
                        lambda step, idle, **kw: sent.append((step, idle, kw)) or "evt")
    real_record = rt.record
    monkeypatch.setattr(rt, "record", lambda step, ok=None, **kw: rows.append((step, ok, kw)))

    job_id = "acl1614-stuck"
    with pj._jobs_lock:
        pj._jobs[job_id] = {"job_id": job_id, "account_label": "Doc19", "status": "running",
                            "results": [], "errors": [], "done_count": 0, "total": 1,
                            "campaign_names": ["CAMP"], "cancel_requested": False}
    try:
        rt._touch_job(job_id, "simple_create")           # последний шаг, t=1000
        state = {"fired": False, "since": 900.0}
        now["t"] = 1299.0
        assert pj._step_stuck_tick(job_id, state, "Doc19") is False
        now["t"] = 1301.0
        assert pj._step_stuck_tick(job_id, state, "Doc19") is True
        now["t"] = 5000.0
        assert pj._step_stuck_tick(job_id, state, "Doc19") is False     # один раз
        assert len(sent) == 1 and len(rows) == 1
        assert sent[0][0] == "simple_create" and sent[0][1] == pytest.approx(301.0)
        assert sent[0][2]["job"] == job_id and sent[0][2]["campaign"] == "CAMP"
        step, ok, kw = rows[0]
        assert step == "step_stuck" and ok is None
        assert kw["diag"] == {"last_step": "simple_create", "idle_s": 301.0}
    finally:
        monkeypatch.setattr(rt, "record", real_record)
        with pj._jobs_lock:
            pj._jobs.pop(job_id, None)
        rt.forget_job(job_id)


def test_step_stuck_is_silent_for_a_job_that_keeps_logging(monkeypatch):
    now = {"t": 0.0}
    monkeypatch.setattr(rt, "_clock", lambda: now["t"])
    monkeypatch.setenv("ACL_STEP_STUCK_S", "300")
    job_id = "acl1614-busy"
    with pj._jobs_lock:
        pj._jobs[job_id] = {"job_id": job_id, "status": "running", "results": [], "errors": [],
                            "campaign_names": [], "done_count": 0}
    try:
        state = {"fired": False, "since": 0.0}
        for _ in range(10):
            now["t"] += 200
            rt._touch_job(job_id, "ad_copy")
            assert pj._step_stuck_tick(job_id, state, "Doc19") is False
    finally:
        with pj._jobs_lock:
            pj._jobs.pop(job_id, None)
        rt.forget_job(job_id)


def test_the_step_stuck_row_does_not_reset_the_stall_clock():
    from src.web.publish_progress import job_progress

    path = rt.TELEMETRY_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    job = {"job_id": "j1", "status": "running", "created_at": "2026-10-06T00:00:00+00:00",
           "started_at": "2026-10-06T00:00:00+00:00", "campaign_names": ["CAMP"], "results": []}
    rows = [
        {"ts": "2026-10-06T00:01:00+00:00", "step": "simple_create", "campaign": "CAMP", "job": "j1"},
        {"ts": "2026-10-06T00:06:00+00:00", "step": "step_stuck", "campaign": "CAMP", "job": "j1"},
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    from datetime import datetime, timezone

    prog = job_progress(job, path=path, now=datetime(2026, 10, 6, 0, 7, tzinfo=timezone.utc))
    assert prog["campaigns"][0]["step"] == "simple_create"
    assert prog["campaigns"][0]["idle_s"] == pytest.approx(360.0)


def test_the_page_says_stopping_then_stopped_never_opening():
    html = (ROOT / "src" / "web" / "templates" / "report.html").read_text(encoding="utf-8")
    assert "p.status === 'cancelling' && !(c && c.result)" in html
    assert "why: 'Stopping…'" in html
    assert "function stoppedText(p, plain)" in html
    assert "stall_|step_stuck$|tab_frozen$" in html
