"""ACL-2345: a publish stopped by a dead tab resumes once by itself; memory admission starts at 75 %. Fakes only."""
from types import SimpleNamespace

import pytest

from src.fb_api.graph import Checkpoint, LoggedOut, PageFailed, PageUnresponsive
from src.web import profile_slots as ps
from src.web import publish_api
from src.web import publish_jobs as pj

JOB = "job-2345"


class _Pub:
    """Publisher fake: scripted outcomes; the refusal rules are the real ones."""

    RESUMABLE_STOPS = publish_api.ApiPublisher.RESUMABLE_STOPS

    def __init__(self, script):
        self.script = list(script)
        self.stop = None
        self.blocked = ""
        self._writes = 0
        self._client = SimpleNamespace(unreconciled=None)
        self.opens = 0
        self.closes = 0
        self.dropped = []
        self.auto_resumed = False
        self.owner_stop = False
        self.published = []

    writes = property(lambda self: self._writes)

    def cancelled(self):
        return self.owner_stop

    def open(self, label):
        self.opens += 1

    def close(self, **kw):
        self.closes += 1

    def drop_frozen_tab(self, clear_pause=False):
        self.stop = None
        self.dropped.append(clear_pause)
        return {}

    def auto_resume_refusal(self, before, res=None):
        return publish_api.ApiPublisher.auto_resume_refusal(self, before, res)

    def publish_draft(self, draft):
        self.published.append(draft.name)
        out = publish_api.Outcome(campaign_name=draft.name)
        if self.stop is not None:       # like the real publisher: a stop that stands leaves the rest not started
            return publish_api.result_dict(out, not_started=True, error="Not started: Graph API stopped earlier.")
        step = self.script.pop(0)
        if step == "ok":
            return publish_api.result_dict(out)
        kind, _, extra = step.partition("+")
        self.stop = {"unresponsive": PageUnresponsive, "failed": PageFailed, "login": LoggedOut,
                     "checkpoint": Checkpoint}[kind]("tab")
        if extra == "write":
            self._writes += 1
        if extra == "unknown":
            self._client.unreconciled = "ads"
        if extra == "mismatch":
            out.readback = {"mismatches": [{"object": "ad", "field": "name", "expected": "a", "actual": "b"}]}
            out.campaign_id = "1"
        return publish_api.result_dict(out, error=f"stopped ({self.stop.kind})")


@pytest.fixture
def rig(monkeypatch):
    with pj._jobs_lock:
        pj._jobs[JOB] = {"job_id": JOB, "status": "running", "account_label": "B97", "started_at": "t",
                         "campaign_names": ["A", "B"], "results": [], "errors": [], "done_count": 0, "total": 2}
    state = SimpleNamespace(closed=[], admits=[(True, 70.0)], rows=[], cancelled=False, shown=[],
                            on_sleep=lambda: None)
    monkeypatch.setattr(pj, "_persist_job", lambda *a, **k: None)
    monkeypatch.setattr(pj, "profile_key_for", lambda label: "p-" + label)
    monkeypatch.setattr(pj, "_stop_idle_profile",
                        lambda label, profile, **k: state.closed.append(profile) or {"ok": True})
    monkeypatch.setattr(pj, "_memory_admits",
                        lambda: state.admits[0] if len(state.admits) == 1 else state.admits.pop(0))
    monkeypatch.setattr(pj, "_load_sleep", lambda s: state.on_sleep())
    monkeypatch.setattr(pj, "_is_cancelled", lambda job_id: state.cancelled)
    monkeypatch.setattr(pj, "_show_resource_wait", lambda j, a, view: state.shown.append(view))
    monkeypatch.setattr(pj, "_wait_out_machine_load", lambda *a, **k: True)
    from src.fb_ads_rpa import run_telemetry
    monkeypatch.setattr(run_telemetry, "record", lambda *a, **k: state.rows.append((a, k)))
    yield state
    with pj._jobs_lock:
        pj._jobs.pop(JOB, None)


def _drafts(*names):
    return [SimpleNamespace(name=n, errors=[]) for n in names]


def _row(state):
    rows = [k["diag"] for a, k in state.rows if k.get("note") == "auto_resume"]
    assert rows, "an auto_resume row is written"
    return rows[-1]


def _names():
    return [r["campaign_name"] for r in pj._jobs[JOB]["results"]]


def test_page_unresponsive_with_a_reconciled_state_resumes_once_and_the_job_finishes(rig):
    pub = _Pub(["unresponsive+write", "ok", "ok"])
    pending = _drafts("A", "B")
    pj._publish_pending(JOB, pub, "B97", pending)
    assert pub.published == ["A", "A", "B"], "the same campaign runs again, nothing else is resent"
    assert _names() == ["A", "B"] and pending == []
    assert rig.closed == ["p-B97"] and pub.opens == 1 and pub.auto_resumed is True
    assert pub.dropped == [True]
    row = _row(rig)
    assert row["resumed"] is True and row["stop"] == "page_unresponsive" and row["refused_reason"] is None
    assert {"waited_s", "memory_pct", "cpu_pct"} <= set(row)


