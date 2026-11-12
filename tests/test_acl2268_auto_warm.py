"""ACL-2268: the profile is brought up automatically when a person starts working with an account in the Builder.

Fakes only: no Meta, no Multilogin, no browser. Covers the server side (one opening path through
`prepare_existing`, quiet mode of `open_one`, no eviction, the 30-minute attempt per physical profile kept on disk,
every refusal, the one 60 s re-look, telemetry without ids) and the client harness (warm_auto_harness.cjs).
"""
from __future__ import annotations

import dataclasses
import json
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.fb_api import connection  # noqa: E402
from src.fb_api import warm  # noqa: E402
from src.web import auto_warm_store  # noqa: E402
from src.web import profile_slots as ps  # noqa: E402
from src.web import warm_api  # noqa: E402
from tests.test_acl1812_warm_sessions import PID, FakeQueue, Rig, entry  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


class QuietQueue(FakeQueue):
    def acquire(self, job_id, profile, *, cancelled=None, on_wait=None, share_profile=False, once=False):
        self.rig.slot_calls.append(("acquire", job_id, profile, "once" if once else "wait"))
        return {"shared": False} if self.rig.grant_ok else None


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path)
    r.reg._slot_queue = lambda: QuietQueue(r)
    return r


# ───────────────────────── quiet mode of open_one ─────────────────────────

def test_quiet_never_restarts_a_hand_opened_window_even_with_the_setting_on(rig, monkeypatch):
    monkeypatch.setenv("ACL_AUTO_REOPEN_MANUAL", "1")
    rig.scan_result[PID] = ("no_port", None)
    with warm.quiet_start():
        res = rig.reg.open_one("Acc", entry())
    assert res["code"] == "open_by_hand"
    assert rig.stopped == [] and rig.started == []
    assert connection.may_reopen_manual() is True      # outside quiet mode the setting still works


def test_quiet_does_not_wait_for_a_closing_profile(rig):
    rig.scan_result[PID] = ("closing", None)
    with warm.quiet_start():
        res = rig.reg.open_one("Acc", entry())
    assert res["code"] == "profile_closing"
    assert rig.sleeps == [] and rig.started == []


def test_quiet_slot_is_asked_once_without_waiting(rig):
    with warm.quiet_start():
        rig.reg.open_one("Acc", entry())
    asks = [c for c in rig.slot_calls if c[0] == "acquire"]
    assert asks and all(c[3] == "once" for c in asks)


def test_quiet_slot_refusal_starts_nothing(rig):
    rig.grant_ok = False
    with warm.quiet_start():
        res = rig.reg.open_one("Acc", entry())
    assert res["code"] == "memory_low" and rig.started == []


def test_quiet_memory_shortage_returns_at_once(tmp_path):
    r = Rig(tmp_path, reg_memory=lambda: ps.MemoryReading(64000, 500))
    with warm.quiet_start():
        res = r.reg.open_one("Acc", entry())
    assert res["code"] == "memory_low"
    assert r.sleeps == [] and r.started == []


def test_slots_once_returns_none_without_evicting_or_queueing():
    evicted = []
    settings = dataclasses.replace(ps.load_settings({}), ceiling=1, warmup_s=0)
    slots = ps.ProfileSlots(memory=lambda: ps.MemoryReading(64000, 60000), settings=lambda: settings,
                            evictor=lambda *a: evicted.append(a) or True, tick_s=0.01)
    assert slots.acquire("a", "p1")["decision"]["allow"]
    assert slots.acquire("b", "p2", once=True) is None
    assert evicted == []
    assert slots.snapshot()["waiting"] == []


# ───────────────────────── attempts on disk ─────────────────────────

def test_attempt_is_per_physical_profile_and_survives_a_restart():
    t = [1000.0]
    assert auto_warm_store.claim(PID, 1800, now=lambda: t[0]) is True
    t[0] += 60
    assert auto_warm_store.claim(PID, 1800, now=lambda: t[0]) is False      # same profile, any label
    assert auto_warm_store.claim(PID.upper(), 1800, now=lambda: t[0]) is False
    t[0] += 1800
    assert auto_warm_store.claim(PID, 1800, now=lambda: t[0]) is True        # window over


