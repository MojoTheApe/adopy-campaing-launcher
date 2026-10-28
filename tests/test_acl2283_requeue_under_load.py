"""ACL-2283: a start that fails because the machine is loaded goes back to the queue (no attempt limit).

Fakes only (same rig as the warm-session tests): no Meta, no Multilogin, no real CPU or memory reads.
"""
from types import SimpleNamespace

import pytest

from src.analysis_agent import account_spend_api as api
from src.fb_api import connection, warm
from src.fb_api.graph import PageUnresponsive
from tests.test_acl1812_warm_sessions import ACT, PID, Rig

KEY = "k-" + PID


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ACL_PROFILE_RESTART_COOLDOWN_S", "0")
    monkeypatch.setattr(connection, "settings", lambda: {"managed_sessions": True, "auto_reopen_manual": False})
    api._COOLDOWN.clear()
    api._FROZEN_RELEASED.clear()
    yield
    api._COOLDOWN.clear()
    api._FROZEN_RELEASED.clear()


def _entry():
    return SimpleNamespace(profile_id=PID, folder_id="F", multilogin_instance="A", ad_account_id=ACT)


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
        # The process vanishes while the action starts: `_verify_process` says "closed".
        sess = rig.reg.session(PID)
        sess.verified = False
        sess.tab = None
        looks = []

        def scan(ids):          # alive for the prepare check, gone when the checkout verifies it
            looks.append(1)
            return {PID: ("port", sess.port) if len(looks) == 1 else ("none", None)}

        rig.reg._scan = scan
    return rig, publish_api.ApiPublisher(dry_run=True), publish_api, reports


# ---- how load is told from a frozen tab ----

def test_first_start_page_unresponsive_under_load_is_requeued_without_a_pause(monkeypatch, tmp_path):
    rig, pub, publish_api, reports = _publisher(monkeypatch, tmp_path, cpu=93.0, frozen=True)
    with pytest.raises(publish_api.LoadRequeue) as ei:
        pub.open("Acc")
    assert ei.value.code == "page_unresponsive" and "cpu_high" in ei.value.snapshot["reasons"]
    assert api._cooldown_state(KEY)[0] == 0, "a load failure must not start the 10-minute pause"
    assert reports == [] and pub.stop is None and pub.writes == 0


def test_frozen_tab_on_an_idle_machine_keeps_the_full_pause_and_stops(monkeypatch, tmp_path):
    rig, pub, publish_api, reports = _publisher(monkeypatch, tmp_path, cpu=12.0, frozen=True)
    with pytest.raises(publish_api.ApiPublishError) as ei:
        pub.open("Acc")
    assert not isinstance(ei.value, publish_api.LoadRequeue) and ei.value.code == "page_unresponsive"
    assert api._cooldown_state(KEY)[0] > 590 and len(reports) == 1


def test_unreadable_cpu_is_not_load_and_stops_as_today(monkeypatch, tmp_path):
    rig, pub, publish_api, _reports = _publisher(monkeypatch, tmp_path, cpu=None, frozen=True)
    with pytest.raises(publish_api.ApiPublishError) as ei:
        pub.open("Acc")
    assert not isinstance(ei.value, publish_api.LoadRequeue)


def test_profile_closed_while_starting_under_load_is_requeued(monkeypatch, tmp_path):
    rig, pub, publish_api, _r = _publisher(monkeypatch, tmp_path, cpu=95.0, closed=True)
    with pytest.raises(publish_api.LoadRequeue) as ei:
        pub.open("Acc")
    assert ei.value.code == "profile_closed"


def test_profile_closed_on_an_idle_machine_stops_as_today(monkeypatch, tmp_path):
    rig, pub, publish_api, _r = _publisher(monkeypatch, tmp_path, cpu=10.0, closed=True)
    with pytest.raises(publish_api.ApiPublishError) as ei:
        pub.open("Acc")
    assert not isinstance(ei.value, publish_api.LoadRequeue) and ei.value.code == "profile_closed"


