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


def test_gate_view_keeps_the_real_reason_behind_account_busy(tmp_path):
    rig = Rig(tmp_path)
    view = rig.reg.gate.wait_view("limit")
    assert view["hold"] == "account_busy", "the dashboard label stays"
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


# ───────────── паспорт машины, нагрузка, счётчики, отпечаток, отчёты ─────────────

from src.acl_event_log import events as ev_mod, machine, shipper, store  # noqa: E402
from src.acl_event_log.writer import use_outbox_for_tests  # noqa: E402


@pytest.fixture
def outbox(tmp_path):
    out = tmp_path / "outbox"
    use_outbox_for_tests(out)
    yield out
    use_outbox_for_tests(None)


def _lines(p):
    import json
    return [json.loads(x) for f in sorted(p.glob("*.ndjson")) for x in f.read_text("utf-8").splitlines()]


def test_machine_profile_reads_this_machine_and_never_raises():
    prof = machine.machine_profile()
    assert prof["ram_gb"] and prof["ram_gb"] > 0
    assert prof["cores_logical"] and prof["os"]
    assert {"screen_px", "scale_pct", "on_battery", "load"} <= set(prof)


def test_machine_profile_survives_a_broken_psutil(monkeypatch):
    monkeypatch.setattr(machine, "_psutil", lambda: (_ for _ in ()).throw(ImportError("no psutil")))
    prof = machine.machine_profile()
    assert prof["ram_gb"] is None and prof["load"]["memory_pct"] is None


def test_shipper_writes_the_passport_once(outbox):
    shipper.emit_machine_profile()
    rows = [x for x in _lines(outbox) if x["type"] == "machine.profile"]
    assert len(rows) == 1 and rows[0]["payload"]["ram_gb"]


def test_cpu_is_averaged_since_the_previous_sample_in_the_thread():
    import threading
    got: list = []
    t = threading.Thread(target=lambda: got.extend([machine.load_now()["cpu_pct"], machine.load_now()["cpu_pct"]]))
    t.start()
    t.join()
    assert got[0] is None, "the first sample in a thread has nothing to compare with"
    assert got[1] is None or 0.0 <= got[1] <= 100.0


def test_job_finished_counts_campaigns_instead_of_a_bare_done(outbox):
    job_id = "acl2347-counts"
    with pj._jobs_lock:
        pj._jobs[job_id] = {"job_id": job_id, "account_label": "Doc120", "fb_backend": "api", "status": "running",
                            "results": [], "errors": [], "done_count": 0,
                            "created_at": "2026-11-03T10:00:00+00:00", "started_at": "2026-11-03T10:00:05+00:00",
                            "finished_at": None}
    try:
        for res in ({"campaign_name": "a", "success": True, "publishable": True},
                    {"campaign_name": "b", "success": False, "error": "page_unresponsive"},
                    {"campaign_name": "c", "success": False, "not_started": True, "error": "Not started"}):
            pj._append_result(job_id, res)
        pj._finish(job_id)
        fin = [x for x in _lines(outbox) if x["type"] == "job.finished"][0]["payload"]
        assert (fin["campaigns"], fin["succeeded"], fin["failed"], fin["not_started"]) == (3, 1, 1, 1)
        assert fin["outcome"] == "partial" and fin["status"] == "done"
        results = [x["payload"] for x in _lines(outbox) if x["type"] == "publish.result"]
        assert all("memory_pct" in r["load"] and "running_jobs" in r["load"] for r in results), \
            "load is written on success too"
        assert results[2]["consequence"] is True and "fail_fp" not in results[2]
        assert results[0]["consequence"] is False and "fail_fp" not in results[0]
        assert results[1]["fail_fp"]
    finally:
        with pj._jobs_lock:
            pj._jobs.pop(job_id, None)