def test_unreadable_attempts_file_means_no_attempt():
    auto_warm_store._path().write_text("not json", encoding="utf-8")
    assert auto_warm_store.claim(PID, 1800) is False
    auto_warm_store._path().write_text(json.dumps([1]), encoding="utf-8")
    assert auto_warm_store.claim(PID, 1800) is False


# ───────────────────────── server: ensure + worker ─────────────────────────

@pytest.fixture
def api(rig, monkeypatch):
    monkeypatch.setattr(warm_api, "_guard", lambda: None)
    monkeypatch.setattr(warm_api, "_entry", lambda label: entry() if label in ("Acc", "Acc2") else None)
    monkeypatch.setattr(warm_api, "_registry", lambda: rig.reg)
    monkeypatch.setattr(warm_api, "auto_enabled", lambda: True)
    monkeypatch.setattr(warm_api, "_publish_busy", lambda label: False)
    monkeypatch.setattr(warm_api, "_start_worker_locked", lambda: None)
    monkeypatch.setattr(warm_api, "_sleep", lambda s: None)
    timers = []
    monkeypatch.setattr(warm_api, "_timer", lambda delay, fn: timers.append((delay, fn)))
    snap = {"waiting": [], "running": [], "settings": {"ceiling": 2}, "next": {"allow": True}}
    monkeypatch.setattr("src.web.publish_jobs.slots_snapshot", lambda: snap)
    monkeypatch.setattr("src.web.publish_api._cooldown", lambda label: (0.0, ""))
    warm_api._AUTO.clear()
    warm_api._AUTO_QUEUE.clear()
    events = []
    monkeypatch.setattr("src.fb_ads_rpa.run_telemetry.record", lambda *a, **k: events.append(("row", a, k)))
    monkeypatch.setattr("src.acl_event_log.emit", lambda *a, **k: events.append(("event", a, k)))
    return SimpleNamespace(snap=snap, timers=timers, events=events, rig=rig)


def run_queue():
    while warm_api._AUTO_QUEUE:
        warm_api._auto_one(warm_api._AUTO_QUEUE.pop(0))


def ensure(**kw):
    body = {"accounts": ["Acc"], "trigger": "add", "campaigns": 1, **kw}
    return warm_api.ensure_open(body["accounts"], body["trigger"], body["campaigns"])


def test_validation_is_a_contract_not_a_free_for_all(api):
    assert ensure(accounts=["Acc", "Acc2"])[1] == 400
    assert ensure(trigger="")[1] == 400
    assert ensure(trigger="load")[1] == 400
    assert ensure(campaigns=0)[1] == 400
    assert ensure(accounts=[])[1] == 400
    assert warm_api._AUTO_QUEUE == []


def test_refused_on_the_rpa_backend_and_off_the_loopback(api, monkeypatch):
    monkeypatch.setattr(warm_api, "_guard", lambda: ({"error": "no", "code": "rpa_backend"}, 409))
    assert ensure()[1] == 409
    assert warm_api._AUTO_QUEUE == []


def test_feature_off_queues_nothing(api, monkeypatch):
    monkeypatch.setattr(warm_api, "auto_enabled", lambda: False)
    data, code = ensure()
    assert code == 200 and data == {"accounts": {"Acc": "off"}}
    assert warm_api._AUTO_QUEUE == [] and not auto_warm_store._path().exists()


def test_feature_is_off_by_default():
    assert connection.DEFAULTS["auto_warm"] is False
    assert warm_api.auto_enabled() is False


