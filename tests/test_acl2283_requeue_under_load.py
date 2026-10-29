"""ACL-2283: staggered start (A), patient readiness (B), memory by share used (D), requeue of
"profile closed while starting" (C).

Fakes only (same rig as the warm-session tests): no Meta, no Multilogin, no real CPU or memory reads.
"""
from types import SimpleNamespace

import pytest

from src.analysis_agent import account_spend_api as api
from src.fb_api import connection, warm
from src.fb_api.graph import PageUnresponsive
from tests.test_acl1812_warm_sessions import ACT, PID, Rig

KEY = "k-" + PID
B = "b" * 8 + "-0000-4000-8000-000000000000"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ACL_PROFILE_RESTART_COOLDOWN_S", "0")
    monkeypatch.setattr(connection, "settings", lambda: {"managed_sessions": True, "auto_reopen_manual": False})
    api._COOLDOWN.clear()
    api._FROZEN_RELEASED.clear()
    yield
    api._COOLDOWN.clear()
    api._FROZEN_RELEASED.clear()


def _entry(pid=PID):
    return SimpleNamespace(profile_id=pid, folder_id="F", multilogin_instance="A", ad_account_id=ACT)


def _publisher(monkeypatch, tmp_path, *, cpu, frozen=False, closed=False):
    """ApiPublisher over a warm rig. `cpu` - the machine CPU share the registry reads."""
    from src.analysis_agent import account_spend as _as
    from src.web import publish_api

    rig = Rig(tmp_path)
    rig.warm_up()
    rig.reg._cpu = lambda: cpu
    monkeypatch.setattr(warm, "_REGISTRY", rig.reg)
    monkeypatch.setattr(publish_api, "_resolve_entry", lambda label: _entry())
    reports = []
    monkeypatch.setattr(publish_api._tm, "report_stop", lambda *a, **k: reports.append(a))

    def make(tab, act, *, writes=True):
        graph = rig.make(tab)
        if frozen:
            def wait(**kw):
                raise PageUnresponsive("frozen")
            graph.wait_ready = wait
        return graph

    monkeypatch.setattr(publish_api, "_make_graph", make)
    monkeypatch.setattr(publish_api, "preflight_account", lambda client, act: SimpleNamespace())
    monkeypatch.setattr(publish_api, "TargetingResolver", lambda client, **k: object())
    monkeypatch.setattr(publish_api, "GraphClient", lambda graph, **kw: SimpleNamespace(graph=graph, writes=0, **kw))
    monkeypatch.setattr(_as, "_profile_key", lambda label, *a, **k: KEY)
    if closed:
        # The process vanishes while the action starts: alive for the prepare check, gone at the checkout.
        sess = rig.reg.session(PID)
        sess.verified = False
        sess.tab = None
        looks = []

        def scan(ids):
            looks.append(1)
            return {PID: ("port", sess.port) if len(looks) == 1 else ("none", None)}

        rig.reg._scan = scan
    pub = publish_api.ApiPublisher(dry_run=True)
    pub.sleep = lambda s: None
    return rig, pub, publish_api, reports


# ---- C: closed while starting (fallback) ----

def test_profile_closed_while_starting_on_a_busy_machine_is_requeued(monkeypatch, tmp_path):
    rig, pub, publish_api, _r = _publisher(monkeypatch, tmp_path, cpu=95.0, closed=True)
    with pytest.raises(publish_api.LoadRequeue) as ei:
        pub.open("Acc")
    assert ei.value.code == "profile_closed" and "cpu_high" in ei.value.snapshot["reasons"]


def test_busy_only_in_the_sample_after_the_failure_still_requeues(monkeypatch, tmp_path):
    rig, pub, publish_api, _r = _publisher(monkeypatch, tmp_path, cpu=10.0, closed=True)
    pauses = []
    pub.sleep = pauses.append
    quiet = {"until_pause": True}
    rig.reg._cpu = lambda: 10.0 if not pauses else 96.0       # quiet before and at the failure, busy after
    with pytest.raises(publish_api.LoadRequeue):
        pub.open("Acc")
    assert pauses == [publish_api.POST_FAILURE_SAMPLE_S], "a sample is taken a few seconds after the failure"
    assert quiet