def test_same_failure_with_other_ids_has_one_fingerprint():
    one = ev_mod.result_payload({"success": False, "stop_kind": "page", "steps_completed": ["campaign", "adset"],
                                 "error": "Ad set 120212345678901 for 'Trading_35' failed after 95 s"})
    two = ev_mod.result_payload({"success": False, "stop_kind": "page", "steps_completed": ["campaign", "adset"],
                                 "error": "Ad set 120299999999999 for 'FX_30' failed after 120 s"})
    other = ev_mod.result_payload({"success": False, "stop_kind": "network", "steps_completed": ["campaign"],
                                   "error": "Ad set 120212345678901 for 'Trading_35' failed after 95 s"})
    assert one["fail_fp"] == two["fail_fp"] != other["fail_fp"]
    assert one["fail_after"] == "adset" and "120212" not in one["fail_text"] and "trading" not in one["fail_text"]


def test_result_dict_carries_the_stop_kind():
    from src.web import publish_api
    out = publish_api.Outcome(campaign_name="c")
    out.stop_kind = "page"
    assert publish_api.result_dict(out, error="x")["stop_kind"] == "page"


def _seg(*rows):
    import gzip
    import json
    return gzip.compress("\n".join(json.dumps(r) for r in rows).encode() + b"\n")


def _ev(seq, type, machine_id="m1", ts=None, account="acc", **payload):
    from datetime import datetime, timezone
    return {"event_id": f"s-{machine_id}:{seq}", "stream_id": f"s-{machine_id}", "seq": seq, "type": type,
            "ts": ts or datetime.now(timezone.utc).isoformat(), "machine_id": machine_id, "machine": machine_id.upper(),
            "manager": "A", "flow_version": "v1", "channel": "prod", "job_id": "j1", "account": account,
            "campaign": "c", "payload": payload}


def test_failures_report_groups_by_fingerprint_and_skips_consequences(tmp_path):
    conn = store.open_db(tmp_path / "e.sqlite")
    try:
        store.ingest_segment(conn, "k1", _seg(
            _ev(1, "publish.result", success=False, fail_fp="aaa", fail_text="tab", stop_kind="page"),
            _ev(2, "publish.result", success=False, consequence=True, not_started=True),
            _ev(3, "publish.result", success=True)))
        store.ingest_segment(conn, "k2", _seg(
            _ev(1, "publish.result", machine_id="m2", success=False, fail_fp="aaa", fail_text="tab"),
            _ev(2, "publish.result", machine_id="m2", success=False, fail_fp="bbb", fail_text="net")))
        rows = store.failures(conn, days=7)
        assert [(r["fail_fp"], r["count"], r["machines"]) for r in rows] == [("aaa", 2, 2), ("bbb", 1, 1)]
    finally:
        conn.close()


def test_events_report_filters_by_type_prefix_and_machine(tmp_path):
    conn = store.open_db(tmp_path / "e.sqlite")
    try:
        store.ingest_segment(conn, "k1", _seg(
            _ev(1, "raw.run"), _ev(2, "raw.fb_api"), _ev(3, "heartbeat"),
            _ev(4, "machine.profile", machine_id="m2", ram_gb=16.0, cores_logical=8)))
        assert [e["type"] for e in store.events(conn, type="raw.")] == ["raw.run", "raw.fb_api"]
        assert [e["type"] for e in store.events(conn, machine="M2")] == ["machine.profile"]
        assert len(store.events(conn, limit=2)) == 2
        m2 = [m for m in store.machines(conn) if m["machine_id"] == "m2"][0]
        assert (m2["ram_gb"], m2["cores"]) == (16.0, 8)
    finally:
        conn.close()


def test_report_script_runs_failures_and_events(tmp_path, capsys):
    from scripts import acl_log_report
    db = tmp_path / "e.sqlite"
    conn = store.open_db(db)
    store.ingest_segment(conn, "k1", _seg(_ev(1, "publish.result", success=False, fail_fp="aaa", fail_text="tab")))
    conn.close()
    assert acl_log_report.main(["--db", str(db), "--failures"]) == 0
    assert "aaa" in capsys.readouterr().out
    assert acl_log_report.main(["--db", str(db), "--events", "--type", "publish.result"]) == 0
    assert "publish.result" in capsys.readouterr().out
