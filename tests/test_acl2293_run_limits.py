# -*- coding: utf-8 -*-
"""ACL-2293: потолок профилей, тяжёлые действия и доля памяти меняются в Settings без перезапуска."""
from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path

import pytest

from src.fb_api import warm
from src.web import profile_slots as ps
from src.web import run_limits as rl

ROOT = Path(__file__).resolve().parent.parent
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
    # update_machine_env выставляет os.environ напрямую: вернуть после теста.
    import os
    saved = {k: os.environ.get(k) for k in KEYS}
    yield config / ".env"
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


@pytest.fixture
def no_side_effects(monkeypatch):
    rows = []
    from src.fb_ads_rpa import run_telemetry as rt
    monkeypatch.setattr(rt, "record", lambda *a, **kw: rows.append((a, kw)))
    return rows


def test_a_change_reaches_the_engine_without_a_restart(machine, no_side_effects):
    assert ps.load_settings().ceiling == 2 and warm.max_heavy() == 1
    result = rl.save(5, 2, 80)
    assert result["ceiling"] == 5 and result["warm_heavy"] == 2 and result["memory_max_used_pct"] == 80
    assert result["running_jobs_affected"] is False and result["applies"] == "next start decision"
    settings = ps.load_settings()
    assert settings.ceiling == 5 and settings.max_used_pct == 80.0 and warm.max_heavy() == 2
    text = machine.read_text(encoding="utf-8")
    assert "KEEP_ME=1" in text and "ACL_MAX_PROFILES" in text


@pytest.mark.parametrize("kwargs", [
    {"ceiling": 0}, {"ceiling": 9}, {"ceiling": 2.5}, {"ceiling": "auto"}, {"ceiling": True},
    {"ceiling": "3 profiles"}, {"ceiling": float("nan")}, {"ceiling": None, "warm_heavy": 4},
    {"warm_heavy": 0}, {"memory_max_used_pct": 49}, {"memory_max_used_pct": 96},
    {"memory_max_used_pct": "abc"}, {"memory_max_used_pct": float("inf")},
])
def test_bad_values_change_nothing(machine, no_side_effects, kwargs):
    before = machine.read_bytes()
    with pytest.raises(rl.RunLimitError):
        rl.save(**kwargs)
    assert machine.read_bytes() == before and not no_side_effects


def test_one_bad_field_rejects_the_whole_save(machine, no_side_effects):
    with pytest.raises(rl.RunLimitError):
        rl.save(ceiling=4, memory_max_used_pct=99)
    assert machine.read_text(encoding="utf-8") == "KEEP_ME=1\n"


def test_bounds_accepted_and_comma_decimal(machine, no_side_effects):
    assert rl.save(ceiling=8, warm_heavy=3, memory_max_used_pct="95")["memory_max_used_pct"] == 95
    assert rl.save(ceiling=1, memory_max_used_pct="50,5")["memory_max_used_pct"] == 50.5


def test_reset_writes_explicit_defaults_not_blank_keys(machine, no_side_effects, monkeypatch):
    # Старый ключ из прежних настроек не должен «просочиться» после сброса.
    monkeypatch.setenv("ACL_MAX_PARALLEL_CEILING", "4")
    rl.save(6, 3, 70)
    result = rl.save(reset=True)
    assert (result["ceiling"], result["warm_heavy"], result["memory_max_used_pct"]) == (2, 1, 92)
    text = machine.read_text(encoding="utf-8")
    assert all(f in text for f in ("ACL_MAX_PROFILES=\"2\"", "ACL_MEMORY_MAX_USED_PCT=\"92\"", "ACL_WARM_MAX_HEAVY=\"1\""))


def test_telemetry_row_has_numbers_only(machine, no_side_effects):
    rl.save(4, None, None)
    rows = [kw for a, kw in no_side_effects if kw.get("note") == "run_limits_changed"]
    assert len(rows) == 1
    diag = rows[0]["diag"]
    assert diag["old"]["ceiling"] == 2 and diag["new"]["ceiling"] == 4 and diag["source"] == "settings"

    def numbers(value):
        if isinstance(value, dict):
            return all(numbers(v) for k, v in value.items() if k != "source")
        return value is None or isinstance(value, (int, float)) and not isinstance(value, bool)
    assert numbers(diag)


def test_nothing_to_save_writes_no_row(machine, no_side_effects):
    assert rl.save()["changed"] is False and not no_side_effects


