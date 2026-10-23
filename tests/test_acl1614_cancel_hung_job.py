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
from tests.profile_lease_fakes import fake_profile

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
def env(monkeypatch, request):
    FakePublisher.instances = []
    FakePublisher.behaviour = None
    probe = ps.ProfileUsage(lambda profiles: {}, clock=time.monotonic, background=False)
    slots = ps.ProfileSlots(
        memory=lambda: ps.MemoryReading(total_mb=32 * 1024, available_mb=64 * 1024),
        settings=lambda: ps.SlotSettings(override=getattr(request, "param", 1), warmup_s=0),
        tick_s=0.02, usage=probe)
    monkeypatch.setattr(pj, "_slots", slots)
    monkeypatch.setattr(pj, "RpaPublisher", FakePublisher)
    monkeypatch.setattr(pj, "use_rpa_publish", lambda: True)
    monkeypatch.setattr(pj, "_clear_inherited_asyncio_loop", lambda: None)
    monkeypatch.setattr(pj, "keep_display_awake", lambda *a, **k: contextlib.nullcontext())
    monkeypatch.setattr(pj, "_watch_screen_lock", lambda *a, **k: contextlib.nullcontext())
    monkeypatch.setattr(pj, "_write_draft_status", lambda *a, **k: None)
    monkeypatch.setattr(pj, "profile_key_for", lambda label: fake_profile(label.rstrip("b")))  # Doc19b — тот же профиль
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


@pytest.mark.parametrize("env", [2], indirect=True)
def test_the_same_profile_waits_for_the_stopped_robot_other_profiles_go_on(env):
    """Решение по ревью PR #622 (B2/B3, вариант b): замок профиля не выбрасываем.
    Два робота на одном браузере недопустимы; другой профиль идёт — место свободно."""
    entered, wake = threading.Event(), env.gate()
    started: list[str] = []

    def behave(pub, draft):
        started.append(threading.current_thread().name)
        if len(started) == 1:
            entered.set()
            wake.wait(20)
        return _ok_result(draft)

    FakePublisher.behaviour = behave
    first = env.start("Doc19")
    assert entered.wait(5)
    same = env.start("Doc19b")            # тот же профиль
    pj.cancel_job(first)
    assert _until(lambda: pj.get_job(first)["status"] == "cancelled", timeout=3)
    other = env.start("KLD")              # другой профиль, место свободно
    assert _until(lambda: pj.get_job(other)["status"] == "done", timeout=5)

    # Тот же профиль не стартует и говорит почему.
    assert _until(lambda: (pj.get_job(same).get("queue") or {}).get("hold") == "stopped_robot", timeout=5)
    time.sleep(0.4)
    assert pj.get_job(same)["status"] == "queued"
    assert len(started) == 2, "a second robot must not drive the profile of a stopped one"

    wake.set()                             # старый поток вышел — профиль свободен
    assert _until(lambda: pj.get_job(same)["status"] == "done", timeout=8)
    assert "queue" not in pj.get_job(same)


def test_a_job_waiting_for_the_stopped_robot_can_still_be_cancelled(env):
    entered, wake = threading.Event(), env.gate()

    def stuck(pub, draft):
        entered.set()
        wake.wait(20)
        return _ok_result(draft)

    FakePublisher.behaviour = stuck
    first = env.start("Doc19")
    assert entered.wait(5)
    pj.cancel_job(first)
    assert _until(lambda: pj.get_job(first)["status"] == "cancelled", timeout=3)
    same = env.start("Doc19b")
    assert _until(lambda: (pj.get_job(same).get("queue") or {}).get("hold") == "stopped_robot", timeout=5)
    pj.cancel_job(same)
    assert _until(lambda: pj.get_job(same)["status"] == "cancelled", timeout=3)


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


# ── ревью PR #622: хвост ────────────────────────────────────────────────────

def test_a_woken_abandoned_thread_never_calls_the_slot_queue_again(env, monkeypatch):
    """N1: `_claim_slot` — единственная защита от `keep`/`release` брошенного потока."""
    entered, wake = threading.Event(), env.gate()
    calls: list[tuple[str, str]] = []

    def spy(name):
        real = getattr(env.slots, name)

        def wrapped(job_id, *a, **k):
            calls.append((name, job_id))
            return real(job_id, *a, **k)

        monkeypatch.setattr(env.slots, name, wrapped)

    spy("keep")
    spy("release")

    def stuck(pub, draft):
        entered.set()
        wake.wait(20)
        return _ok_result(draft)

    FakePublisher.behaviour = stuck
    job_id = env.start("Doc19")
    assert entered.wait(5)
    assert pj._job_ran_in_browser(job_id), "the woken thread must look like it ran in the browser"
    pj.cancel_job(job_id)
    assert _until(lambda: pj.get_job(job_id)["status"] == "cancelled", timeout=3)
    assert calls == [("release", job_id)]
    wake.set()
    assert _until(lambda: not env.threads[0].is_alive())
    assert calls == [("release", job_id)], "the woken thread touched the slot queue again"


