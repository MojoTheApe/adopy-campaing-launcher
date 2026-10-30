# -*- coding: utf-8 -*-
"""ACL-2255: сколько профилей разом решает ДОЛЯ занятой памяти, а не «N ГБ на профиль».

Ноутбук владельца, 31 ГБ: свободно ~7 ГБ, потолок 5, а очередь пускала двух — оценка
3,5 ГБ на профиль + запас 3 ГБ просили 6,5 ГБ на каждого следующего, хотя профили
меряются в 0,6–1,9 ГБ. Решение владельца (28.10): профиль можно стартовать, пока занято
не больше ~90 % всей памяти (`ACL_MEMORY_MAX_USED_PCT`), 86–87 % занятых — старт обязан быть.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_api import warm  # noqa: E402
from src.web import profile_slots as ps  # noqa: E402
from src.web import publish_jobs as pj  # noqa: E402

GB = 1024


def reading(total_gb: float, used_pct: float) -> ps.MemoryReading:
    total = int(total_gb * GB)
    return ps.MemoryReading(total_mb=total, available_mb=int(total * (100 - used_pct) / 100))


def settings(**kw) -> ps.SlotSettings:
    # ACL-2345: the product default is 75 %; this file pins the 90 % the rule was written for
    return ps.SlotSettings(**{"ceiling": 5, "max_used_pct": 90.0, **kw})


@pytest.mark.parametrize("used,allowed", [(50, True), (86, True), (87, True), (89, True), (95, False)])
def test_a_start_depends_on_the_share_of_memory_in_use(used, allowed):
    d = ps.decide(3, 0, reading(31, used), settings())
    assert d["allow"] is allowed
    assert d["reason"] == ("memory_ok" if allowed else "below_reserve")
    assert d["max_used_pct"] == 90.0 and d["used_pct"] == pytest.approx(used, abs=0.2)


def test_exactly_ninety_percent_in_use_still_allows_and_just_over_does_not():
    total = 31 * GB
    keep = ps.keep_free_mb(total, settings())
    assert keep == int(total * 0.10)
    assert ps.decide(1, 0, ps.MemoryReading(total, keep), settings())["allow"] is True
    assert ps.decide(1, 0, ps.MemoryReading(total, keep - 1), settings())["allow"] is False


def test_the_owner_laptop_runs_five_at_once_with_seven_gb_free():
    """31 ГБ, ~7 ГБ свободно (77 % занято), потолок 5: раньше упиралось в 2 (6,5 ГБ на следующего)."""
    mem = ps.MemoryReading(31 * GB, 7 * GB)
    for running in (1, 2, 3, 4):
        d = ps.decide(running, 0, mem, settings())
        assert d["allow"] is True and d["reason"] == "memory_ok", running
    assert ps.decide(5, 0, mem, settings())["reason"] == "ceiling"


def test_the_cap_of_two_does_not_come_from_an_estimate_of_gigabytes_per_profile():
    """Идущие профили уже в доле занятой памяти: ни оценка 3,5 ГБ, ни пол замера, ни «рост» не вычитаются."""
    mem = reading(31, 80)
    s = settings()
    d = ps.decide(4, 0, mem, s, measured_mb=500, current_mb=[500, 500, 500, 500])
    assert d["allow"] is True and "growth_mb" not in d
    assert d["need_mb"] == ps.keep_free_mb(31 * GB, s)


def test_the_ceiling_is_respected_whatever_the_memory():
    assert ps.decide(5, 0, reading(64, 10), settings())["reason"] == "ceiling"
    assert ps.decide(2, 0, reading(64, 10), ps.SlotSettings())["reason"] == "ceiling"   # умолчание потолка — 2


def test_profiles_that_just_started_are_charged_until_they_have_taken_their_memory():
    mem = reading(31, 70)                       # свободно ~9,3 ГБ, держим 3,1
    s = settings()
    assert ps.decide(2, 0, mem, s)["allow"] is True
    assert ps.decide(2, 1, mem, s)["allow"] is True                # 3,1 + 3,5 = 6,6 <= 9,3
    two = ps.decide(2, 2, mem, s)                                  # 3,1 + 7,0 = 10,1 > 9,3
    assert two["allow"] is False and two["reason"] == "memory_low"
    assert two["need_mb"] == ps.keep_free_mb(31 * GB, s) + 2 * s.profile_mb


def test_a_measured_profile_is_charged_by_its_measurement_while_warming():
    mem = reading(31, 80)
    light = ps.decide(2, 1, mem, settings(profile_floor_mb=GB), measured_mb=GB)
    assert light["profile_mb_source"] == "measured" and light["profile_mb"] == GB and light["allow"] is True


@pytest.mark.parametrize("total_gb,used,allowed", [
    (8, 80, True),      # 1,6 ГБ свободно
    (8, 87, True),      # 1,04 ГБ свободно — не меньше минимума в 1 ГБ
    (8, 89, False),     # 0,88 ГБ: доля ещё не 90 %, но свободного меньше минимума
    (4, 75, True),      # 1 ГБ свободно
    (4, 80, False),
    (16, 90, True),
    (16, 91, False),
])
def test_a_laptop_with_little_memory_always_keeps_one_gigabyte_free(total_gb, used, allowed):
    assert ps.decide(1, 0, reading(total_gb, used), settings())["allow"] is allowed


def test_the_setting_is_read_from_the_machine_env_and_garbage_falls_back():
    assert ps.load_settings({}).max_used_pct == 75.0
    assert ps.load_settings({"ACL_MEMORY_MAX_USED_PCT": "85"}).max_used_pct == 85.0
    assert ps.load_settings({"ACL_MEMORY_MAX_USED_PCT": "92,5"}).max_used_pct == 92.5
    assert ps.load_settings({"ACL_MEMORY_MAX_USED_PCT": "lots"}).max_used_pct == 75.0
    assert ps.load_settings({"ACL_MEMORY_MAX_USED_PCT": "5"}).max_used_pct == 75.0     # ниже 10 — мусор
    assert ps.load_settings({"ACL_MEMORY_MAX_USED_PCT": "150"}).max_used_pct == 99.0


def test_a_stricter_share_blocks_where_the_default_allows():
    mem = reading(31, 86)
    assert ps.decide(1, 0, mem, settings())["allow"] is True
    assert ps.decide(1, 0, mem, settings(max_used_pct=80.0))["allow"] is False


def test_unknown_memory_and_the_open_profile_path_are_unchanged():
    assert ps.decide(1, 0, None, settings())["reason"] == "memory_unknown"
    assert ps.decide(2, 0, None, settings())["reason"] == "memory_unknown_full"
    opened = ps.decide(3, 0, reading(31, 97), settings(), profile_open=True)
    assert opened["allow"] is True and opened["reason"] == "profile_open"


def test_the_limit_shown_follows_the_free_share():
    d = ps.decide(1, 0, ps.MemoryReading(31 * GB, 12 * GB), settings(profile_mb=GB))
    assert d["limit"] == 5            # (12 - 3,1) / 1 = 8 ещё, потолок 5


def test_the_queue_view_tells_the_share_for_the_dashboard():
    d = ps.decide(2, 0, reading(31, 95), settings())
    view = pj._queue_view(d)
    assert view["state"] == "waiting_memory" and view["hold"] == "below_reserve"
    assert view["used_pct"] == pytest.approx(95, abs=0.2) and view["max_used_pct"] == 90.0
    html = (ROOT / "src" / "web" / "templates" / "report.html").read_text(encoding="utf-8")
    assert "% in use, a new profile starts below " in html


# ── очередь целиком ──────────────────────────────────────────────────────

def _until(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return bool(cond())


def _queue(available_mb, total_mb=31 * GB, **kw):
    now = [1000.0]
    s = settings(warmup_s=0, **kw)
    return ps.ProfileSlots(memory=lambda: ps.MemoryReading(total_mb, available_mb()), settings=lambda: s,
                           clock=lambda: now[0], tick_s=0.02,
                           usage=ps.ProfileUsage(lambda p: {}, clock=lambda: now[0], background=False))


def test_five_jobs_run_at_once_on_a_31_gb_laptop_with_a_ceiling_of_five():
    q = _queue(lambda: 7 * GB)
    grants: dict = {}

    def run(i):
        grants[f"j{i}"] = q.acquire(f"j{i}", f"P{i}")

    threads = [threading.Thread(target=run, args=(i,), daemon=True) for i in range(6)]
    for t in threads:
        t.start()
    assert _until(lambda: sum(1 for g in grants.values() if g) == 5)
    time.sleep(0.2)
    assert len(grants) == 5, "the sixth job must wait for the ceiling"
    held = q.snapshot()["next"]
    assert held["allow"] is False and held["reason"] == "ceiling"
    for i in range(5):
        q.release(f"j{i}")
    assert _until(lambda: len(grants) == 6)
    q.release("j5")


def test_jobs_wait_when_memory_in_use_goes_above_the_share_and_start_when_it_drops():
    free = [7 * GB]
    q = _queue(lambda: free[0])
    assert q.acquire("a", "P1") is not None
    free[0] = int(1.5 * GB)                       # занято 95 %
    got: list = []
    t = threading.Thread(target=lambda: got.append(q.acquire("b", "P2")), daemon=True)
    t.start()
    time.sleep(0.2)
    assert got == []
    free[0] = 4 * GB                              # 87 % - старт обязан быть
    assert _until(lambda: len(got) == 1 and got[0] is not None)
    q.release("a")
    q.release("b")


# ── ворота тяжёлых действий и прогрев профиля (warm.py) ──────────────────

def _gate(mem: ps.MemoryReading, active=1, limit=3):
    gate = warm.HeavyGate(limit=lambda: limit, memory=lambda: mem, settings=lambda: settings(), usage=None)
    for i in range(active):
        gate._active[f"p{i}"] = 1
    return gate


@pytest.mark.parametrize("used,ok", [(50, True), (86, True), (89, True), (95, False)])
def test_the_heavy_gate_follows_the_share_not_a_gigabyte_estimate(used, ok):
    allowed, why = _gate(reading(31, used))._allowed()
    assert allowed is ok and why == ("memory_ok" if ok else "memory_low")


def test_the_heavy_gate_first_action_waits_only_above_the_share():
    assert _gate(reading(31, 86), active=0)._allowed() == (True, "first")
    assert _gate(reading(31, 95), active=0)._allowed() == (False, "below_reserve")


def test_the_heavy_gate_wait_view_carries_the_share():
    view = _gate(reading(31, 95)).wait_view("memory_low")
    assert view["hold"] == "memory_low" and view["max_used_pct"] == 90.0
    assert view["used_pct"] == pytest.approx(95, abs=0.2)
    assert view["need_mb"] == ps.keep_free_mb(31 * GB, settings())


def test_the_warm_start_needs_only_the_free_share_plus_profiles_with_no_measurement():
    s = settings()
    reg = warm.WarmRegistry.__new__(warm.WarmRegistry)
    reg._memory = lambda: ps.MemoryReading(31 * GB, 7 * GB)
    reg._settings = lambda: s
    reg._mono = lambda: 1000.0
    reg._lock = threading.RLock()
    reg._sessions = {"p1": object(), "p2": object()}
    reg._settling = {"p1": 990.0, "p2": 990.0}
    reg._usage = ps.ProfileUsage(lambda profiles: {"p1": GB}, background=False, clock=lambda: 1000.0)
    need, growth = reg._memory_need(s, ps)
    assert growth == s.profile_mb                      # p2 ещё никто не измерил; p1 уже в доле занятой
    assert need == ps.keep_free_mb(31 * GB, s) + s.profile_mb
