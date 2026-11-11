"""ACL-2347: телеметрия для тестов с байерами: ожидание ресурсов и загрузка видео.

Живой Meta и Multilogin не нужны: Meta и вкладка поддельные, часы подменены.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.fb_ads_rpa import run_telemetry as rt  # noqa: E402
from src.fb_ads_rpa import sentry_signal  # noqa: E402
from src.fb_api import creator as cr  # noqa: E402
from src.web import publish_jobs as pj  # noqa: E402
from tests.test_acl1761_api_create import FakeMeta, _draft, _run  # noqa: E402
from tests.test_acl1812_warm_sessions import Rig  # noqa: E402


@pytest.fixture
def job(monkeypatch):
    rows: list = []
    monkeypatch.setattr(rt, "record", lambda step, ok=None, **kw: rows.append((step, ok, kw)))
    monkeypatch.setattr(pj._slots, "kept_labels", lambda: [])
    job_id = "acl2347-metrics"
    with pj._jobs_lock:
        pj._jobs[job_id] = {"job_id": job_id, "status": "running", "account_label": "Doc120",
                            "started_at": "t", "campaign_names": ["C"], "done_count": 0}
    yield job_id, rows
    with pj._jobs_lock:
        pj._jobs.pop(job_id, None)


def _noted(rows, note):
    return [kw for step, _ok, kw in rows if kw.get("note") == note]


def _video_rows(rows):
    return [(ok, kw) for step, ok, kw in rows if (kw.get("diag") or {}).get("metric") == "video_upload"]


def test_gate_view_keeps_the_real_reason_behind_the_hold_label(tmp_path):
    rig = Rig(tmp_path)
    view = rig.reg.gate.wait_view("limit")
    assert view["hold"] == "heavy_limit", "ACL-2293: the limit is named, not 'another account'"
    assert view["why"] == "limit"


def test_wait_writes_its_start_at_once_and_each_reason_with_seconds(job, monkeypatch):
    job_id, rows = job
    now = {"t": 100.0}
    monkeypatch.setattr(pj.time, "monotonic", lambda: now["t"])
    pj._show_resource_wait(job_id, "Doc120", {"hold": "memory_low", "used_pct": 80.0})
    assert [kw["diag"]["why"] for kw in _noted(rows, "resource_hold_start")] == ["memory_low"]
    now["t"] = 110.0
    pj._show_resource_wait(job_id, "Doc120", {"hold": "memory_low", "used_pct": 80.0})
    now["t"] = 130.0
    pj._show_resource_wait(job_id, "Doc120", {"hold": "account_busy", "why": "limit"})
    now["t"] = 145.0
    pj._show_resource_wait(job_id, "Doc120", None)

    assert len(_noted(rows, "resource_hold_start")) == 1, "one start for one wait"
    end = _noted(rows, "resource_wait")
    assert len(end) == 1 and end[0]["duration_s"] == 45.0
    assert end[0]["diag"]["hold"] == "account_busy"
    assert end[0]["diag"]["holds"] == [{"why": "memory_low", "s": 30.0}, {"why": "limit", "s": 15.0}]
    assert "resource_holds" not in pj._jobs[job_id], "the next wait starts clean"


def test_next_wait_gets_its_own_start(job):
    job_id, rows = job
    for _ in range(2):
        pj._show_resource_wait(job_id, "Doc120", {"hold": "memory_low"})
        pj._show_resource_wait(job_id, "Doc120", None)
    assert len(_noted(rows, "resource_hold_start")) == 2
    assert len(_noted(rows, "resource_wait")) == 2


def test_upload_row_has_size_hash_and_mode(tmp_path, monkeypatch):
    rows: list = []
    monkeypatch.setattr(rt, "record", lambda step, ok=None, **kw: rows.append((step, ok, kw)))
    out, _client = _run(FakeMeta(), _draft(tmp_path, video=True, ads=3))
    assert out.error == "", out.error
    videos = _video_rows(rows)
    assert len(videos) == 1
    ok, kw = videos[0]
    data = (tmp_path / "1002_clip.mp4").read_bytes()
    assert ok is True and kw["campaign"]
    assert kw["diag"]["mode"] == "whole"
    assert kw["diag"]["bytes"] == len(data)
    assert kw["diag"]["md5"] == cr._md5(data)[:12]
    assert isinstance(kw["diag"]["upload_s"], float)
    assert not any(step == "api_video_upload" for step, _ok, _kw in rows), "panel knows api_create_step only"


def test_second_run_says_the_video_was_found(tmp_path, monkeypatch):
    rows: list = []
    monkeypatch.setattr(rt, "record", lambda step, ok=None, **kw: rows.append((step, ok, kw)))
    meta = FakeMeta()
    draft = _draft(tmp_path, video=True, ads=3)
    plan = cr.plan_from_draft(draft)
    data = (tmp_path / "1002_clip.mp4").read_bytes()
    meta._new("video", title=cr.video_title(plan.tag, data), _polls=0)
    out, _client = _run(meta, draft)
    assert out.error == "", out.error
    assert [kw["diag"]["mode"] for _ok, kw in _video_rows(rows)] == ["found"]


def test_failed_upload_is_recorded_and_the_mark_is_cleared(tmp_path, monkeypatch):
    rows: list = []
    monkeypatch.setattr(rt, "record", lambda step, ok=None, **kw: rows.append((step, ok, kw)))
    rt.set_current_job("acl2347-upload")
    meta = FakeMeta()
    real = meta.write
    seen: list = []

    def refuse(path, params, **kw):
        if path.endswith("/advideos"):
            seen.append(rt.waiting_on("acl2347-upload"))
            raise cr.CreationStopped("boom", kind="video_error")
        return real(path, params, **kw)

    monkeypatch.setattr(meta, "write", refuse)
    try:
        _run(meta, _draft(tmp_path, video=True, ads=3))
        assert seen and seen[0][0] == "video_upload", "the watcher sees the upload while it runs"
        assert rt.waiting_on("acl2347-upload") is None
        ok, kw = _video_rows(rows)[0]
        assert ok is False and kw["diag"]["error"] == "CreationStopped"
        assert kw["diag"]["kind"] == "video_error"
    finally:
        rt.forget_job("acl2347-upload")
        rt.set_current_job("")


def test_silence_during_an_upload_is_marked_as_waiting(job, monkeypatch):
    job_id, rows = job
    now = {"t": 1000.0}
    sent: list = []
    monkeypatch.setattr(rt, "_clock", lambda: now["t"])
    monkeypatch.setenv("ACL_STEP_STUCK_S", "300")
    monkeypatch.setattr(sentry_signal, "report_step_stuck", lambda *a, **kw: sent.append(a) or "evt")
    try:
        rt.set_current_job(job_id)
        rt._touch_job(job_id, "api_create_step")
        rt.set_waiting("video_upload")
        now["t"] = 1301.0
        assert pj._step_stuck_tick(job_id, {"fired": False, "since": 900.0}, "Doc120") is True
        diag = next(kw["diag"] for step, _ok, kw in rows if step == "step_stuck")
        assert diag["waiting_on"] == "video_upload" and diag["waiting_s"] == 301.0
        assert sent[0][0] == "video_upload", "Sentry must not group it with hung tabs"
    finally:
        rt.forget_job(job_id)
        rt.set_current_job("")


def test_an_ordinary_silence_has_no_waiting_mark(job, monkeypatch):
    job_id, rows = job
    now = {"t": 1000.0}
    monkeypatch.setattr(rt, "_clock", lambda: now["t"])
    monkeypatch.setenv("ACL_STEP_STUCK_S", "300")
    monkeypatch.setattr(sentry_signal, "report_step_stuck", lambda *a, **kw: "evt")
    try:
        rt._touch_job(job_id, "api_create_step")
        now["t"] = 1301.0
        assert pj._step_stuck_tick(job_id, {"fired": False, "since": 900.0}, "Doc120") is True
        diag = next(kw["diag"] for step, _ok, kw in rows if step == "step_stuck")
        assert "waiting_on" not in diag
    finally:
        rt.forget_job(job_id)