def test_closed_profile_is_opened_once_through_prepare_existing(api, monkeypatch):
    seen = []
    real = connection.prepare_existing
    monkeypatch.setattr(connection, "prepare_existing", lambda *a: seen.append(a[2]) or real(*a))
    monkeypatch.setattr(api.rig.reg, "open_many",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("open_many is a person's button")))
    data, code = ensure()
    assert code == 202 and data["accounts"]["Acc"] == "queued"
    run_queue()
    assert seen == ["Acc"]
    assert len(api.rig.started) == 1
    assert warm_api._AUTO["Acc"]["status"] == "done" and warm_api._AUTO["Acc"]["code"] == "started"
    assert api.rig.reg.is_warm(PID)


def test_repeat_inside_the_window_starts_nothing(api):
    ensure()
    assert ensure()[0]["accounts"]["Acc"] == "queued"          # in flight: no second queue entry
    assert warm_api._AUTO_QUEUE == ["Acc"]
    run_queue()
    data, code = ensure(accounts=["Acc2"])                    # another label, same physical profile
    assert code == 200 and data["accounts"]["Acc2"] == "recent"
    assert len(api.rig.started) == 1


def test_a_failed_start_is_never_retried(api):
    api.rig.start_error = RuntimeError("mlx down")
    ensure()
    run_queue()
    assert warm_api._AUTO["Acc"]["code"] == "start_failed"
    assert api.timers == [] and warm_api._AUTO_QUEUE == []
    assert len(api.rig.started) == 0


@pytest.mark.parametrize("why", ["jobs_waiting", "ceiling", "memory", "cooldown", "busy", "open_by_hand",
                                 "closing", "machine_busy"])
def test_every_refusal_starts_nothing_and_uses_the_attempt(api, monkeypatch, why):
    if why == "jobs_waiting":
        api.snap["waiting"] = [{"job_id": "j", "profile": "x"}]
    elif why == "ceiling":
        api.snap["running"] = [{"profile": "a"}, {"profile": "b"}]
    elif why == "memory":
        api.snap["next"] = {"allow": False}
    elif why == "cooldown":
        monkeypatch.setattr("src.web.publish_api._cooldown", lambda label: (300.0, "checkpoint"))
    elif why == "busy":
        monkeypatch.setattr(warm_api, "_publish_busy", lambda label: True)
    elif why == "open_by_hand":
        api.rig.scan_result[PID] = ("no_port", None)
    elif why == "closing":
        api.rig.scan_result[PID] = ("closing", None)
    elif why == "machine_busy":
        monkeypatch.setattr(api.rig.reg, "load_snapshot", lambda pid="": {"busy": True})
    ensure()
    run_queue()
    assert warm_api._AUTO["Acc"]["status"] == "refused" and warm_api._AUTO["Acc"]["code"] == why
    assert api.rig.started == [] and api.rig.stopped == []
    assert ensure()[0]["accounts"]["Acc"] == "recent"          # the refusal used the attempt


def test_ceiling_counts_warm_sessions_and_queue_without_double_counting(api):
    api.rig.warm_up("other-pid-1", account="O1")
    api.snap["running"] = [{"profile": "other-pid-1"}]         # the same profile in both places: one
    api.snap["settings"] = {"ceiling": 2}
    why, numbers = warm_api._admission(api.rig.reg, PID)
    assert why == "" and numbers["open_profiles"] == 1
    api.snap["running"].append({"profile": "other-pid-2"})
    why, numbers = warm_api._admission(api.rig.reg, PID)
    assert why == "ceiling" and numbers["open_profiles"] == 2


def test_already_open_profile_is_not_started_again(api):
    api.rig.warm_up()
    ensure()
    run_queue()
    assert warm_api._AUTO["Acc"]["code"] == "already_open" and api.rig.started == []


