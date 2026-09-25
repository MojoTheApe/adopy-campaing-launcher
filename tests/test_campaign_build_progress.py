"""`GET /publish-jobs/{id}/progress` — факты для точки сборки (ACL-1315).

Точка у имени кампании в ящике билдера решает свой цвет по этому ответу:
итог кампании, последний шаг робота из телеметрии, сколько секунд задание
молчит и что роботу не по силам. Здесь проверяется, что факты верные:
чужие и старые строки телеметрии не попадают, хвост файла читается
ограниченно, остановки «нужен человек» распознаются.
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.web import publish_jobs as pj  # noqa: E402
from src.web import publish_progress as pp  # noqa: E402

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)


def _iso(delta_s: float) -> str:
    return (NOW + timedelta(seconds=delta_s)).isoformat()


def _job(**over):
    job = {
        "job_id": "job-1",
        "account_label": "Demo",
        "status": "running",
        "created_at": _iso(-600),
        "started_at": _iso(-590),
        "campaign_names": ["CL_A", "CL_B", "CL_C"],
        "results": [],
        "errors": [],
    }
    job.update(over)
    return job


def _write(path: Path, rows: list) -> Path:
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(row if isinstance(row, str) else json.dumps(row))
            f.write("\n")
    return path


def _by_name(progress: dict) -> dict:
    return {c["name"]: c for c in progress["campaigns"]}


def test_last_step_per_campaign_skips_old_and_foreign_rows(tmp_path):
    tele = _write(tmp_path / "t.jsonl", [
        {"ts": _iso(-900), "campaign": "CL_B", "step": "from_yesterday", "ok": True},   # до задания
        {"ts": _iso(-120), "campaign": "CL_A", "step": "ad_level_summary", "ok": True},
        {"ts": _iso(-60), "campaign": "OTHER", "step": "not_ours", "ok": True},
        '{"ts": "broken',
        {"ts": _iso(-30), "campaign": "CL_B", "step": "audience_age", "ok": True},
        {"ts": _iso(-20), "campaign": "CL_B", "step": "targeting", "ok": False, "note": "retrying"},
        {"ts": _iso(-10), "step": "window_bounds", "ok": True},   # без кампании — не наша забота
    ])
    job = _job(results=[{"campaign_name": "CL_A", "success": True, "publishable": True}])
    p = pp.job_progress(job, path=tele, now=NOW)
    c = _by_name(p)

    assert [x["name"] for x in p["campaigns"]] == ["CL_A", "CL_B", "CL_C"]
    assert p["current"] == "CL_B", "идёт первая кампания без итога"
    assert c["CL_A"]["result"]["publishable"] is True
    assert c["CL_B"]["step"] == "targeting" and c["CL_B"]["ok"] is False
    assert c["CL_B"]["note"] == "retrying"
    assert c["CL_B"]["idle_s"] == 20.0
    assert c["CL_C"]["step"] == "" and c["CL_C"]["idle_s"] is None
    assert p["idle_s"] == 20.0, "тишина задания — от последнего шага любой его кампании"
    assert p["active"] is True and p["stall_after_s"] == 180
    assert not any(x["blocker"] for x in p["campaigns"])


def test_silence_is_counted_from_the_start_before_the_first_step(tmp_path):
    tele = _write(tmp_path / "t.jsonl", [])
    p = pp.job_progress(_job(started_at=_iso(-240)), path=tele, now=NOW)
    assert p["idle_s"] == 240.0
    assert p["idle_s"] >= p["stall_after_s"]


def test_a_queued_or_finished_job_is_never_silent(tmp_path):
    tele = _write(tmp_path / "t.jsonl", [])
    for status in ("queued", "done", "cancelled"):
        p = pp.job_progress(_job(status=status, started_at=_iso(-3600)), path=tele, now=NOW)
        assert p["idle_s"] is None and p["current"] == ""
    assert pp.job_progress(_job(status="done"), path=tele, now=NOW)["active"] is False


def test_conditions_the_robot_cannot_solve_are_named(tmp_path):
    tele = _write(tmp_path / "t.jsonl", [
        {"ts": _iso(-50), "campaign": "CL_A", "step": "account_blocked", "ok": False},
        {"ts": _iso(-40), "campaign": "CL_B", "step": "lead_terms_not_accepted", "ok": False},
        {"ts": _iso(-30), "campaign": "CL_C", "step": "create", "ok": False},
        {"ts": _iso(-20), "campaign": "CL_C", "step": "create", "ok": False},
    ])
    c = _by_name(pp.job_progress(_job(), path=tele, now=NOW))
    assert "restricted by Meta" in c["CL_A"]["blocker"]
    assert "Lead Generation terms" in c["CL_B"]["blocker"]
    assert c["CL_C"]["blocker"] == "", "две попытки из трёх — робот ещё пробует"

    _write(tele, [{"ts": _iso(-30 + i), "campaign": "CL_C", "step": "create", "ok": False} for i in range(3)])
    c = _by_name(pp.job_progress(_job(), path=tele, now=NOW))
    assert "Gave up after 3 attempts at step 'create'" in c["CL_C"]["blocker"]


def test_blockers_also_come_from_the_result(tmp_path):
    tele = _write(tmp_path / "t.jsonl", [])
    job = _job(results=[
        {"campaign_name": "CL_A", "success": False, "publishable": False,
         "blocking_failures": ["account_blocked"], "error": "Recorded flow failed at: account_blocked"},
        {"campaign_name": "CL_B", "success": False, "publishable": False,
         "steps_completed": ["recorded_flow_retry_exhausted:objective_leads:attempt3"]},
        {"campaign_name": "CL_C", "success": True, "publishable": False,
         "blocking_failures": ["ad_copy"]},
    ])
    c = _by_name(pp.job_progress(job, path=tele, now=NOW))
    assert "restricted by Meta" in c["CL_A"]["blocker"]
    assert "objective_leads" in c["CL_B"]["blocker"]
    assert c["CL_C"]["blocker"] == ""
    assert c["CL_C"]["result"] == {"success": True, "publishable": False, "error": "",
                                   "blocking_failures": ["ad_copy"]}


def test_only_the_tail_of_the_file_is_read(tmp_path):
    rows = [{"ts": _iso(-500 + i), "campaign": "CL_A", "step": f"s{i}", "ok": True} for i in range(400)]
    tele = _write(tmp_path / "t.jsonl", rows)
    tail = pp.tail_rows(tele, max_bytes=2000)
    assert 0 < len(tail) < 40
    assert tail[-1]["step"] == "s399"
    assert all(isinstance(r, dict) and "step" in r for r in tail), "обрезанная первая строка выброшена"
    assert pp.tail_rows(tmp_path / "missing.jsonl") == []


async def _get(path: str) -> httpx.Response:
    from src.web.app import app

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        return await client.get(path)


@pytest.fixture
def registered_job():
    job_id = "acl-1315-test"
    now = datetime.now(timezone.utc)
    with pj._jobs_lock:
        pj._jobs[job_id] = {
            "job_id": job_id, "account_label": "Demo", "status": "running",
            "created_at": (now - timedelta(minutes=5)).isoformat(),
            "started_at": (now - timedelta(minutes=5)).isoformat(),
            "campaign_names": ["CL_A", "CL_B"], "results": [], "errors": [],
            "creative_notices": [], "total": 2, "done_count": 0,
        }
    yield job_id, now
    with pj._jobs_lock:
        pj._jobs.pop(job_id, None)


def test_the_endpoint_answers_for_a_known_job(tmp_path, monkeypatch, registered_job):
    job_id, now = registered_job
    tele = _write(tmp_path / "t.jsonl", [
        {"ts": (now - timedelta(seconds=5)).isoformat(), "campaign": "CL_A", "step": "ad_copy", "ok": True},
    ])
    monkeypatch.setattr(pp, "_telemetry_path", lambda: tele)
    r = asyncio.run(_get(f"/publish-jobs/{job_id}/progress"))
    assert r.status_code == 200
    body = r.json()
    assert body["current"] == "CL_A"
    assert body["campaigns"][0]["step"] == "ad_copy"
    assert body["idle_s"] < 180
    # Статус задания тоже несёт имена — страница узнаёт свои строки.
    assert asyncio.run(_get(f"/publish-jobs/{job_id}")).json()["campaign_names"] == ["CL_A", "CL_B"]


def test_an_unknown_job_is_404_not_running():
    r = asyncio.run(_get("/publish-jobs/gone-after-restart/progress"))
    assert r.status_code == 404
