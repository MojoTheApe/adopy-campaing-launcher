"""Клиент журнала для хаба: частота отправки, побудка при сбое, паспорт машины, нагрузка."""
import pytest

import src.acl_event_log as acl_log
from src.acl_event_log import shipper
from src.web import publish_jobs as pj


def test_cycle_is_two_minutes():
    assert shipper.CYCLE_S == 120


@pytest.fixture
def api_job(monkeypatch):
    job_id = "job-test-hub"
    monkeypatch.setitem(pj._jobs, job_id, {"results": [], "done_count": 0, "fb_backend": "api",
                                            "account_label": "ACC"})
    monkeypatch.setattr(pj, "_status_snapshot_locked", lambda job: {})
    monkeypatch.setattr(pj, "_persist_job", lambda *a, **k: None)
    monkeypatch.setattr(pj, "_abandoned", lambda job_id: False)
    events = []
    monkeypatch.setattr(pj, "_log_event", lambda t, j, p, **k: events.append(t))
    wakes = []
    monkeypatch.setattr(acl_log, "wake_shipper", lambda: wakes.append(1))
    return job_id, events, wakes


def test_publish_failure_wakes_shipper_without_blocking(api_job, monkeypatch):
    job_id, events, wakes = api_job
    pj._append_result(job_id, {"campaign_name": "C1", "success": False, "error": "boom"})
    assert events == ["publish.result"] and wakes == [1]
    pj._append_result(job_id, {"campaign_name": "C2", "success": True})
    assert wakes == [1]

    def broken():
        raise RuntimeError("shipper down")

    monkeypatch.setattr(acl_log, "wake_shipper", broken)
    pj._append_result(job_id, {"campaign_name": "C3", "success": False})   # не бросает
    assert pj._jobs[job_id]["done_count"] == 3


# ── Task 8: паспорт машины и нагрузка ──
import getpass
from pathlib import Path

from src.acl_event_log import machine_profile as mp
from src.acl_event_log.events import stats_payload

PROFILE_KEYS = {"ram_gb", "cpu_model", "cores_logical", "cores_physical", "windows", "screen_w", "screen_h",
                "scale_pct", "on_battery", "uptime_h", "mlx_running", "adspower_running", "chrome_procs",
                "disk_free_gb"}


def test_collect_profile_keys_and_no_identity():
    profile = mp.collect_profile()
    assert set(profile) == PROFILE_KEYS
    text = repr(profile).lower()
    assert getpass.getuser().lower() not in text
    assert str(Path.home()).lower() not in text


def test_collect_profile_survives_failures(monkeypatch):
    def boom(*a, **k):
        raise OSError("no access")

    monkeypatch.setattr(mp.psutil, "virtual_memory", boom)
    profile = mp.collect_profile()
    assert profile["ram_gb"] is None
    assert profile["cores_logical"] is not None


def test_profile_emitted_once(monkeypatch):
    sent = []
    monkeypatch.setattr(mp, "_emitted", False)
    monkeypatch.setattr(mp, "emit", lambda t, p=None, **k: sent.append((t, p)))
    monkeypatch.setattr(mp, "collect_profile", lambda: {"ram_gb": 16})
    acl_log.emit_machine_profile()
    acl_log.emit_machine_profile()
    assert sent == [("machine.profile", {"ram_gb": 16})]


def test_load_snapshot_keys():
    load = mp.load_snapshot()
    assert set(load) == {"cpu_pct", "mem_pct", "mem_avail_mb", "chrome_procs", "chrome_mem_mb"}


def test_stats_payload_carries_load():
    out = stats_payload("C", "h", began=0.0, calls=1, writes=0, load={"cpu_pct": 50})
    assert out["load"] == {"cpu_pct": 50}


def test_stats_payload_without_load_unchanged():
    assert "load" not in stats_payload("C", "h", began=0.0, calls=1, writes=0)


# ── Финальное ревью: исправления клиента ──
import threading


def test_old_url_keeps_hourly_cycle(monkeypatch):
    monkeypatch.setenv("ACL_EVENT_LOG_URL", "https://acl-event-log.adopy-acl.workers.dev")
    assert shipper.cycle_s() == 3600
    monkeypatch.setenv("ACL_EVENT_LOG_URL", "https://acl-hub.example.workers.dev")
    assert shipper.cycle_s() == 120


def test_wake_ignored_while_failing():
    # backoff идёт: побудка не сокращает ожидание
    assert shipper._after_wake(deadline=1000.0, last=0.0, failures=2, now=10.0) == 1000.0


def test_wake_soon_after_ship_is_deferred():
    # отправка была 10 с назад: следующая не раньше чем через 30 с после неё
    assert shipper._after_wake(deadline=120.0, last=0.0, failures=0, now=10.0) == shipper.MIN_WAKE_GAP_S
    # прошло больше 30 с: отправка сразу
    assert shipper._after_wake(deadline=120.0, last=0.0, failures=0, now=45.0) == 45.0


def test_load_snapshot_does_not_scan_processes(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("process scan on the publish path")

    monkeypatch.setattr(mp.psutil, "process_iter", boom)
    monkeypatch.setattr(mp, "_last_load", {"cpu_pct": 12.0, "mem_pct": 50.0, "mem_avail_mb": 4000,
                                           "chrome_procs": 7, "chrome_mem_mb": 900.0})
    assert mp.load_snapshot()["chrome_procs"] == 7


def test_profile_collected_off_the_caller_thread(monkeypatch):
    seen = []
    done = threading.Event()

    def fake_collect():
        seen.append(threading.current_thread().name)
        done.set()
        return {}

    monkeypatch.setattr(mp, "_emitted", False)
    monkeypatch.setattr(mp, "_sampler", None)
    monkeypatch.setattr(mp, "collect_profile", fake_collect)
    monkeypatch.setattr(mp, "emit", lambda *a, **k: None)
    monkeypatch.setattr(mp, "_sample_load", lambda: {})
    monkeypatch.setattr(mp, "SAMPLE_S", 0.01)
    mp.start_sampler()
    assert done.wait(5)
    assert seen and seen[0] != threading.current_thread().name
