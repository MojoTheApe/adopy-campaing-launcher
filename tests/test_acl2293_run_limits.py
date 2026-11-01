# -*- coding: utf-8 -*-
"""ACL-2293: потолок профилей и лимиты памяти меняются в Settings без перезапуска.

Движок и так читает `os.environ` на каждое решение, а `update_machine_env` пишет `.env`
и окружение сразу, поэтому здесь проверяем слой над ним: проверка значений, явная запись,
телеметрия только числами, текст ожидания «потолок достигнут» и лимит тяжёлых действий.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_api import warm  # noqa: E402
from src.web import profile_slots as ps  # noqa: E402
from src.web import run_limits as rl  # noqa: E402

GB = 1024
KEYS = ("ACL_MAX_PROFILES", "ACL_MAX_PARALLEL_PROFILES", "ACL_MAX_PARALLEL_CEILING",
        "ACL_WARM_MAX_HEAVY", "ACL_MEMORY_MAX_USED_PCT")


@pytest.fixture
def machine(tmp_path, monkeypatch):
    config = tmp_path / "ACL"
    config.mkdir()
    (config / ".env").write_text("KEEP_ME=1\n", encoding="utf-8")
    monkeypatch.setenv("ACL_CONFIG_DIR", str(config))
    for key in KEYS:
        monkeypatch.delenv(key, raising=False)
    sent: list[dict] = []
    from src.fb_ads_rpa import run_telemetry
    monkeypatch.setattr(run_telemetry, "record", lambda step, ok=None, **kw: sent.append({"step": step, **kw}))
    monkeypatch.setattr(rl, "read_memory", lambda: ps.MemoryReading(16 * GB, 8 * GB))
    monkeypatch.setattr(rl, "_running", lambda: 1)
    return config / ".env", sent


def test_a_saved_value_reaches_the_engine_without_a_restart(machine):
    env, _ = machine
    assert ps.load_settings().ceiling == 2 and warm.max_heavy() == 1
    result = rl.save(ceiling=5, warm_heavy=2, memory_max_used_pct=80)
    assert result["ceiling"] == 5 and result["warm_heavy"] == 2 and result["memory_max_used_pct"] == 80
    assert result["applies"] == "next start decision" and result["running_jobs_affected"] is False
    assert ps.load_settings().ceiling == 5 and ps.load_settings().max_used_pct == 80.0
    assert warm.max_heavy() == 2
    text = env.read_text(encoding="utf-8")
    assert "KEEP_ME=1" in text and 'ACL_MAX_PROFILES="5"' in text and 'ACL_WARM_MAX_HEAVY="2"' in text


def test_a_subset_changes_only_what_was_given(machine):
    rl.save(ceiling=4)
    rl.save(warm_heavy=3)
    now = rl.current()
    assert (now["ceiling"], now["warm_heavy"], now["memory_max_used_pct"]) == (4, 3, 75)


@pytest.mark.parametrize("kwargs,word", [
    ({"ceiling": 0}, "1 to 8"), ({"ceiling": 9}, "1 to 8"), ({"ceiling": 2.5}, "whole number"),
    ({"ceiling": True}, "number"), ({"ceiling": "auto"}, "number"), ({"ceiling": "3 profiles"}, "units"),
    ({"ceiling": float("nan")}, "number"), ({"ceiling": float("inf")}, "number"), ({"ceiling": []}, "number"),
    ({"warm_heavy": 4}, "1 to 3"), ({"warm_heavy": 0}, "1 to 3"),
    ({"memory_max_used_pct": 49}, "50 to 90"), ({"memory_max_used_pct": 91}, "50 to 90"),
    ({"memory_max_used_pct": "75%"}, "units"),
])
def test_a_bad_value_is_refused_and_nothing_is_written(machine, kwargs, word):
    env, sent = machine
    with pytest.raises(rl.RunLimitError, match=word):
        rl.save(**kwargs)
    assert env.read_text(encoding="utf-8") == "KEEP_ME=1\n" and sent == []


def test_one_bad_field_stops_the_good_one_too(machine):
    env, _ = machine
    with pytest.raises(rl.RunLimitError):
        rl.save(ceiling=4, warm_heavy=9)
    assert env.read_text(encoding="utf-8") == "KEEP_ME=1\n" and ps.load_settings().ceiling == 2


def test_numbers_are_accepted_as_text_and_with_a_decimal_comma(machine):
    result = rl.save(ceiling="3", warm_heavy=" 2 ", memory_max_used_pct="72,5")
    assert (result["ceiling"], result["warm_heavy"], result["memory_max_used_pct"]) == (3, 2, 72.5)


def test_a_legacy_ceiling_key_cannot_override_the_saved_value(machine, monkeypatch):
    monkeypatch.setenv("ACL_MAX_PARALLEL_CEILING", "4")
    rl.save(ceiling=6)
    assert rl.current()["ceiling"] == 6


def test_reset_writes_explicit_defaults(machine, monkeypatch):
    env, _ = machine
    monkeypatch.setenv("ACL_MAX_PARALLEL_CEILING", "4")
    rl.save(ceiling=7, warm_heavy=3, memory_max_used_pct=88)
    result = rl.reset()
    assert (result["ceiling"], result["warm_heavy"], result["memory_max_used_pct"]) == (2, 1, 75)
    text = env.read_text(encoding="utf-8")
    assert 'ACL_MAX_PROFILES="2"' in text and 'ACL_WARM_MAX_HEAVY="1"' in text and 'ACL_MEMORY_MAX_USED_PCT="75"' in text


def test_an_unwritable_env_changes_nothing_in_memory(machine, monkeypatch):
    def boom(values):
        raise OSError("locked")
    monkeypatch.setattr(rl, "update_machine_env", boom)
    with pytest.raises(rl.RunLimitError, match="could not be saved"):
        rl.save(ceiling=5)
    assert ps.load_settings().ceiling == 2


def test_warnings_for_a_risky_ceiling_or_share(machine):
    assert rl.current()["warnings"] == []
    risky = rl.save(ceiling=5, memory_max_used_pct=85)
    assert len(risky["warnings"]) == 2
    big = rl.warnings_for({"ceiling": 5, "memory_max_used_pct": 75}, 64 * GB)
    assert big == []


def test_the_change_is_logged_with_numbers_only(machine):
    _, sent = machine
    rl.save(ceiling=4, memory_max_used_pct=80)
    rows = [r for r in sent if r.get("note") == "run_limits_changed"]
    assert len(rows) == 1
    diag = rows[0]["diag"]
    assert (diag["old_ceiling"], diag["new_ceiling"], diag["new_memory_max_used_pct"]) == (2, 4, 80)
    assert diag["source"] == "settings" and diag["total_mb"] == 16 * GB
    assert all(isinstance(v, (int, float, str)) for v in diag.values())
    assert set(diag) == {"old_ceiling", "old_warm_heavy", "old_memory_max_used_pct", "new_ceiling",
                         "new_warm_heavy", "new_memory_max_used_pct", "running", "waiting",
                         "total_mb", "used_pct", "source"}


# ── the routes ───────────────────────────────────────────────────────────

class _Req:
    def __init__(self, body):
        self.body = body

    async def json(self):
        if isinstance(self.body, Exception):
            raise self.body
        return self.body


def test_the_routes_read_save_reset_and_reject(machine):
    from src.web import app as web
    got = json.loads(web.api_run_limits().body)
    assert got["ok"] and got["bounds"]["ceiling"] == [1, 8] and got["defaults"]["ceiling"] == 2
    ok = json.loads(asyncio.run(web.api_save_run_limits(_Req({"ceiling": 3}))).body)
    assert ok["ok"] and ok["ceiling"] == 3 and ok["changed"] == ["ceiling"]
    bad = asyncio.run(web.api_save_run_limits(_Req({"ceiling": 99})))
    assert bad.status_code == 400 and "1 to 8" in json.loads(bad.body)["error"]
    assert asyncio.run(web.api_save_run_limits(_Req({"reserve_gb": 1}))).status_code == 400
    assert asyncio.run(web.api_save_run_limits(_Req(ValueError("x")))).status_code == 400
    assert asyncio.run(web.api_save_run_limits(_Req([1]))).status_code == 400
    reset = json.loads(asyncio.run(web.api_save_run_limits(_Req({"reset": True}))).body)
    assert reset["ceiling"] == 2


# ── the queue follows the saved value ────────────────────────────────────

def _until(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return bool(cond())


def _queue():
    now = [1000.0]
    return ps.ProfileSlots(memory=lambda: ps.MemoryReading(31 * GB, 20 * GB), settings=ps.load_settings,
                           clock=lambda: now[0], tick_s=0.02,
                           usage=ps.ProfileUsage(lambda p: {}, clock=lambda: now[0], background=False))


def test_a_waiting_job_starts_after_the_ceiling_is_raised(machine):
    q = _queue()
    assert q.acquire("a", "P1") and q.acquire("b", "P2")
    got: list = []
    threading.Thread(target=lambda: got.append(q.acquire("c", "P3")), daemon=True).start()
    time.sleep(0.2)
    assert got == [] and q.snapshot()["next"]["reason"] == "ceiling"
    rl.save(ceiling=3)
    q.wake()
    assert _until(lambda: len(got) == 1 and got[0] is not None)
    for job in "abc":
        q.release(job)


def test_lowering_the_ceiling_below_the_running_count_stops_nobody(machine):
    rl.save(ceiling=3)
    q = _queue()
    for i in range(3):
        assert q.acquire(f"j{i}", f"P{i}")
    rl.save(ceiling=1)
    snap = q.snapshot()
    assert len(snap["running"]) == 3, "running jobs are never closed"
    assert snap["next"]["allow"] is False and snap["next"]["reason"] == "ceiling"
    assert snap["next"]["running"] == 3 and snap["next"]["cap"] == 1


# ── heavy-action limit is its own wait ───────────────────────────────────

def test_the_heavy_limit_is_reported_as_the_limit_not_as_an_account_wait():
    mem = ps.MemoryReading(16 * GB, 10 * GB)
    gate = warm.HeavyGate(limit=lambda: 2, memory=lambda: mem, settings=lambda: ps.SlotSettings(), usage=None)
    gate._active.update({"p1": 1, "p2": 1})
    allowed, why = gate._allowed()
    assert (allowed, why) == (False, "limit")
    view = gate.wait_view(why)
    assert view["hold"] == "heavy_limit" and view["heavy_limit"] == 2 and view["running"] == 2
    assert gate.wait_view("memory_ok")["hold"] == "account_busy"


def test_the_queue_view_carries_the_heavy_limit():
    from src.web import publish_jobs as pj
    q = pj._queue_view({"reason": "heavy_limit", "heavy_limit": 2, "running": 2})
    assert q["hold"] == "heavy_limit" and q["heavy_limit"] == 2 and q["state"] == "waiting_slot"


def test_a_ceiling_decision_is_marked_in_telemetry():
    from src.web import publish_jobs as pj
    rows: list = []

    class Rt:
        def record(self, step, ok=None, **kw):
            rows.append(kw)

    pj._record_slot_decision(Rt(), "c", "A", {"reason": "ceiling", "limit": 3, "running": 3, "cap": 3})
    pj._record_slot_decision(Rt(), "c", "A", {"reason": "memory_low", "limit": 1})
    assert rows[0]["diag"]["decision"]["ceiling_reached"] is True
    assert rows[0]["diag"]["decision"]["effective_limit"] == 3
    assert rows[1]["diag"]["decision"]["ceiling_reached"] is False


# ── wait text and page ───────────────────────────────────────────────────

def _wait_text(q: dict) -> str:
    html = (ROOT / "src/web/templates/report.html").read_text(encoding="utf-8")
    start = html.index("function cbCeilingWait(q)")
    body = html[start:html.index("/* ACL-2113", start)]
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    out = subprocess.run([node, "-e", body + f"\nconsole.log(JSON.stringify(cbCeilingWait({json.dumps(q)})));"],
                         capture_output=True, text=True, check=True, timeout=30)
    return json.loads(out.stdout)


def test_ceiling_wait_text():
    assert _wait_text({"hold": "ceiling", "running": 5, "cap": 5, "ahead": 2}) == (
        "Waiting: 5 of 5 profiles in use (ceiling 5) (2 ahead). The next one starts when a run finishes.")
    lowered = _wait_text({"hold": "ceiling", "running": 5, "cap": 3})
    assert "5 profiles running, ceiling is now 3" in lowered and "fewer than 3" in lowered
    assert "of 5" not in lowered
    assert _wait_text({"hold": "memory_low", "running": 1}) is None


def test_heavy_limit_wait_text():
    text = _wait_text({"hold": "heavy_limit", "running": 2, "heavy_limit": 2})
    assert text.startswith("Waiting: 2 of 2 heavy actions in progress (limit 2)")
    assert "1 heavy action in" in _wait_text({"hold": "heavy_limit", "running": 1, "heavy_limit": 1})


def test_the_settings_page_has_the_run_limits_block_and_both_queue_lines_use_the_text():
    html = (ROOT / "src/web/templates/report.html").read_text(encoding="utf-8")
    assert 'id="rl-ceiling"' in html and 'id="rl-heavy"' in html and 'id="rl-share"' in html
    assert "/api/run-limits" in html and "This can overload your computer" in html
    assert html.count("cbCeilingWait(") >= 6