def test_the_job_waits_for_memory_before_the_profile_is_reopened(rig):
    rig.admits = [(False, 82.0), (False, 79.0), (True, 70.0)]
    pub = _Pub(["unresponsive", "ok", "ok"])
    pj._publish_pending(JOB, pub, "B97", _drafts("A", "B"))
    holds = [v["hold"] for v in rig.shown if v]
    assert holds == ["auto_resume", "auto_resume"] and rig.shown[-1] is None
    assert pub.opens == 1 and _names() == ["A", "B"]


def test_a_second_stop_does_not_resume_again(rig):
    pub = _Pub(["unresponsive", "unresponsive"])
    pj._publish_pending(JOB, pub, "B97", _drafts("A", "B"))
    assert pub.opens == 1 and rig.closed == ["p-B97"], "one resume only"
    res = pj._jobs[JOB]["results"]
    assert res[0]["error"] and "page_unresponsive" in res[0]["error"]
    assert len([1 for a, k in rig.rows if k.get("note") == "auto_resume"]) == 1, "only the one resume is logged"


@pytest.mark.parametrize("step,reason", [
    ("unresponsive+unknown", "write_unknown"),
    ("login", "stop_logged_out"),
    ("checkpoint", "stop_checkpoint"),
    ("unresponsive+mismatch", "mismatches"),
    ("failed+write", "write_may_be_in_flight"),
])
def test_unsafe_stops_never_resume(rig, step, reason):
    pub = _Pub([step])
    pj._publish_pending(JOB, pub, "B97", _drafts("A"))
    assert pub.published == ["A"] and pub.opens == 0 and rig.closed == []
    assert _row(rig)["refused_reason"] == reason and _row(rig)["resumed"] is False


def test_an_unknown_write_flag_on_the_publisher_never_resumes(rig):
    pub = _Pub(["unresponsive"])
    pub.stop = PageUnresponsive("tab")
    pub.blocked = "A"
    assert pub.auto_resume_refusal(0) == "write_unknown"


def test_owner_stop_never_resumes(rig):
    pub = _Pub(["unresponsive"])
    pub.owner_stop = True
    pj._publish_pending(JOB, pub, "B97", _drafts("A"))
    assert pub.opens == 0 and rig.closed == [] and _row(rig)["refused_reason"] == "owner_stop"


def test_owner_stop_during_the_wait_ends_the_job_without_a_reopen(rig):
    rig.admits = [(False, 82.0)]
    pub = _Pub(["unresponsive"])

    def cancel():
        rig.cancelled = True

    rig.on_sleep = cancel
    pj._publish_pending(JOB, pub, "B97", _drafts("A"))
    assert pub.opens == 0 and _row(rig)["refused_reason"] == "owner_stop"


def test_page_failed_with_nothing_written_for_the_campaign_resumes(rig):
    pub = _Pub(["failed", "ok"])
    pj._publish_pending(JOB, pub, "B97", _drafts("A"))
    assert pub.opens == 1 and _names() == ["A"] and _row(rig)["stop"] == "page_failed"


def test_when_the_wait_limit_passes_the_job_ends_with_the_normal_stop_text(rig, monkeypatch):
    rig.admits = [(False, 85.0)]
    monkeypatch.setattr(pj, "_auto_resume_wait_s", lambda: 0.0)
    pub = _Pub(["unresponsive"])
    pending = _drafts("A", "B")
    pj._publish_pending(JOB, pub, "B97", pending)
    assert pub.opens == 0 and pub.dropped == [] and pub.stop is not None, "the stop is left as it was"
    assert _row(rig)["refused_reason"] == "wait_timeout" and _row(rig)["resumed"] is False
    assert "page_unresponsive" in pj._jobs[JOB]["results"][0]["error"]


def test_the_waiting_text_is_the_paused_message():
    html = open("src/web/templates/report.html", encoding="utf-8").read()
    assert "Paused: the computer is short of memory, will continue by itself" in html
    assert pj._queue_view({"reason": "auto_resume"})["state"] == "waiting_memory"


# ---- Part 2: the stricter default ----

def test_the_memory_limit_defaults_to_75_and_keeps_the_env_override():
    assert ps.DEFAULT_MAX_USED_PCT == 75.0
    assert ps.load_settings({}).max_used_pct == 75.0
    assert ps.load_settings({"ACL_MEMORY_MAX_USED_PCT": "85"}).max_used_pct == 85.0


@pytest.mark.parametrize("used,allowed", [(70, True), (74, True), (76, False), (80, False)])
def test_with_used_memory_over_75_percent_a_new_profile_waits(used, allowed):
    total = 32 * 1024
    mem = ps.MemoryReading(total, int(total * (100 - used) / 100))
    verdict = ps.decide(1, 0, mem, ps.SlotSettings(ceiling=5))
    assert verdict["allow"] is allowed
    if not allowed:
        assert verdict["reason"] == "below_reserve" and verdict["max_used_pct"] == 75.0
        assert pj._queue_view(verdict)["state"] == "waiting_memory"
