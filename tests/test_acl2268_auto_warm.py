"""ACL-2268: автопрогрев профиля из билдера — один путь открытия, тихий режим, лимит попыток."""
import json
import threading
from types import SimpleNamespace

import pytest

from src.fb_api import connection, warm
from src.web import auto_warm_store, profile_slots, warm_api
from tests.test_acl1812_warm_sessions import ACT, PID, FakeQueue, Rig

LABEL = "Acc"


class AutoQueue(FakeQueue):
    """Очередь слотов с `snapshot()` и `once`, как у настоящей."""

    def __init__(self, rig):
        super().__init__(rig)
        self.waiting = []
        self.running = []
        self.allow = True
        self.once_calls = []

    def acquire(self, job_id, profile, *, cancelled=None, on_wait=None, share_profile=False, once=False):
        self.once_calls.append(once)
        return super().acquire(job_id, profile, cancelled=cancelled, on_wait=on_wait)

    def snapshot(self):
        return {"settings": {"ceiling": 2}, "waiting": list(self.waiting), "running": list(self.running),
                "next": {"allow": self.allow}}


def _entry(pid=PID, label=LABEL):
    return SimpleNamespace(profile_id=pid, folder_id="F", multilogin_instance="A", ad_account_id=ACT)


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("ACL_PROFILE_RESTART_COOLDOWN_S", "0")
    monkeypatch.setattr(connection, "settings", lambda: {"managed_sessions": True, "auto_reopen_manual": False,
                                                          "auto_warm": True})
    rig = Rig(tmp_path)
    rig.scan_result[PID] = ("none", None)
    queue = AutoQueue(rig)
    rig.reg._slot_queue = lambda: queue
    clock = [1_000_000.0]
    monkeypatch.setattr(auto_warm_store, "_clock", lambda: clock[0])
    monkeypatch.setattr(auto_warm_store, "_path", lambda: tmp_path / "auto_warm_attempts.json")
    monkeypatch.setattr(warm_api, "_registry", lambda: rig.reg)
    monkeypatch.setattr(warm_api, "_entry", lambda label: _entry() if label.startswith("Acc") else None)
    monkeypatch.setattr(warm_api, "_guard", lambda: None)
    monkeypatch.setattr(warm_api, "_publish_busy", lambda label: False)
    monkeypatch.setattr(warm_api, "_sleep", lambda s: rig.sleeps.append(s))
    monkeypatch.setattr(warm_api, "_start_worker_locked", lambda: None)    # воркер гоняем руками
    timers = []
    monkeypatch.setattr(warm_api, "_timer", lambda delay, fn: timers.append((delay, fn)))
    from src.web import publish_api
    monkeypatch.setattr(publish_api, "_cooldown", lambda label: (0.0, ""))
    warm_api._AUTO.clear()
    warm_api._AUTO_QUEUE.clear()
    rows, events = [], []
    from src.fb_ads_rpa import run_telemetry
    monkeypatch.setattr(run_telemetry, "record", lambda step, ok=None, **kw: rows.append((step, ok, kw)))
    from src.acl_event_log import writer
    monkeypatch.setattr(writer, "emit", lambda t, p=None, **kw: events.append((t, p, kw)))
    spied = []
    real_prepare = connection.prepare_existing
    monkeypatch.setattr(connection, "prepare_existing",
                        lambda reg, entry, label: spied.append(label) or real_prepare(reg, entry, label))
    return SimpleNamespace(rig=rig, queue=queue, clock=clock, timers=timers, rows=rows, events=events,
                           spied=spied)


def go(trigger="add", campaigns=1, label=LABEL):
    body, code = warm_api.ensure_open([label], trigger, campaigns)
    warm_api._auto_worker()
    return body, code


def outcome():
    return warm_api._AUTO[LABEL]["code"]


def test_closed_profile_opens_once_through_prepare_existing(env):
    body, code = go()
    assert code == 202 and body["accounts"][LABEL] == "queued"
    assert env.spied == [LABEL] and len(env.rig.started) == 1
    assert outcome() == "started" and env.rig.reg.is_warm(PID)


def test_repeat_inside_thirty_minutes_starts_nothing_then_one_more_after(env):
    go()
    env.rig.scan_result[PID] = ("none", None)
    env.rig.reg._forget(PID, stopped=False)
    body, _ = go()
    assert body["accounts"][LABEL] == "skipped" and len(env.rig.started) == 1
    env.clock[0] += auto_warm_store.AUTO_RETRY_S + 1
    warm_api._AUTO.clear()
    go()
    assert len(env.rig.started) == 2


