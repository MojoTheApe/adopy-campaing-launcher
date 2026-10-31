"""Точки подключения журнала (ACL-2347): сборщики, зеркала и места в коде."""
from __future__ import annotations

import json

import pytest

from src.acl_event_log import emit, writer
from src.acl_event_log.events import (
    account_payload, check_payload, slots_brief, spec_payload, stats_payload,
)
from src.acl_event_log.writer import use_outbox_for_tests
from src.fb_ads_rpa import run_telemetry
from src.fb_api import commands, creator as cr, graph as g, telemetry
from src.web import publish_api, publish_jobs
from tests.test_acl1761_api_create import ACT, FakeMeta, _draft, _publisher


@pytest.fixture
def log(tmp_path):
    out = tmp_path / "outbox"
    use_outbox_for_tests(out)
    yield out
    use_outbox_for_tests(None)
    run_telemetry.clear_current_run()


def lines(p):
    return [json.loads(x) for f in sorted(p.glob("*.ndjson")) for x in f.read_text("utf-8").splitlines()]


def types(p):
    return [x["type"] for x in lines(p)]


# ───────────────────────── сборщики ─────────────────────────

def test_slots_brief_has_no_link():
    out = slots_brief([{"name": "c", "targeting": "T1", "copy": "A", "tracking_link": "https://x/?s=1"}])
    assert out == [{"name": "c", "targeting": "T1", "copy": "A"}]


def _plan(tmp_path, link="https://t.example/?sub=p7"):
    draft = _draft(tmp_path, ads=1)
    draft.link = link
    draft.ad_sets[0].ads[0].ad_title = "Hello"
    return cr.plan_from_draft(draft)


def test_spec_payload_fingerprints_link_and_keeps_text(tmp_path):
    plan = _plan(tmp_path)
    facts = cr.preflight_account(g.GraphClient(FakeMeta(), allow_writes=True, sleep=lambda s: None,
                                               min_interval_s=0.0), ACT)
    p = spec_payload(plan, facts)
    assert p["ads"][0]["title"] == "Hello"
    assert p["link"]["param_names"] == ["sub"] and "p7" not in json.dumps(p)
    assert len(p["spec_hash"]) == 64 and p["currency"] == facts.currency


def test_spec_payload_missing_file_keeps_going(tmp_path):
    plan = _plan(tmp_path)
    plan.ad_sets[0].ads[0].path = str(tmp_path / "nope.png")
    p = spec_payload(plan, None)
    assert p["ads"][0]["file_size"] is None and p["currency"] == ""


def test_check_payload_counts_verdicts_and_early_exit():
    res = {"results": [{"audit": [{"rule": "page_ok", "status": "error"}, {"rule": "budget_min", "status": "ok"}]}],
           "error_kind": ""}
    p = check_payload(res, plan_hash_value="h", duration_s=1.2)
    assert p["verdicts"] == {"page_ok": "error", "budget_min": "ok"} and p["blocking_rules"] == ["page_ok"]
    assert check_payload({"results": [], "error_kind": "plan"}, plan_hash_value="h", duration_s=0)["error_kind"] == "plan"


def test_account_payload_refused_row():
    p = account_payload("act_1", {"account_status": 2, "disable_reason": 3})
    assert p["access"] is True and p["account_status"] == 2 and p["disable_reason"] == 3


def test_account_payload_no_access():
    assert account_payload("act_1", None)["access"] is False


# ───────────────────────── зеркала ─────────────────────────

def test_telemetry_record_mirrors_after_scrub(log, tmp_path):
    telemetry.record({"event": "fb_api_mlx_start", "ok": True}, path=tmp_path / "j.jsonl")
    row = lines(log)[0]
    assert row["type"] == "raw.fb_api" and row["payload"]["event"] == "fb_api_mlx_start"


def test_run_telemetry_mirror_filters(log, tmp_path):
    run_telemetry.record("api_create_step", True, path=tmp_path / "r.jsonl")
    run_telemetry.record("click_refused", False, path=tmp_path / "r.jsonl")
    assert [x["payload"]["step"] for x in lines(log)] == ["api_create_step"]


def test_persist_emits_command(log, tmp_path):
    store = commands.OperationStore(path=tmp_path / "h.jsonl")
    store._persist({"op_id": "o1", "command": "set_budget", "value": 5, "state": "done",
                    "actor": "me", "account": "acc", "affected": [{"id": "120212345678901234"}]})
    row = lines(log)[0]
    assert row["type"] == "command.executed" and row["account"] == "acc"
    assert row["payload"]["affected"] == [{"id": "120212345678901234"}]


# ───────────────────────── места подключения ─────────────────────────

class _ClosedMeta(FakeMeta):
    """Кабинет есть в /me/adaccounts, но не активен."""


def test_preflight_emits_on_refusal(log):
    client = g.GraphClient(FakeMeta(), allow_writes=True, sleep=lambda s: None, min_interval_s=0.0)
    with pytest.raises(cr.PlanError):
        cr.preflight_account(client, "act_1234567890123456")   # кабинета нет у профиля
    row = next(x for x in lines(log) if x["type"] == "account.state")
    assert row["payload"]["access"] is False


def test_preflight_emits_on_success(log):
    client = g.GraphClient(FakeMeta(), allow_writes=True, sleep=lambda s: None, min_interval_s=0.0)
    cr.preflight_account(client, ACT)
    row = next(x for x in lines(log) if x["type"] == "account.state")
    assert row["payload"]["access"] is True
    assert row["payload"]["currency"]