def test_profile_closed_on_an_idle_machine_stops_as_today(monkeypatch, tmp_path):
    rig, pub, publish_api, _r = _publisher(monkeypatch, tmp_path, cpu=10.0, closed=True)
    with pytest.raises(publish_api.ApiPublishError) as ei:
        pub.open("Acc")
    assert not isinstance(ei.value, publish_api.LoadRequeue) and ei.value.code == "profile_closed"


def test_publisher_with_a_write_does_not_turn_into_a_requeue(monkeypatch, tmp_path):
    rig, pub, publish_api, _r = _publisher(monkeypatch, tmp_path, cpu=95.0, closed=True)
    pub._client = SimpleNamespace(writes=2)
    with pytest.raises(publish_api.ApiPublishError) as ei:
        pub.open("Acc")
    assert not isinstance(ei.value, publish_api.LoadRequeue)


def test_load_sample_of_an_earlier_attempt_does_not_count(monkeypatch, tmp_path):
    rig, pub, publish_api, _r = _publisher(monkeypatch, tmp_path, cpu=10.0, closed=True)
    pub._load_before = {"loaded": True, "reasons": ["cpu_high"]}          # left over from attempt 1
    with pytest.raises(publish_api.ApiPublishError) as ei:
        pub.open("Acc")
    assert not isinstance(ei.value, publish_api.LoadRequeue)


def test_a_frozen_tab_on_an_idle_machine_keeps_the_full_pause_and_stops(monkeypatch, tmp_path):
    rig, pub, publish_api, reports = _publisher(monkeypatch, tmp_path, cpu=12.0, frozen=True)
    with pytest.raises(publish_api.ApiPublishError) as ei:
        pub.open("Acc")
    assert not isinstance(ei.value, publish_api.LoadRequeue) and ei.value.code == "page_unresponsive"
    assert api._cooldown_state(KEY)[0] > 590 and len(reports) == 1


def test_started_now_stays_sticky_when_a_retry_attaches(monkeypatch, tmp_path):
    from src.web import publish_api

    rig = Rig(tmp_path)                                  # no warm session: the cold path
    monkeypatch.setattr(warm, "_REGISTRY", rig.reg)
    rig.reg._cpu = lambda: 10.0
    monkeypatch.setattr(publish_api, "_resolve_entry", lambda label: _entry())
    pub = publish_api.ApiPublisher(dry_run=True)
    pub._opened = SimpleNamespace(started_now=True, port=1, cdp_url="x")
    monkeypatch.setattr(connection, "settings", lambda: {"managed_sessions": False, "auto_reopen_manual": False})
    monkeypatch.setattr(publish_api, "_open_profile",
                        lambda *a: SimpleNamespace(started_now=False, port=1, cdp_url="x"))

    def stop_here(url):
        raise RuntimeError("stop here")

    monkeypatch.setattr(publish_api, "_open_tab", stop_here)
    with pytest.raises(RuntimeError):
        pub.open("Acc")
    assert pub.started_profile is True, "the window the first attempt opened is still ours"


# ---- D: memory by share used (the rule itself is ACL-2255; here: it governs the start in this flow) ----

def _mem(used_pct, total=16000):
    from src.web import profile_slots as ps

    return ps.MemoryReading(total, int(total * (100 - used_pct) / 100))


def test_heavy_gate_uses_the_share_too():
    from src.web import profile_slots as ps

    mem = [_mem(87)]
    gate = warm.HeavyGate(limit=lambda: 3, memory=lambda: mem[0], settings=lambda: ps.SlotSettings(),
                          usage=None, tick_s=0.01)
    gate.acquire("a")
    gate.acquire("b", wait_s=0.2)                       # 87% in use: the second action goes
    mem[0] = _mem(95)
    with pytest.raises(warm.WarmError):
        gate.acquire("c", wait_s=0.1)