def test_a_waiting_job_starts_after_the_ceiling_is_raised(machine, no_side_effects):
    memory = ps.MemoryReading(total_mb=32 * 1024, available_mb=28 * 1024)
    slots = ps.ProfileSlots(memory=lambda: memory, tick_s=0.02,
                            usage=ps.ProfileUsage(lambda profiles: {}, background=False),
                            open_probe=ps.OpenProfileProbe(lambda profiles: set(), background=False))
    rl.save(1, None, None)
    assert slots.acquire("j1", "p1") is not None
    got = {}
    t = threading.Thread(target=lambda: got.setdefault("r", slots.acquire("j2", "p2")), daemon=True)
    t.start()
    time.sleep(0.2)
    assert "r" not in got and slots.snapshot()["next"]["reason"] == "ceiling"
    rl.save(3, None, None)
    slots.wake()
    t.join(3)
    assert got.get("r") is not None
    slots.release("j1")
    slots.release("j2")


def test_lowering_the_ceiling_below_running_stops_nobody(machine, no_side_effects):
    memory = ps.MemoryReading(total_mb=32 * 1024, available_mb=28 * 1024)
    slots = ps.ProfileSlots(memory=lambda: memory, tick_s=0.02,
                            usage=ps.ProfileUsage(lambda profiles: {}, background=False),
                            open_probe=ps.OpenProfileProbe(lambda profiles: set(), background=False))
    rl.save(3, None, None)
    for i in range(3):
        assert slots.acquire(f"j{i}", f"p{i}") is not None
    rl.save(1, None, None)
    snap = slots.snapshot()
    assert len(snap["running"]) == 3 and snap["next"]["allow"] is False and snap["next"]["reason"] == "ceiling"
    assert snap["next"]["running"] == 3 and snap["next"]["cap"] == 1


def _post(body):
    from src.web import app as web

    class Req:
        async def json(self):
            if isinstance(body, Exception):
                raise body
            return body
    return asyncio.run(web.api_save_run_limits(Req()))


def test_api_roundtrip_and_validation(machine, no_side_effects):
    from src.web import app as web
    ok = _post({"ceiling": 4, "memory_max_used_pct": 85})
    data = json.loads(ok.body)
    assert ok.status_code == 200 and data["ceiling"] == 4 and data["memory_max_used_pct"] == 85
    got = json.loads(web.api_run_limits().body)
    assert got["ok"] and got["ceiling"] == 4 and got["bounds"]["ceiling"] == [1, 8]
    assert got["defaults"]["memory_max_used_pct"] == 92
    for bad in ({"ceiling": 99}, {"colour": 1}, {"ceiling": True}, {"reset": "yes"}, [1], ValueError("x")):
        assert _post(bad).status_code == 400
    assert json.loads(web.api_run_limits().body)["ceiling"] == 4
    assert json.loads(_post({"reset": True}).body)["ceiling"] == 2


def test_warnings_for_a_small_machine():
    assert rl.warnings_for({"ceiling": 5, "memory_max_used_pct": 85}, 16 * 1024)
    assert not rl.warnings_for({"ceiling": 5, "memory_max_used_pct": 70}, 64 * 1024)
    assert not rl.warnings_for({"ceiling": 3, "memory_max_used_pct": 70}, 16 * 1024)


def test_heavy_limit_is_labelled_as_a_limit_not_an_account(machine, no_side_effects):
    memory = ps.MemoryReading(total_mb=32 * 1024, available_mb=28 * 1024)
    gate = warm.HeavyGate(limit=lambda: 2, memory=lambda: memory, settings=ps.load_settings,
                          usage=ps.ProfileUsage(lambda profiles: {}, background=False))
    gate.acquire("a")
    gate.acquire("b")
    view = gate.wait_view("limit")
    assert view["hold"] == "heavy_limit" and view["heavy_limit"] == 2 and view["running"] == 2
    assert gate.wait_view("memory_ok")["hold"] == "account_busy"
    from src.web import publish_jobs as pj
    queue = pj._queue_view({"reason": view["hold"], **{k: v for k, v in view.items() if k != "hold"}})
    assert queue["state"] == "waiting_account" and queue["hold"] == "heavy_limit" and queue["heavy_limit"] == 2


def test_the_ceiling_wait_text_and_the_settings_block_are_on_the_page():
    html = (ROOT / "src/web/templates/report.html").read_text(encoding="utf-8")
    assert "function cbCeilingWait(" in html
    assert "profiles in use (ceiling " in html and "ceiling is now " in html
    assert html.count("cbCeilingWait(") >= 4
    assert "heavy actions in progress (limit " in html
    for marker in ('id="rl-box"', 'id="rl-ceiling"', 'id="rl-heavy"', 'id="rl-share"', 'id="rl-reset"',
                   "/api/run-limits"):
        assert marker in html
    # slot_decision несёт различие «потолок / память» для разбора.
    src = (ROOT / "src/web/publish_jobs.py").read_text(encoding="utf-8")
    assert '"ceiling_reached"' in src and '"effective_limit"' in src