def test_lease_busy_means_one_relook_after_a_minute_then_busy(api, monkeypatch):
    from src.fb_ads_rpa import operations
    from src.fb_ads_rpa.profile_lease import ProfileLeaseError

    class Busy:
        def holding(self, **_kw):
            raise ProfileLeaseError("busy")

    real = operations.profile_lock
    monkeypatch.setattr(operations, "profile_lock", lambda key: Busy())
    ensure()
    run_queue()
    assert warm_api._AUTO["Acc"]["status"] == "queued" and len(api.timers) == 1
    assert api.timers[0][0] == warm_api.AUTO_REQUEUE_S
    api.timers[0][1]()                                          # the re-look fires
    assert warm_api._AUTO_QUEUE == ["Acc"]
    run_queue()                                                 # still busy: refused, no third look
    assert warm_api._AUTO["Acc"]["code"] == "busy" and len(api.timers) == 1
    # a free lease on the re-look: one start
    warm_api._AUTO.clear()
    monkeypatch.setattr(operations, "profile_lock", real)
    warm_api._AUTO["Acc"] = {"status": "queued", "code": "", "retried": True}
    warm_api._AUTO_QUEUE.append("Acc")
    run_queue()
    assert warm_api._AUTO["Acc"]["code"] == "started" and len(api.rig.started) == 1


def test_race_with_publish_finds_the_profile_open_and_starts_once(api):
    release = threading.Event()
    held = threading.Event()

    def publish():
        from src.fb_ads_rpa.operations import profile_lock
        with profile_lock(PID).holding(timeout=5):
            api.rig.warm_up()                                  # the publish brings the profile up
            held.set()
            release.wait(5)

    t = threading.Thread(target=publish)
    t.start()
    assert held.wait(5)
    ensure()
    run_queue()                                                 # lease busy -> one timer
    assert len(api.timers) == 1
    release.set()
    t.join(5)
    api.timers[0][1]()
    run_queue()
    assert warm_api._AUTO["Acc"]["code"] == "already_open" and api.rig.started == []


def test_state_shows_the_auto_status_and_the_reason_for_the_tooltip(api, monkeypatch):
    monkeypatch.setattr(warm_api, "availability", lambda: {"available": True, "actions": True})
    monkeypatch.setattr(warm_api, "_profile_id", lambda label: PID)
    warm_api._AUTO["Acc"] = {"status": "refused", "code": "memory"}
    info = warm_api.state(["Acc"])["accounts"]["Acc"]
    assert info["auto"] == "refused" and info["auto_reason"] == "the computer is short of memory"


def test_telemetry_row_and_event_carry_codes_and_numbers_only(api):
    ensure()
    run_queue()
    kinds = [e[0] for e in api.events]
    assert kinds.count("row") == 1 and kinds.count("event") == 1
    blob = json.dumps([e[1:] for e in api.events], default=str)
    assert PID not in blob and "token" not in blob.lower() and "http" not in blob
    row = next(e for e in api.events if e[0] == "row")
    assert row[2]["note"] == "auto_warm" and row[2]["diag"]["outcome"] == "started"


def test_the_restart_pause_is_not_touched_by_the_auto_path():
    src = (ROOT / "src/web/warm_api.py").read_text(encoding="utf-8")
    auto = src[src.index("# ───────────── ACL-2268"):src.index("def _job_view")]
    assert "RESTART_COOLDOWN" not in auto and "restart_cooldown_s" not in auto and "record_stop" not in auto


def test_route_is_registered_as_post():
    from src.web.app import app
    routes = {(r.path, tuple(sorted(r.methods or ()))) for r in app.routes if hasattr(r, "methods")}
    assert ("/warm/ensure", ("POST",)) in routes


# ───────────────────────── client ─────────────────────────

def test_client_harness_and_static_rules():
    js = (ROOT / "src/web/static/warm_auto.js").read_text(encoding="utf-8")
    assert "setInterval" not in js and "/warm/close" not in js and "/warm/open" not in js
    assert "Open profile" not in js
    html = (ROOT / "src/web/templates/report.html").read_text(encoding="utf-8")
    assert '/static/warm_auto.js' in html and html.count("aclWarmAutoUser(") >= 2
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    out = subprocess.run([node, str(ROOT / "tests/warm_auto_harness.cjs"), str(ROOT / "src/web/static/warm_auto.js")],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stdout + out.stderr