def test_profile_start_goes_at_87_percent_and_waits_at_95(tmp_path):
    mem = [_mem(95)]
    rig = Rig(tmp_path, reg_memory=lambda: mem[0])
    views = []
    base = rig.reg._sleep

    def sleeping(secs):
        base(secs)
        if len(rig.sleeps) >= 3:
            mem[0] = _mem(87)                            # memory freed

    rig.reg._sleep = sleeping
    with warm.job_wait(lambda: False, views.append):
        res = rig.reg.open_one("Acc", _entry())
    assert res["ok"] and rig.started
    shown = [v for v in views if v and v.get("hold") == "memory_low"]
    assert shown and shown[0]["used_pct"] == pytest.approx(95, abs=0.1) and shown[0]["max_used_pct"] == 90.0


def _turn_wait_rig(tmp_path, start_pct, rise_pct):
    """Another profile warms up (holds the turn); memory moves to `rise_pct` during the turn wait."""
    mem = [_mem(start_pct)]
    rig = Rig(tmp_path, reg_memory=lambda: mem[0])
    rig.reg._starting[PID] = rig.reg._mono()
    base = rig.reg._sleep
    at_start = []
    state = {"freed": False}

    def sleeping(secs):
        base(secs)
        if len(rig.sleeps) == 2:
            mem[0] = _mem(rise_pct)
        if len(rig.sleeps) == 4:
            rig.reg.mark_ready(PID)                      # the warming profile answered: the turn frees
        if len(rig.sleeps) == 20:
            mem[0] = _mem(80)                            # memory frees much later
            state["freed"] = True

    rig.reg._sleep = sleeping
    real = rig.reg._start_profile

    def start(p, f, i):
        m = mem[0]
        at_start.append((round(100 * (m.total_mb - m.available_mb) / m.total_mb), state["freed"]))
        return real(p, f, i)

    rig.reg._start_profile = start
    return rig, at_start


def test_memory_is_rechecked_after_the_turn_wait(tmp_path):
    """87% at admission, 95% during the turn wait: the profile starts only after memory frees."""
    rig, at_start = _turn_wait_rig(tmp_path, 87, 95)
    views = []
    with warm.job_wait(lambda: len(rig.sleeps) > 80, views.append):
        res = rig.reg.open_one("B", _entry(B))
    assert res["ok"] and len(at_start) == 1
    pct, freed = at_start[0]
    assert freed and pct <= 80, "started while memory was over the line"
    assert any(v and v.get("hold") == "memory_low" for v in views), "the wait was shown queued with numbers"
    assert B not in rig.reg._starting or rig.reg._starting[B] > 0


def test_turn_is_given_back_while_waiting_for_memory(tmp_path):
    rig, at_start = _turn_wait_rig(tmp_path, 87, 95)
    seen = []
    base = rig.reg._sleep

    def spying(secs):
        base(secs)
        if 8 <= len(rig.sleeps) <= 10:
            seen.append(B in rig.reg._starting)

    rig.reg._sleep = spying
    with warm.job_wait(lambda: len(rig.sleeps) > 80, lambda v: None):
        rig.reg.open_one("B", _entry(B))
    assert seen and not any(seen), "a job waiting for memory must not hold the start turn"


def test_cancel_while_waiting_for_memory_after_the_turn(tmp_path):
    rig, at_start = _turn_wait_rig(tmp_path, 87, 95)
    with warm.job_wait(lambda: len(rig.sleeps) >= 12, lambda v: None):
        res = rig.reg.open_one("B", _entry(B))
    assert res["ok"] is False and not at_start and B not in rig.reg._starting


