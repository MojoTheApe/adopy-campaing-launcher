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
