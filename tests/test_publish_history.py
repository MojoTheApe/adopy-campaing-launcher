# -*- coding: utf-8 -*-
"""ACL-2292: publish job results survive a dashboard restart (publish_history.jsonl)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.web import publish_history as ph  # noqa: E402
from src.web import publish_jobs as pj  # noqa: E402


@pytest.fixture(autouse=True)
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("ACL_CONFIG_DIR", str(tmp_path))
    ph._written.clear()
    return tmp_path


def _job(job_id="j1", **over):
    job = {
        "job_id": job_id, "account_label": "FacebookDoc19", "country": "MX", "status": "done",
        "created_at": "2026-10-29T10:00:00+00:00", "started_at": "2026-10-29T10:00:01+00:00",
        "finished_at": "2026-10-29T10:05:00+00:00", "fb_backend": "api", "total": 1, "done_count": 1,
        "publishable": True, "errors": [],
        "results": [{
            "campaign_name": "MX Test 1", "success": True, "publishable": True,
            "warnings": ["w1"], "blocking_failures": [], "campaign_id": "120201234567890123",
            "adset_id": "120201234567890124", "ad_id": "120201234567890125",
            "ad_ids": ["120201234567890125"], "creative_id": "9988776655443322",
            "error": None, "steps_completed": ["open", "fill"], "screenshots": ["C:/x.png"],
        }],
    }
    job.update(over)
    return job


def _lines(cfg):
    return (cfg / "publish_history.jsonl").read_text(encoding="utf-8").splitlines()


def test_job_end_writes_one_line(cfg):
    assert ph.record_job(_job())
    assert not ph.record_job(_job())          # second end path: still one line
    lines = _lines(cfg)
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["job_id"] == "j1" and row["account_label"] == "FacebookDoc19"
    camp = row["results"][0]
    assert camp["campaign_id"] == "120201234567890123"        # long ids stay readable
    assert camp["adset_id"] and camp["ad_id"] and camp["creative_id"]
    assert camp["status"] == "ready" and camp["steps"] == ["open", "fill"]
    assert "screenshots" not in camp and "build" in row


def test_pj_finish_hook_writes_history(cfg):
    pj._jobs["jf"] = {**_job("jf", status="running", finished_at=None), "cancel_requested": False}
    try:
        pj._finish("jf")
    finally:
        pj._jobs.pop("jf", None)
    rows = [json.loads(x) for x in _lines(cfg)]
    assert [r["job_id"] for r in rows] == ["jf"] and rows[0]["status"] == "done"


def test_lookup_after_restart_returns_stored_result(cfg):
    ph.record_job(_job("j7"))
    pj._jobs.clear()                          # "restart": fresh in-memory store
    assert pj.get_job("j7") is None
    found = ph.find_job("j7")
    assert found["from_history"] is True and found["status"] == "done"
    assert found["results"][0]["campaign_name"] == "MX Test 1"
    assert ph.find_job("nope") is None


def test_status_endpoint_falls_back_to_file(cfg):
    from starlette.testclient import TestClient
    from src.web.app import app

    ph.record_job(_job("j8"))
    client = TestClient(app, base_url="http://127.0.0.1")
    r = client.get("/publish-jobs/j8")
    assert r.status_code == 200 and r.json()["job_id"] == "j8"
    assert client.get("/publish-jobs/missing").status_code == 404


def test_secrets_are_not_written(cfg):
    token = "EAAB" + "x" * 40
    job = _job(errors=[f"access_token={token} failed"])
    job["results"][0]["steps_completed"] = [
        f"Bearer {token}", "Cookie: c_user=100012345; xs=abc%3Adef; datr=zzz",
        "GET https://graph.facebook.com/v20/act?access_token=SECRETQ&x=1#frag",
        'json {"password": "hunter2"}',
    ]
    job["results"][0]["error"] = f"refresh_token=RT123456 {token}"
    ph.record_job(job)
    raw = (cfg / "publish_history.jsonl").read_text(encoding="utf-8")
    for secret in (token, "xxxxxxxxxxxxxxxx", "c_user=100012345", "abc%3Adef", "datr=zzz",
                   "SECRETQ", "frag", "hunter2", "RT123456"):
        assert secret not in raw, secret
    assert "https://graph.facebook.com/v20/act" in raw
    assert "120201234567890123" in raw


def test_rotation_keeps_three_old_files(cfg, monkeypatch):
    monkeypatch.setattr(ph, "MAX_BYTES", 300)
    for i in range(12):
        ph.record_job(_job(f"r{i}"))
    names = sorted(p.name for p in cfg.glob("publish_history.jsonl*"))
    assert names == ["publish_history.jsonl", "publish_history.jsonl.1",
                     "publish_history.jsonl.2", "publish_history.jsonl.3"]
    assert ph.find_job("r11") is not None
    assert ph.find_job("r0") is None          # fell off the oldest kept file


def test_broken_last_line_is_tolerated(cfg):
    ph.record_job(_job("a"))
    with open(cfg / "publish_history.jsonl", "ab") as fh:
        fh.write(b'{"job_id": "broken", "resu')      # crash mid-write, no newline
    assert ph.find_job("a") is not None
    assert [r["job_id"] for r in ph.recent()] == ["a"]
    ph.record_job(_job("b"))                         # next write starts a fresh line
    assert [r["job_id"] for r in ph.recent()] == ["b", "a"]


def test_recent_endpoint_returns_last_50_newest_first(cfg):
    from starlette.testclient import TestClient
    from src.web.app import app

    for i in range(60):
        ph.record_job(_job(f"n{i:02d}"))
    body = TestClient(app, base_url="http://127.0.0.1").get("/publish-history").json()
    ids = [r["job_id"] for r in body["runs"]]
    assert len(ids) == 50 and ids[0] == "n59" and ids[-1] == "n10"
    assert body["runs"][0]["campaigns"][0]["name"] == "MX Test 1"
