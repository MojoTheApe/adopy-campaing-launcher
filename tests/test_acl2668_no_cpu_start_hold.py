"""ACL-2668 (решение №13): старт следующего профиля задания больше не ждёт CPU.

Было (ACL-2283 / ACL-2316 / ACL-2328): CPU ниже 70 % 5 с, не дольше 180 с. Теперь старт охраняют только
порог памяти (ACL-2255) и потолок профилей; очередь «по одному, пока предыдущий прогревается» остаётся.

Fakes only (same rig as the warm-session tests): no Meta, no Multilogin, no real CPU or memory reads.
"""
from types import SimpleNamespace

import pytest

from src.fb_api import warm
from tests.test_acl1812_warm_sessions import ACT, PID, Rig
from tests.test_acl2283_requeue_under_load import _mem

B = "b" * 8 + "-0000-4000-8000-000000000000"


def _entry(pid=PID):
    return SimpleNamespace(profile_id=pid, folder_id="F", multilogin_instance="A", ad_account_id=ACT)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ACL_PROFILE_RESTART_COOLDOWN_S", "0")
    monkeypatch.delenv(warm.LOAD_CPU_ENV, raising=False)


def test_cpu_hold_settings_are_gone():
    for name in ("START_CPU_SETTLE_ENV", "DEFAULT_START_CPU_SETTLE_S", "START_MAX_HOLD_ENV",
                 "DEFAULT_START_MAX_HOLD_S"):
        assert not hasattr(warm, name), name


def test_busy_cpu_does_not_delay_the_start(tmp_path):
    rig = Rig(tmp_path)
    reads = []
    rig.reg._cpu = lambda: reads.append(1) or 99.0
    started_at = []
    real = rig.reg._start_profile

    def start(p, f, i):
        started_at.append(rig.now[0])
        return real(p, f, i)

    rig.reg._start_profile = start
    t0 = rig.now[0]
    views = []
    with warm.job_wait(lambda: False, views.append):
        assert rig.reg.open_one("A", _entry())["ok"]
    assert started_at and started_at[0] - t0 < 2.0, started_at[0] - t0
    assert not [v for v in views if v and v.get("hold") == "machine_busy"]
    assert views[-1] is None if views else True


def test_above_92_percent_memory_the_start_still_waits(tmp_path):
    rig = Rig(tmp_path, reg_memory=lambda: _mem(95))
    rig.reg._cpu = lambda: 10.0
    views = []
    with warm.job_wait(lambda: len(rig.sleeps) > 400, views.append):
        res = rig.reg.open_one("A", _entry())
    assert res["ok"] is False and not rig.started
    assert all(v is None or v.get("hold") == "memory_low" for v in views)


def test_a_profile_still_warming_up_keeps_the_turn(tmp_path):
    rig = Rig(tmp_path)
    rig.reg._cpu = lambda: 10.0
    rig.reg._starting[PID] = rig.reg._mono()
    views = []
    with warm.job_wait(lambda: len(rig.sleeps) > 60, views.append):
        res = rig.reg.open_one("B", _entry(B))
    assert res["ok"] is False and not rig.started
    held = [v for v in views if v and v.get("hold") == "machine_busy"]
    assert held and held[0]["warming"] == 1 and "cpu_pct" not in held[0] and "max_hold_s" not in held[0]


def test_tab_patience_keeps_the_70_percent_threshold(tmp_path, monkeypatch):
    rig = Rig(tmp_path)
    rig.reg._cpu = lambda: 75.0
    snap = rig.reg.load_snapshot()
    assert snap["cpu_level"] == 70.0 and snap["busy"] is True and "cpu_high" in snap["reasons"]
    rig.reg._cpu = lambda: 69.0
    assert rig.reg.load_snapshot()["busy"] is False
    monkeypatch.setenv(warm.LOAD_CPU_ENV, "60")
    assert rig.reg.load_snapshot()["cpu_level"] == 60.0


def test_load_wait_text_no_longer_promises_a_cpu_start_level():
    from pathlib import Path
    html = (Path(__file__).resolve().parents[1] / "src" / "web" / "templates" / "report.html").read_text(
        encoding="utf-8")
    body = html.split("function cbLoadWait(q)", 1)[1].split("\n}", 1)[0]
    assert "starts below" not in body