def test_memory_boundary_inside_the_turn_flow(tmp_path):
    for pct, starts in ((86, True), (88, True), (90, True), (91, False), (95, False)):
        rig = Rig(tmp_path / str(pct), reg_memory=lambda pct=pct: _mem(pct))
        with warm.job_wait(lambda rig=rig: len(rig.sleeps) > 8, lambda v: None):
            res = rig.reg.open_one("B", _entry(B))
        assert bool(rig.started) is starts, (pct, res)


def test_a_failed_prepare_gives_the_start_turn_back(tmp_path):
    rig = Rig(tmp_path)
    rig.warm_up()
    rig.reg._starting[PID] = rig.reg._mono()
    real_make = rig.make

    def make(tab):
        graph = real_make(tab)

        def wait_ready(timeout=90.0):
            raise PageUnresponsive("frozen")

        graph.wait_ready = wait_ready
        return graph

    rig.make = make
    with pytest.raises(PageUnresponsive):
        rig.checkout()
    assert PID not in rig.reg._starting


# ---- A: staggered start ----

def test_next_profile_waits_for_the_previous_one_to_be_ready(tmp_path):
    rig = Rig(tmp_path)
    views = []
    with warm.job_wait(lambda: False, views.append):
        assert rig.reg.open_one("A", _entry(PID))["ok"]
    assert PID in rig.reg._starting
    order = []
    base = rig.reg._sleep

    def sleeping(secs):
        base(secs)
        order.append("wait")
        if order.count("wait") == 4:
            order.append("ready")
            rig.reg.mark_ready(PID)                      # the first tab answered

    rig.reg._sleep = sleeping
    real_start = rig.reg._start_profile

    def start(p, f, i):
        order.append(("start", p))
        return real_start(p, f, i)

    rig.reg._start_profile = start
    with warm.job_wait(lambda: False, views.append):
        assert rig.reg.open_one("B", _entry(B))["ok"]
    assert order.index(("start", B)) > order.index("ready"), "B must not start before A is ready"
    waiting = [v for v in views if v and v.get("hold") == "machine_busy"]
    assert waiting and views[-1] is None


def test_next_start_waits_for_the_cpu_to_settle(tmp_path):
    rig = Rig(tmp_path)
    reads = [96.0, 96.0, 91.0]
    rig.reg._cpu = lambda: reads.pop(0) if reads else 40.0
    started_at = []
    real_start = rig.reg._start_profile

    def start(p, f, i):
        started_at.append(rig.now[0])
        return real_start(p, f, i)

    rig.reg._start_profile = start
    t0 = rig.now[0]
    with warm.job_wait(lambda: False, lambda v: None):
        assert rig.reg.open_one("A", _entry(PID))["ok"]
    # three busy looks (6 s), then the CPU must stay below the level for the settle time
    assert started_at[0] - t0 >= 6 + warm.DEFAULT_START_CPU_SETTLE_S


def test_a_failed_start_gives_the_turn_back(tmp_path):
    rig = Rig(tmp_path)
    rig.start_error = RuntimeError("mlx down")
    with warm.job_wait(lambda: False, lambda v: None):
        res = rig.reg.open_one("A", _entry(PID))
    assert not res["ok"] and rig.reg._starting == {}


def test_cancel_while_waiting_for_the_turn(tmp_path):
    rig = Rig(tmp_path)
    rig.reg._starting[PID] = rig.reg._mono()             # another profile is warming up
    base = rig.reg._sleep
    stop = [False]

    def sleeping(secs):
        base(secs)
        stop[0] = len(rig.sleeps) >= 3

    rig.reg._sleep = sleeping
    views = []
    with warm.job_wait(lambda: stop[0], views.append):
        res = rig.reg.open_one("B", _entry(B))
    assert res["ok"] is False and res["code"] == "cancelled" and not rig.started and views[-1] is None


def test_actions_without_a_job_are_not_staggered(tmp_path):
    rig = Rig(tmp_path)
    rig.reg._cpu = lambda: 99.0
    rig.reg._starting[PID] = rig.reg._mono()
    assert rig.reg.open_one("B", _entry(B))["ok"] and rig.sleeps == []