def test_a_step_that_swallows_exceptions_still_lets_the_cancel_through(env):
    """N2: мягкие отказы шагов ловят `except Exception` — Stop сквозь них проходит."""
    from src.fb_ads_rpa import meta_dom as md

    entered = threading.Event()

    def soft(pub, draft):
        entered.set()
        while True:
            try:
                md._sleep_h(0.01)
            except Exception:
                pass

    FakePublisher.behaviour = soft
    job_id = env.start("Doc19")
    assert entered.wait(5)
    pj.cancel_job(job_id)
    assert _until(lambda: pj.get_job(job_id)["status"] == "cancelled", timeout=3)
    assert not pj.get_job(job_id).get("abandoned"), "the cancel was swallowed by a step"


def test_quiet_waits_keep_the_job_alive_for_the_step_stuck_watch(monkeypatch):
    """N3: ожидание человека / сжатие не пишут строк — heartbeat, а не зависание."""
    now = {"t": 1000.0}
    monkeypatch.setattr(rt, "_clock", lambda: now["t"])
    monkeypatch.setenv("ACL_STEP_STUCK_S", "300")
    job_id = "acl1614-quiet"
    with pj._jobs_lock:
        pj._jobs[job_id] = {"job_id": job_id, "status": "running", "results": [], "errors": [],
                            "campaign_names": [], "done_count": 0}
    box: dict = {}

    def thread():
        rt.set_current_job(job_id)
        job_cancel.bind(threading.Event())
        try:
            rt._touch_job(job_id, "ad_copy")
            for _ in range(10):
                now["t"] += 200            # 2000 с тишины в сумме
                job_cancel.heartbeat()
            box["last"] = rt.last_activity(job_id)
        finally:
            job_cancel.unbind()

    try:
        t = threading.Thread(target=thread)
        t.start()
        t.join(5)
        assert box["last"][1] == "ad_copy", "the heartbeat must keep the last step's name"
        assert pj._step_stuck_tick(job_id, {"fired": False, "since": 0.0}, "Doc19") is False
    finally:
        with pj._jobs_lock:
            pj._jobs.pop(job_id, None)
        rt.forget_job(job_id)


def test_ffmpeg_wait_is_cancelled_and_terminates_the_child():
    from src.fb_ads_rpa import creative_compress as cc

    ev = threading.Event()
    box: dict = {}

    def run():
        job_cancel.bind(ev)
        try:
            cc._run([sys.executable, "-c", "import time; time.sleep(60)"], 120)
        except BaseException as exc:      # noqa: BLE001
            box["exc"] = exc
        finally:
            job_cancel.unbind()

    t = threading.Thread(target=run)
    t0 = time.monotonic()
    t.start()
    time.sleep(0.7)
    ev.set()
    t.join(10)
    assert not t.is_alive() and isinstance(box.get("exc"), job_cancel.JobCancelled)
    assert time.monotonic() - t0 < 5, "the child was not terminated on Stop"


def test_ffmpeg_run_still_returns_output_and_code():
    from src.fb_ads_rpa import creative_compress as cc

    code, text = cc._run([sys.executable, "-c", "import sys; print('out'); sys.stderr.write('err')"], 30)
    assert code == 0 and "out" in text and "err" in text


def test_the_window_and_the_page_are_left_alone_after_stop():
    import inspect

    from src.fb_ads_rpa import ads_manager_flow, meta_dom

    flow = inspect.getsource(ads_manager_flow)
    assert flow.index("if not job_cancel.requested():") < flow.index("put_window_back(self.page)")
    walk = inspect.getsource(meta_dom._walk_creative_wizard)
    assert walk.index("_job_cancel.requested()") < walk.index("_open_dialog_count(page)")


def test_a_stop_that_arrived_before_the_publisher_registered_still_reaches_it(env):
    """N4: Stop между проверкой в `_run_job_inner` и регистрацией публикатора."""
    pj_id = pj.create_job("Doc19", "CL", [{}], total=1, campaign_names=["CAMP"])
    with pj._jobs_lock:
        pj._jobs[pj_id]["cancel_requested"] = True
        pj._jobs[pj_id]["status"] = "cancelling"
    seen = threading.Event()

    def poll(pub, draft):
        seen.set()
        return _ok_result(draft)

    FakePublisher.behaviour = poll
    pj._run_rpa(pj_id, [{}], account_label="Doc19", country="CL", activate=False, hand_off=True)
    assert FakePublisher.instances[0].cancel_event.is_set()
    assert not seen.is_set()
    assert pj.get_job(pj_id)["status"] == "cancelled"
    job_cancel.unbind()
    with pj._jobs_lock:
        pj._jobs.pop(pj_id, None)