def test_short_memory_or_warming_profiles_also_count_as_load(tmp_path):
    from src.web import profile_slots as ps

    rig = Rig(tmp_path, reg_memory=lambda: ps.MemoryReading(64000, 1000))
    rig.reg._cpu = lambda: 5.0
    snap = rig.reg.load_snapshot(PID)
    assert snap["loaded"] and "memory_low" in snap["reasons"] and snap["clear"] is False
    rig2 = Rig(tmp_path)
    rig2.reg._cpu = lambda: 5.0
    rig2.warm_up()
    rig2.reg._settling[PID] = rig2.reg._mono()      # another profile started a moment ago
    snap2 = rig2.reg.load_snapshot("this-job-profile")
    assert snap2["loaded"] and "profiles_warming_up" in snap2["reasons"]
    rig3 = Rig(tmp_path)
    rig3.reg._cpu = lambda: 72.0                      # between resume (70) and the load limit (85)
    snap3 = rig3.reg.load_snapshot(PID)
    assert snap3["loaded"] is False and snap3["clear"] is False
    rig3.reg._cpu = lambda: 40.0
    assert rig3.reg.load_snapshot(PID)["clear"] is True


# ---- the job loop ----

class _FakeReg:
    def __init__(self, clear_after):
        self.calls = 0
        self.clear_after = clear_after

    def load_snapshot(self, pid=""):
        self.calls += 1
        return {"loaded": True, "clear": self.calls >= self.clear_after, "cpu_pct": 91.0,
                "available_mb": 2500, "need_mb": 4000, "running": 3, "warming": 1, "reasons": ["cpu_high"]}

    def restart_wait_s(self, pid):
        return 0.0


class _FlakyPublisher:
    """Fails with LoadRequeue `fails` times, then opens. Records its calls."""

    def __init__(self, fails, writes=0):
        from src.web import publish_api

        self.fails, self.attempts, self.closed, self._writes = fails, 0, 0, writes
        self._session_profile_id = PID
        self._err = publish_api.LoadRequeue("busy", "page_unresponsive", {"reasons": ["cpu_high"], "cpu_pct": 93.0})

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

    monkeypatch.setattr(warm, "LOAD_MIN_PAUSE_S", 0.0)
    monkeypatch.setattr(pj, "_load_sleep", lambda s: None)
    job_id = "job-2283"
    with pj._jobs_lock:
        pj._jobs[job_id] = {"job_id": job_id, "status": "running", "account_label": "B97",
                            "started_at": "t", "campaign_names": ["C"]}
    yield pj, job_id
    with pj._jobs_lock:
        pj._jobs.pop(job_id, None)


def test_requeued_job_starts_again_with_no_attempt_limit(monkeypatch, job):
    pj, job_id = job
    reg = _FakeReg(clear_after=3)
    monkeypatch.setattr(warm, "registry", lambda: reg)
    states = []
    real = pj._show_resource_wait

    def spy(jid, label, view):
        real(jid, label, view)
        states.append((view and view.get("hold"), pj._jobs[job_id]["status"],
                       (pj._jobs[job_id].get("queue") or {}).get("state")))

    monkeypatch.setattr(pj, "_show_resource_wait", spy)
    pub = _FlakyPublisher(fails=7)                      # far beyond any "one retry" limit
    pj._open_with_load_requeue(job_id, pub, "B97")
    assert pub.attempts == 8 and pub.closed == 7
    waiting = [s for s in states if s[0] == "machine_busy"]
    assert waiting and all(s[1] == "queued" and s[2] == "waiting_load" for s in waiting)
    assert states[-1][0] is None and pj._jobs[job_id]["status"] == "running"
    assert "queue" not in pj._jobs[job_id]