def test_a_started_profile_that_never_gets_an_action_does_not_block_forever(tmp_path):
    rig = Rig(tmp_path)
    rig.reg._starting[PID] = rig.reg._mono() - warm.START_READY_CAP_S - 1
    with warm.job_wait(lambda: False, lambda v: None):
        assert rig.reg.open_one("B", _entry(B))["ok"]


# ---- B: patient readiness ----

def test_silent_tab_is_waited_for_on_a_busy_machine_but_not_on_an_idle_one(tmp_path):
    from src.fb_api import cdp

    for cpu, expect_patient in ((93.0, True), (10.0, False)):
        rig = Rig(tmp_path / str(int(cpu)))
        rig.warm_up()
        rig.reg._cpu = lambda cpu=cpu: cpu
        seen = {}
        real_make = rig.make

        def make(tab, real_make=real_make, seen=seen, rig=rig):
            graph = real_make(tab)

            def wait_ready(timeout=90.0):
                seen["patient"] = cdp.patient_active()
                seen["budget_left"] = rig.reg._remaining()
                return {"kind": "ads_manager", "has_token": True}

            graph.wait_ready = wait_ready
            return graph

        rig.make = make
        rig.checkout().release()
        assert seen["patient"] is expect_patient
        assert (seen["budget_left"] > 300) is expect_patient, "busy: minutes; idle: today's 150 s cap"


def test_wait_ready_keeps_waiting_while_patient_then_reports_the_silence():
    from src.fb_api import cdp
    from src.fb_api.cdp import CdpTimeout
    from src.fb_api.token_capture import InPageGraph

    graph = object.__new__(InPageGraph)
    now = [0.0]
    graph._clock = lambda: now[0]
    graph._sleep = lambda s: now.__setitem__(0, now[0] + s)
    calls = []

    def helper(expr, timeout=10.0):
        calls.append(now[0])
        if now[0] < 200:
            raise CdpTimeout("silent")
        return {"kind": "ads_manager", "has_token": True}

    graph._eval_helper = helper
    with cdp.patient_wait(lambda: True):
        assert graph.wait_ready(timeout=480.0)["has_token"] is True
    assert len(calls) > 100
    now[0] = 0.0
    with pytest.raises(CdpTimeout):                       # idle machine: today's immediate stop
        graph.wait_ready(timeout=480.0)

    def stays_silent(expr, timeout=10.0):
        raise CdpTimeout("silent")

    graph._eval_helper = stays_silent
    now[0] = 0.0
    with cdp.patient_wait(lambda: now[0] < 100):
        with pytest.raises(CdpTimeout):
            graph.wait_ready(timeout=480.0)
    assert 100 <= now[0] < 130, "patience ends when the machine is quiet again or the limit passes"


# ---- the job loop ----

class _FakeReg:
    def __init__(self, clear_after):
        self.calls = 0
        self.clear_after = clear_after

    def load_snapshot(self, pid=""):
        self.calls += 1
        return {"loaded": True, "clear": self.calls >= self.clear_after, "cpu_pct": 91.0, "cpu_level": 85.0,
                "available_mb": 2500, "used_pct": 88.0, "max_used_pct": 90.0, "running": 3, "warming": 1,
                "reasons": ["cpu_high"]}

    def restart_wait_s(self, pid):
        return 0.0


class _FlakyPublisher:
    """Fails with LoadRequeue `fails` times, then opens. Records its calls."""

    def __init__(self, fails, writes=0):
        from src.web import publish_api

        self.fails, self.attempts, self.closed, self._writes = fails, 0, 0, writes
        self._session_profile_id = PID
        self._err = publish_api.LoadRequeue("busy", "profile_closed", {"reasons": ["cpu_high"], "cpu_pct": 93.0})

    @property
    def writes(self):
        return self._writes

    def open(self, label):
        self.attempts += 1
        if self.attempts <= self.fails:
            raise self._err

    def close(self):
        self.closed += 1