@pytest.fixture
def jobs(monkeypatch):
    monkeypatch.setattr(publish_jobs, "_persist_job", lambda *a, **k: None)
    monkeypatch.setattr(publish_jobs, "_record_history", lambda *a, **k: None)
    saved = dict(publish_jobs._jobs)
    publish_jobs._jobs.clear()
    yield publish_jobs._jobs
    publish_jobs._jobs.clear()
    publish_jobs._jobs.update(saved)


def _job(jobs, job_id, backend):
    jobs[job_id] = {"job_id": job_id, "account_label": "Test", "fb_backend": backend, "status": "running",
                    "results": [], "errors": [], "done_count": 0, "created_at": "2026-10-30T10:00:00+00:00",
                    "started_at": "2026-10-30T10:00:05+00:00", "finished_at": None}
    return job_id


def test_append_result_emits_for_api_only(log, jobs):
    api_job, rpa_job = _job(jobs, "ja", "api"), _job(jobs, "jr", "rpa")
    publish_jobs._append_result(api_job, {"campaign_name": "c", "campaign_id": "120212345678901234", "error": ""})
    publish_jobs._append_result(rpa_job, {"campaign_name": "c"})
    rows = [x for x in lines(log) if x["type"] == "publish.result"]
    assert len(rows) == 1 and rows[0]["payload"]["campaign_id"] == "120212345678901234"
    assert rows[0]["job_id"] == "ja" and rows[0]["campaign"] == "c"


def test_finish_emits_job_finished_once(log, jobs):
    job_id = _job(jobs, "jf", "api")
    publish_jobs._finish(job_id)
    publish_jobs._log_job_finished(job_id)
    rows = [x for x in lines(log) if x["type"] == "job.finished"]
    assert len(rows) == 1 and rows[0]["payload"]["status"] == "done"
    assert rows[0]["payload"]["duration_s"] is not None


def test_publish_survives_broken_log(monkeypatch, jobs):
    def boom(*a, **k):
        raise RuntimeError("log broke")

    monkeypatch.setattr("src.acl_event_log.writer.emit", boom)
    monkeypatch.setattr("src.acl_event_log.emit", boom)
    job_id = _job(jobs, "jb", "api")
    publish_jobs._append_result(job_id, {"campaign_name": "c"})
    publish_jobs._finish(job_id)
    assert jobs[job_id]["done_count"] == 1 and jobs[job_id]["status"] == "done"


def test_check_plan_logged_emits_and_returns_result_unchanged(log, monkeypatch):
    result = {"account": "Test", "results": [], "error_kind": "plan", "error": "x", "dry_run": True}
    monkeypatch.setattr(publish_api, "_check_plan_once", lambda *a, **k: result)
    slots = [{"name": "c"}]
    out = publish_api._check_plan_logged(slots, account_label="Test", country="IT")
    assert out is result
    row = lines(log)[0]
    assert row["type"] == "plan.checked" and row["account"] == "Test"
    assert row["payload"]["error_kind"] == "plan"
    from src.acl_event_log.clean import plan_hash
    assert row["payload"]["plan_hash"] == plan_hash("IT", slots)


def test_check_plan_survives_broken_log(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("log broke")

    result = {"results": [], "error_kind": ""}
    monkeypatch.setattr(publish_api, "_check_plan_once", lambda *a, **k: result)
    monkeypatch.setattr("src.acl_event_log.emit", boom)
    assert publish_api._check_plan_logged([], account_label="Test", country="IT") is result


def test_publish_draft_logs_spec_and_stats(log, tmp_path):
    meta = FakeMeta()
    pub = _publisher(meta)
    res = pub.publish_draft(_draft(tmp_path, ads=1))
    assert res["error"] is None
    spec = next(x for x in lines(log) if x["type"] == "publish.spec")
    stats = next(x for x in lines(log) if x["type"] == "publish.stats")
    assert stats["payload"]["spec_hash"] == spec["payload"]["spec_hash"]
    assert stats["payload"]["calls"] == pub._client.calls and stats["payload"]["writes"] == pub._client.writes
    assert stats["payload"]["raised"] is None
    media = next(iter(stats["payload"]["ad_media"].values()))
    assert len(media["file_md5"]) == 32
    assert "example.invalid/l?" not in json.dumps(spec)


def test_publish_draft_logs_stats_on_exception(log, tmp_path, monkeypatch):
    pub = _publisher(FakeMeta())

    def boom(self, plan):
        raise RuntimeError("creator broke")

    monkeypatch.setattr(cr.CampaignCreator, "run", boom)
    with pytest.raises(RuntimeError):
        pub.publish_draft(_draft(tmp_path, ads=1))
    stats = next(x for x in lines(log) if x["type"] == "publish.stats")
    assert stats["payload"]["raised"] == "RuntimeError"


def test_publish_draft_dry_run_logs_no_spec(log, tmp_path):
    pub = _publisher(FakeMeta(), dry_run=True)
    pub.publish_draft(_draft(tmp_path, ads=1))
    assert "publish.spec" not in types(log) and "publish.stats" not in types(log)


def test_publish_draft_result_same_with_broken_log(tmp_path, monkeypatch):
    plain = _publisher(FakeMeta()).publish_draft(_draft(tmp_path, ads=1))

    def boom(*a, **k):
        raise RuntimeError("log broke")

    monkeypatch.setattr("src.acl_event_log.emit", boom)
    broken = _publisher(FakeMeta()).publish_draft(_draft(tmp_path, ads=1))
    for key in ("success", "publishable", "error", "created", "reused", "status"):
        assert plain[key] == broken[key]