def test_one_start_at_a_time_it_waits_for_the_load_to_clear_before_each_start(monkeypatch, job):
    pj, job_id = job
    reg = _FakeReg(clear_after=4)
    monkeypatch.setattr(warm, "registry", lambda: reg)
    order = []
    pub = _FlakyPublisher(fails=1)
    orig_open = pub.open
    pub.open = lambda label: (order.append(("open", reg.calls)), orig_open(label))
    pj._open_with_load_requeue(job_id, pub, "B97")
    assert order == [("open", 0), ("open", 4)], "the next start waits until the reading is clear"


def test_cancel_of_a_requeued_job_stops_the_wait(monkeypatch, job):
    from src.fb_ads_rpa import job_cancel

    pj, job_id = job
    reg = _FakeReg(clear_after=10 ** 9)
    monkeypatch.setattr(warm, "registry", lambda: reg)
    ticks = {"n": 0}

    def sleeper(s):
        ticks["n"] += 1
        if ticks["n"] == 3:
            pj._jobs[job_id]["cancel_requested"] = True

    monkeypatch.setattr(pj, "_load_sleep", sleeper)
    pub = _FlakyPublisher(fails=10 ** 9)
    with pytest.raises(job_cancel.JobCancelled):
        pj._open_with_load_requeue(job_id, pub, "B97")
    assert pub.attempts == 1 and pj._jobs[job_id].get("queue") is None


def test_failure_after_a_meta_write_is_never_requeued(monkeypatch, job):
    from src.web import publish_api

    pj, job_id = job
    monkeypatch.setattr(warm, "registry", lambda: pytest.fail("no wait after a write"))
    pub = _FlakyPublisher(fails=1, writes=1)
    with pytest.raises(publish_api.LoadRequeue):
        pj._open_with_load_requeue(job_id, pub, "B97")
    assert pub.attempts == 1


def test_publisher_with_a_write_does_not_turn_into_a_requeue(monkeypatch, tmp_path):
    rig, pub, publish_api, _r = _publisher(monkeypatch, tmp_path, cpu=95.0, frozen=True)
    pub._client = SimpleNamespace(writes=2)
    with pytest.raises(publish_api.ApiPublishError) as ei:
        pub.open("Acc")
    assert not isinstance(ei.value, publish_api.LoadRequeue)


def test_other_failures_still_stop_the_job(monkeypatch, job):
    pj, job_id = job
    from src.web import publish_api

    class Broken(_FlakyPublisher):
        def open(self, label):
            raise publish_api.ApiPublishError("no profile_id in the account map")

    with pytest.raises(publish_api.ApiPublishError):
        pj._open_with_load_requeue(job_id, Broken(0), "B97")


def test_long_requeue_does_not_fire_a_false_step_stuck(monkeypatch, job):
    from src.fb_ads_rpa import run_telemetry as rt
    from src.fb_ads_rpa import sentry_signal

    pj, job_id = job
    now = {"t": 1000.0}
    monkeypatch.setattr(rt, "_clock", lambda: now["t"])
    monkeypatch.setenv("ACL_STEP_STUCK_S", "300")
    sent, rows = [], []
    monkeypatch.setattr(sentry_signal, "report_step_stuck", lambda *a, **kw: sent.append(a) or "evt")
    monkeypatch.setattr(rt, "record", lambda step, ok=None, **kw: rows.append((step, kw)))
    reg = _FakeReg(clear_after=40)
    monkeypatch.setattr(warm, "registry", lambda: reg)
    state = {"fired": False, "since": 900.0}
    checks = []

    def sleeper(s):
        now["t"] += 20                                  # 40 looks x 20 s = a 13 min requeue
        checks.append(pj._step_stuck_tick(job_id, state, "B97"))

    monkeypatch.setattr(pj, "_load_sleep", sleeper)
    rt.set_current_job(job_id)
    rt._touch_job(job_id, "open_profile")
    pj._open_with_load_requeue(job_id, _FlakyPublisher(fails=1), "B97")
    assert not any(checks) and sent == []
    notes = [kw.get("note") for _s, kw in rows]
    assert "load_requeue" in notes and "resource_wait" in notes, "start and end of the wait are recorded"