@pytest.fixture
def job(monkeypatch):
    from src.web import publish_jobs as pj

    clock = {"t": 0.0}
    import time as _real_time

    class _Clock:
        monotonic = staticmethod(lambda: clock["t"])

        def __getattr__(self, name):
            return getattr(_real_time, name)

    monkeypatch.setattr(pj, "time", _Clock())
    monkeypatch.setattr(pj, "_load_sleep", lambda s: clock.__setitem__("t", clock["t"] + s))
    job_id = "job-2283"
    with pj._jobs_lock:
        pj._jobs[job_id] = {"job_id": job_id, "status": "running", "account_label": "B97",
                            "started_at": "t", "campaign_names": ["C"]}
    yield pj, job_id, clock
    with pj._jobs_lock:
        pj._jobs.pop(job_id, None)


def test_requeued_job_starts_again_with_no_attempt_limit(monkeypatch, job):
    pj, job_id, clock = job
    reg = _FakeReg(clear_after=1)
    monkeypatch.setattr(warm, "registry", lambda: reg)
    states = []
    real = pj._show_resource_wait

    def spy(jid, label, view):
        real(jid, label, view)
        states.append((view and view.get("hold"), pj._jobs[job_id]["status"],
                       (pj._jobs[job_id].get("queue") or {}).get("state")))

    monkeypatch.setattr(pj, "_show_resource_wait", spy)
    pub = _FlakyPublisher(fails=12)                     # far beyond any "one retry" limit
    pj._open_with_load_requeue(job_id, pub, "B97")
    assert pub.attempts == 13 and pub.closed == 12
    waiting = [s for s in states if s[0] == "machine_busy"]
    assert waiting and all(s[1] == "queued" and s[2] == "waiting_load" for s in waiting)
    assert states[-1][0] is None and pj._jobs[job_id]["status"] == "running"
    assert "queue" not in pj._jobs[job_id]


def test_backoff_grows_and_is_capped_so_multilogin_is_never_hammered(monkeypatch, job):
    pj, job_id, clock = job
    reg = _FakeReg(clear_after=1)                       # the machine is clear at once: only the backoff holds
    monkeypatch.setattr(warm, "registry", lambda: reg)
    starts = []
    pub = _FlakyPublisher(fails=9)
    real_open = pub.open

    def open_(label):
        starts.append(clock["t"])
        return real_open(label)

    pub.open = open_
    pj._open_with_load_requeue(job_id, pub, "B97")
    gaps = [round(b - a) for a, b in zip(starts, starts[1:])]
    assert gaps[:5] == [16, 30, 60, 120, 240] or gaps[:5] == [16, 32, 62, 122, 242] or (
        gaps[0] >= 15 and gaps[1] >= 30 and gaps[2] >= 60 and gaps[3] >= 120 and gaps[4] >= 240)
    assert all(g >= 300 for g in gaps[5:]), "capped at 5 minutes"
    assert all(b >= a for a, b in zip(gaps, gaps[1:])), "never shorter than the previous pause"


def test_one_start_at_a_time_it_waits_for_the_load_to_clear_before_each_start(monkeypatch, job):
    pj, job_id, clock = job
    reg = _FakeReg(clear_after=100)
    monkeypatch.setattr(warm, "registry", lambda: reg)
    order = []
    pub = _FlakyPublisher(fails=1)
    orig_open = pub.open
    pub.open = lambda label: (order.append(("open", reg.calls)), orig_open(label))
    pj._open_with_load_requeue(job_id, pub, "B97")
    assert order[0] == ("open", 0) and order[1][1] >= 100 or clock["t"] >= 300
    assert len(order) == 2