def test_attempt_survives_restart_and_is_per_physical_profile(env, monkeypatch):
    go()
    warm_api._AUTO.clear()                                  # «перезапуск»: память пуста, файл остался
    monkeypatch.setattr(warm_api, "_entry", lambda label: _entry())
    body, _ = go(label="Acc2")                              # другая подпись того же профиля
    assert body["accounts"]["Acc2"] == "skipped"


def test_unreadable_attempts_file_means_no_attempt(env, tmp_path):
    (tmp_path / "auto_warm_attempts.json").write_text("not json", encoding="utf-8")
    body, code = go()
    assert body["accounts"][LABEL] == "skipped" and not env.rig.started


def test_hand_opened_window_is_never_touched_even_with_reopen_setting(env, monkeypatch):
    monkeypatch.setattr(connection, "settings", lambda: {"managed_sessions": True, "auto_reopen_manual": True,
                                                          "auto_warm": True})
    env.rig.scan_result[PID] = ("no_port", None)
    go()
    assert outcome() == "open_by_hand" and not env.rig.stopped and not env.rig.started


def test_quiet_mode_forbids_manual_reopen_even_when_scan_changes_after_the_check(env, monkeypatch):
    monkeypatch.setattr(connection, "settings", lambda: {"managed_sessions": True, "auto_reopen_manual": True,
                                                          "auto_warm": True})
    env.rig.scan_result[PID] = ("no_port", None)
    with warm.quiet_start():
        assert connection.may_reopen_manual() is False
        res = env.rig.reg.open_one(LABEL, _entry())
    assert res["code"] == "open_by_hand" and not env.rig.stopped


def test_failed_start_is_not_retried(env):
    env.rig.start_error = RuntimeError("boom")
    go()
    assert outcome() == "start_failed" and not env.timers and not warm_api._AUTO_QUEUE


def test_busy_lease_gets_one_relook_after_sixty_seconds(env):
    from src.fb_ads_rpa.operations import profile_lock
    held, release = threading.Event(), threading.Event()

    def hold():
        with profile_lock(PID):
            held.set()
            release.wait(10)

    t = threading.Thread(target=hold)
    t.start()
    held.wait(5)
    try:
        go()
        assert [d for d, _ in env.timers] == [warm_api.AUTO_REQUEUE_S]
        assert warm_api._AUTO[LABEL]["status"] == "queued" and not env.rig.started
        env.timers[0][1]()
        warm_api._auto_worker()
        assert outcome() == "busy" and len(env.timers) == 1       # третьего взгляда нет
    finally:
        release.set()
        t.join()


def test_busy_lease_freed_on_second_look_starts_once(env):
    from src.fb_ads_rpa.operations import profile_lock
    held, release = threading.Event(), threading.Event()

    def hold():
        with profile_lock(PID):
            held.set()
            release.wait(10)

    t = threading.Thread(target=hold)
    t.start()
    held.wait(5)
    go()
    release.set()
    t.join()
    env.timers[0][1]()
    warm_api._auto_worker()
    assert outcome() == "started" and len(env.rig.started) == 1


@pytest.mark.parametrize("setup,code", [
    (lambda e, m: setattr(e.queue, "waiting", [{"job_id": "j", "profile": "x"}]), "jobs_waiting"),
    (lambda e, m: setattr(e.queue, "running", [{"job_id": "a", "profile": "p1"}, {"job_id": "b", "profile": "p2"}]),
     "ceiling"),
    (lambda e, m: setattr(e.queue, "allow", False), "memory"),
    (lambda e, m: setattr(e.rig, "scan_result", {PID: ("closing", None)}), "closing"),
    (lambda e, m: m.setattr(warm_api, "_publish_busy", lambda label: True), "busy"),
])
def test_each_refusal_starts_nothing_with_the_right_code(env, monkeypatch, setup, code):
    setup(env, monkeypatch)
    go()
    assert outcome() == code and not env.rig.started


def test_meta_stop_refuses(env, monkeypatch):
    from src.web import publish_api
    monkeypatch.setattr(publish_api, "_cooldown", lambda label: (300.0, "checkpoint"))
    go()
    assert outcome() == "cooldown" and not env.rig.started


def test_another_profile_warming_refuses(env):
    env.rig.reg._starting["other"] = env.rig.reg._mono()
    go()
    assert outcome() == "machine_busy" and not env.rig.started