def test_job_ended_events_do_not_leak(env):
    FakePublisher.behaviour = lambda pub, draft: _ok_result(draft)
    made = pj.create_job("KLD", "CL", [{}], total=1, campaign_names=["CAMP"])
    assert made not in pj._job_ended, "create_job alone must not leave an event behind"
    job_id = env.start("Doc19")
    assert _until(lambda: pj.get_job(job_id)["status"] == "done")
    assert _until(lambda: job_id not in pj._job_ended)
    with pj._jobs_lock:
        pj._jobs.pop(made, None)


# ── ревью PR #622, раунд 2 ──────────────────────────────────────────────────

def test_with_one_slot_a_stopped_profile_does_not_block_other_accounts(env):
    """R2-B1: ожидающее брошенного робота задание места не держит."""
    entered, wake = threading.Event(), env.gate()
    started: list[str] = []

    def behave(pub, draft):
        started.append(threading.current_thread().name)
        if len(started) == 1:
            entered.set()
            wake.wait(20)
        return _ok_result(draft)

    FakePublisher.behaviour = behave
    first = env.start("Doc19")
    assert entered.wait(5)
    pj.cancel_job(first)
    assert _until(lambda: pj.get_job(first)["status"] == "cancelled", timeout=3)
    same = env.start("Doc19b")                  # тот же профиль P
    assert _until(lambda: (pj.get_job(same).get("queue") or {}).get("hold") == "stopped_robot", timeout=5)
    other = env.start("KLD")                    # профиль Q, место одно
    assert _until(lambda: pj.get_job(other)["status"] == "done", timeout=5)
    snap = env.slots.snapshot()
    assert same not in [w["job_id"] for w in snap["waiting"]] + _running_ids(env.slots)
    assert pj.get_job(same)["status"] == "queued" and len(started) == 2
    # Stop снимает ожидание.
    pj.cancel_job(same)
    assert _until(lambda: pj.get_job(same)["status"] == "cancelled", timeout=3)


def test_the_profile_lock_is_unchanged_by_an_abandon(env):
    from src.fb_ads_rpa.operations import profile_lock

    entered, wake = threading.Event(), env.gate()

    def stuck(pub, draft):
        entered.set()
        wake.wait(20)
        return _ok_result(draft)

    FakePublisher.behaviour = stuck
    lock = profile_lock(fake_profile("Doc19"))
    first = env.start("Doc19")
    assert entered.wait(5)
    pj.cancel_job(first)
    assert _until(lambda: pj.get_job(first)["status"] == "cancelled", timeout=3)
    assert profile_lock(fake_profile("Doc19")) is lock, "two robots must never drive one browser"


def test_the_takeover_and_teach_loops_send_the_heartbeat(monkeypatch):
    from src.fb_ads_rpa import pacing

    monkeypatch.setattr(pacing.time, "sleep", lambda s: None)
    beats = []

    def beat():
        beats.append(1)
        pacing.release_takeover()
        pacing._TEACH_WAITING = False

    monkeypatch.setattr(job_cancel, "heartbeat", beat)
    pacing.request_takeover()
    try:
        assert pacing.wait_for_human_release(None, max_wait_s=5) is True
        assert len(beats) == 1
        pacing._TEACH_WAITING = True
        assert pacing.wait_for_teach_resume(5) is True
        assert len(beats) == 2
    finally:
        pacing.release_takeover()
        pacing._TEACH_WAITING = False


def test_a_job_queued_before_the_abandon_gives_its_slot_back(env):
    """R2-B1: задание, ждавшее место до отказа от робота, место не удерживает."""
    entered, wake = threading.Event(), env.gate()
    started: list[str] = []

    def behave(pub, draft):
        started.append(threading.current_thread().name)
        if len(started) == 1:
            entered.set()
            wake.wait(20)
        return _ok_result(draft)

    FakePublisher.behaviour = behave
    first = env.start("Doc19")
    assert entered.wait(5)
    same = env.start("Doc19b")                  # уже в очереди за местом / профилем
    other = env.start("KLD")
    pj.cancel_job(first)
    assert _until(lambda: pj.get_job(other)["status"] == "done", timeout=8)
    assert pj.get_job(same)["status"] == "queued" and len(started) == 2
    assert same not in _running_ids(env.slots)