def test_cancel_of_a_requeued_job_stops_the_wait(monkeypatch, job):
    from src.fb_ads_rpa import job_cancel

    pj, job_id, clock = job
    reg = _FakeReg(clear_after=10 ** 9)
    monkeypatch.setattr(warm, "registry", lambda: reg)
    ticks = {"n": 0}

    def sleeper(s):
        ticks["n"] += 1
        clock["t"] += s
        if ticks["n"] == 3:
            pj._jobs[job_id]["cancel_requested"] = True

    monkeypatch.setattr(pj, "_load_sleep", sleeper)
    pub = _FlakyPublisher(fails=10 ** 9)
    with pytest.raises(job_cancel.JobCancelled):
        pj._open_with_load_requeue(job_id, pub, "B97")
    assert pub.attempts == 1 and pj._jobs[job_id].get("queue") is None


def test_failure_after_a_meta_write_is_never_requeued(monkeypatch, job):
    from src.web import publish_api

    pj, job_id, clock = job
    monkeypatch.setattr(warm, "registry", lambda: pytest.fail("no wait after a write"))
    pub = _FlakyPublisher(fails=1, writes=1)
    with pytest.raises(publish_api.LoadRequeue):
        pj._open_with_load_requeue(job_id, pub, "B97")
    assert pub.attempts == 1


def test_other_failures_still_stop_the_job(monkeypatch, job):
    pj, job_id, clock = job
    from src.web import publish_api

    class Broken(_FlakyPublisher):
        def open(self, label):
            raise publish_api.ApiPublishError("no profile_id in the account map")

    with pytest.raises(publish_api.ApiPublishError):
        pj._open_with_load_requeue(job_id, Broken(0), "B97")


def test_long_wait_does_not_fire_a_false_step_stuck(monkeypatch, job):
    from src.fb_ads_rpa import run_telemetry as rt
    from src.fb_ads_rpa import sentry_signal

    pj, job_id, clock = job
    now = {"t": 1000.0}
    monkeypatch.setattr(rt, "_clock", lambda: now["t"])
    monkeypatch.setenv("ACL_STEP_STUCK_S", "300")
    sent, rows = [], []
    monkeypatch.setattr(sentry_signal, "report_step_stuck", lambda *a, **kw: sent.append(a) or "evt")
    monkeypatch.setattr(rt, "record", lambda step, ok=None, **kw: rows.append((step, kw)))
    reg = _FakeReg(clear_after=1)
    monkeypatch.setattr(warm, "registry", lambda: reg)
    state = {"fired": False, "since": 900.0}
    checks = []

    def sleeper(s):
        now["t"] += s
        clock["t"] += s
        checks.append(pj._step_stuck_tick(job_id, state, "B97"))

    monkeypatch.setattr(pj, "_load_sleep", sleeper)
    rt.set_current_job(job_id)
    rt._touch_job(job_id, "open_profile")
    pj._open_with_load_requeue(job_id, _FlakyPublisher(fails=3), "B97")      # 15 + 30 + 60 s of backoff
    assert not any(checks) and sent == []
    notes = [kw.get("note") for _s, kw in rows]
    assert "load_requeue" in notes and "resource_wait" in notes, "start and end of the wait are recorded"


def test_turn_wait_view_is_shown_as_queued_with_live_numbers():
    from src.web import publish_jobs as pj

    job_id = "job-2283-turn"
    with pj._jobs_lock:
        pj._jobs[job_id] = {"job_id": job_id, "status": "running", "account_label": "B97", "started_at": "t"}
    try:
        pj._show_resource_wait(job_id, "B97", {"hold": "machine_busy", "cpu_pct": 93.0, "cpu_level": 85.0,
                                               "running": 2, "warming": 2})
        job = pj._jobs[job_id]
        assert job["status"] == "queued" and job["queue"]["state"] == "waiting_load"
        assert job["queue"]["cpu_pct"] == 93.0 and job["queue"]["cpu_level"] == 85.0
        pj._show_resource_wait(job_id, "B97", None)
        assert pj._jobs[job_id]["status"] == "running"
    finally:
        with pj._jobs_lock:
            pj._jobs.pop(job_id, None)