def test_ceiling_counts_warm_sessions_and_queue_running_without_double_count(env):
    env.rig.warm_up(pid="p1", account="A1")
    env.queue.running = [{"job_id": "a", "profile": "p1"}]       # тот же профиль в обоих списках
    reg = env.rig.reg
    assert warm_api._admission(reg, PID)[0] == ""                # 1 профиль < потолка 2
    env.queue.running.append({"job_id": "b", "profile": "p2"})
    assert warm_api._admission(reg, PID)[0] == "ceiling"


def test_slot_queue_once_does_not_wait_or_evict():
    GB = 1024
    settings = profile_slots.SlotSettings(ceiling=1, kept_idle_close_s=0.0)
    evicted = []
    q = profile_slots.ProfileSlots(
        memory=lambda: profile_slots.MemoryReading(total_mb=128 * GB, available_mb=120 * GB),
        settings=lambda: settings, tick_s=0.02, evictor=lambda *a, **k: evicted.append(a) or True)
    assert q.acquire("j0", "p0")
    q.keep("j0", "Acc", profile_slots.KEPT_CONVENIENCE)       # простаивающее чужое окно
    assert q.acquire("auto", "pz", once=True) is None          # место занято: молча отказ
    assert not evicted and "auto" not in q._waiting


def test_quiet_start_returns_at_once_on_memory_shortage_without_sleeping(env):
    env.rig.reg._memory = lambda: profile_slots.MemoryReading(64000, 1000)
    with warm.quiet_start():
        res = env.rig.reg.open_one(LABEL, _entry())
    assert res["code"] == "memory_low" and not env.rig.sleeps and not env.rig.started


def test_quiet_start_asks_the_queue_once(env):
    go()
    assert env.queue.once_calls == [True]


def test_race_with_publish_registers_warm_and_auto_records_already_open(env):
    env.rig.warm_up()
    env.rig.checkout().release()
    go()
    assert outcome() == "already_open" and not env.rig.started


def test_validation(env, monkeypatch):
    assert warm_api.ensure_open([LABEL], "", 1)[1] == 400
    assert warm_api.ensure_open([LABEL], "load", 1)[1] == 400
    assert warm_api.ensure_open([LABEL], "add", 0)[1] == 400
    assert warm_api.ensure_open([LABEL, "Acc2"], "add", 1)[1] == 400
    assert warm_api.ensure_open([], "add", 1)[1] == 400


def test_feature_off_queues_nothing(env, monkeypatch):
    monkeypatch.setattr(connection, "settings", lambda: {"managed_sessions": True, "auto_reopen_manual": False,
                                                          "auto_warm": False})
    body, code = warm_api.ensure_open([LABEL], "add", 1)
    assert code == 200 and body == {"accounts": {LABEL: "off"}} and not warm_api._AUTO_QUEUE


def test_auto_warm_is_off_by_default():
    assert connection.DEFAULTS["auto_warm"] is False


def test_rpa_backend_and_non_loopback_refused(monkeypatch):
    monkeypatch.setattr(warm_api, "_guard", lambda: ({"error": "no"}, 409))
    assert warm_api.ensure_open([LABEL], "add", 1)[1] == 409


def test_no_stop_log_is_written_and_cooldown_default_untouched(env):
    go()
    assert not env.rig.stops._path.exists() or not json.loads(env.rig.stops._path.read_text() or "{}")
    src = open(warm_api.__file__, encoding="utf-8").read()
    auto_part = src[src.index("ACL-2268: автопрогрев"):]
    assert "RESTART_COOLDOWN" not in auto_part


def test_telemetry_row_per_attempt_and_scrubbed(env):
    go()
    assert len(env.rows) == 1 and env.rows[0][2]["note"] == "auto_warm"
    diag = env.rows[0][2]["diag"]
    assert diag["outcome"] == "started" and diag["trigger"] == "add"
    assert PID not in json.dumps(diag) and "http" not in json.dumps(diag)
    assert env.events and env.events[0][0] == "profile_auto_warm"


def test_state_shows_quiet_reason_in_the_chip_note(env, monkeypatch):
    monkeypatch.setattr(warm_api, "availability", lambda: {"available": True, "actions": True})
    env.queue.allow = False
    go()
    got = warm_api.state([LABEL])["accounts"][LABEL]
    assert got["auto"] == "refused" and got["auto_code"] == "memory"
    assert "Check plan or Publish" in got["auto_note"] and "red" not in got["auto_note"]
