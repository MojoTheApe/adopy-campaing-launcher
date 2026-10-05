# -*- coding: utf-8 -*-
"""Профиль берёт 3,5 ГБ на старте, дальше — сколько реально занимает (ACL-1424).

2026-10-01: четыре профиля Mimic заняли 13,6 ГБ (≈3,4 ГБ каждый), ноутбук —
87 % памяти, а очередь (ACL-1321) считала по 1,5 ГБ на профиль. Владелец:
«начинать с 3,5 и подстраиваться под свободное место». Здесь — оценка на
старте, замер идущих профилей (подставной), ожидание при нехватке памяти и
старт, когда она освободилась, откат к оценке при сбое замера, пол замера,
потолок/пол числа профилей и то, как это видит задание публикации.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.web import profile_slots as ps  # noqa: E402

GB = 1024
# До ACL-1603: потолок 4, запас 1,5 ГБ, пол замера 1,5 ГБ. Механику (замер,
# рост, откат к оценке) эти тесты проверяют на прежних числах; новые
# умолчания (2 / 3 ГБ / 3,5 ГБ) — в test_acl1603_stable_slots.py.
OLD = dict(ceiling=4, reserve_mb=int(1.5 * GB), profile_floor_mb=int(1.5 * GB))


def _S(**kw):
    return ps.SlotSettings(**{**OLD, **kw})


def mem(available_gb: float, total_gb: float = 32) -> ps.MemoryReading:
    return ps.MemoryReading(total_mb=int(total_gb * GB), available_mb=int(available_gb * GB))


# ── правило ──────────────────────────────────────────────────────────────

def test_a_run_starts_assuming_three_and_a_half_gb_per_profile():
    assert ps.DEFAULT_PROFILE_MEMORY_GB == 3.5
    s = ps.load_settings({})
    assert s.profile_mb == int(3.5 * GB)
    d = ps.decide(1, 0, mem(6.4), s)            # 3,5 + 3 запас = 6,5 (ACL-1603)
    assert d["allow"] is False and d["reason"] == "memory_low"
    assert d["profile_mb"] == int(3.5 * GB) and d["profile_mb_source"] == "estimate"
    assert ps.decide(1, 0, mem(6.5), s)["allow"] is True


def test_the_measured_average_replaces_the_estimate():
    s = _S()
    d = ps.decide(1, 0, mem(4.5), s, measured_mb=int(2.4 * GB))   # 2,4 + 1,5 = 3,9
    assert d["allow"] is True and d["profile_mb_source"] == "measured"
    assert d["profile_mb"] == int(2.4 * GB) and d["need_mb"] == int(2.4 * GB) + s.reserve_mb
    heavy = ps.decide(1, 0, mem(4.5), s, measured_mb=int(4.2 * GB))
    assert heavy["allow"] is False and heavy["profile_mb"] == int(4.2 * GB)


def test_a_tiny_measurement_never_goes_below_the_floor():
    s = ps.SlotSettings()
    assert s.profile_floor_mb == int(3.5 * GB)   # ACL-1603
    assert s.profile_floor_mb == int(ps.DEFAULT_PROFILE_FLOOR_GB * GB)
    d = ps.decide(1, 0, mem(20), s, measured_mb=200)
    assert d["profile_mb"] == s.profile_floor_mb and d["measured_mb"] == 200


def test_ceiling_and_floor_still_hold_with_measurements():
    s = _S()
    first = ps.decide(0, 0, mem(20), s, measured_mb=None)
    assert first["allow"] and first["reason"] == "memory_ok"
    capped = ps.decide(4, 0, mem(60), s, measured_mb=int(1.6 * GB))
    assert capped["allow"] is False and capped["reason"] == "ceiling"


def test_the_floor_setting_is_read_from_the_machine_env():
    got = ps.load_settings({"ACL_PROFILE_MEMORY_FLOOR_GB": "2"})
    assert got.profile_floor_mb == 2 * GB


# ── замер процессов (подставные процессы) ────────────────────────────────

class FakeProc:
    def __init__(self, pid, cmdline, rss_mb, children=(), exe=r"C:\Users\u\mlx\deps\mimic_153.2\chrome.exe",  # linux-ok: a Windows path as opaque data, never touched on disk
                 name="chrome.exe"):
        self.pid, self._cmd, self._rss, self._children = pid, cmdline, rss_mb, list(children)
        self.info = {"name": name, "exe": exe, "cmdline": cmdline}

    def cmdline(self):
        return self._cmd

    def memory_info(self):
        return type("M", (), {"rss": self._rss * 2**20})()

    def children(self, recursive=False):
        return list(self._children)


def test_a_profile_tree_is_found_by_its_id_and_never_counts_other_chrome():
    kid = FakeProc(2, ["chrome.exe", "--type=renderer"], 900)
    root = FakeProc(1, ["chrome.exe", "--client-session-id=AAAA-1111", "--user-data-dir=X"], 600, [kid])
    other = FakeProc(3, ["chrome.exe", "--client-session-id=BBBB-2222"], 3000)
    own_chrome = FakeProc(4, ["chrome.exe", "--client-session-id=aaaa-1111"], 5000,
                          exe=r"C:\Program Files\Google\Chrome\Application\chrome.exe")  # linux-ok: a Windows path as opaque data, never touched on disk
    got = ps.measure_profiles(["aaaa-1111", "CCCC-3333"], processes=lambda: [root, other, own_chrome, kid])
    assert got == {"aaaa-1111": 1500}


def test_the_real_probe_runs_on_this_machine_without_error():
    """Только чтение: процессы не трогаем. Профиля с таким id нет — пусто."""
    assert ps.measure_profiles(["no-such-profile-acl-1424"]) == {}


# ── очередь: замер идущих профилей ───────────────────────────────────────

class Machine:
    """Подставная машина: свободная память, часы и замер профилей — от теста."""

    def __init__(self, available_gb, usage=None, settings=None):
        self.available_gb = available_gb
        self.now = 1000.0
        self.usage = usage or {}          # профиль → МБ; исключение — сбой замера
        # settle_s=60: здесь «прогрелся» и «устоялся» совпадают; молодой профиль — свой тест.
        self.settings = settings or _S(warmup_s=60, settle_s=60)
        self.asked: list[list[str]] = []

    def memory(self):
        # ACL-1603: первый профиль тоже ждёт запаса, поэтому пока ничего не
        # идёт, у подставной машины памяти вдоволь — тесты ниже проверяют
        # решение о СЛЕДУЮЩЕМ профиле при заданной памяти.
        if self._queue is not None and not self._queue._running:
            return mem(max(self.available_gb, 64))
        return mem(self.available_gb)

    _queue = None

    def measure(self, profiles):
        self.asked.append(sorted(profiles))
        if isinstance(self.usage, Exception):
            raise self.usage
        return {p: mb for p, mb in self.usage.items() if p in profiles}

    def slots(self, tick_s=0.05):
        probe = ps.ProfileUsage(self.measure, clock=lambda: self.now, background=False)
        self._queue = ps.ProfileSlots(memory=self.memory, settings=lambda: self.settings,
                                      clock=lambda: self.now, tick_s=tick_s, usage=probe)
        return self._queue


class Job:
    def __init__(self, slots, job_id, profile):
        self.id, self.slots = job_id, slots
        self.waits: list[dict] = []
        self.grant = None
        self.done = threading.Event()
        threading.Thread(target=self._run, args=(profile,), daemon=True).start()
        assert _until(lambda: self.done.is_set() or self.waits)

    def _run(self, profile):
        self.grant = self.slots.acquire(self.id, profile, on_wait=self.waits.append)
        self.done.set()

    @property
    def started(self):
        return self.done.is_set() and self.grant is not None

    def finish(self):
        self.slots.release(self.id)


def _until(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return bool(cond())


def test_until_a_profile_has_settled_the_estimate_decides():
    m = Machine(available_gb=4.5, usage={"P1": int(1.0 * GB)})
    slots = m.slots()
    a = Job(slots, "a", "P1")
    assert a.started
    b = Job(slots, "b", "P2")
    time.sleep(0.15)
    assert not b.started
    hold = b.waits[0]
    # Первый профиль ещё греется: его 3,5 ГБ вычитаем сами, замер не спрашиваем.
    assert hold["reason"] == "memory_low" and hold["profile_mb_source"] == "estimate"
    assert m.asked == []
    a.finish()
    b.finish()


def test_the_measured_profiles_let_the_next_one_start_and_low_memory_holds_it():
    m = Machine(available_gb=4.0, usage={"P1": int(2.0 * GB), "P2": int(2.4 * GB)})
    slots = m.slots()
    a = Job(slots, "a", "P1")
    m.now += 61                                     # первый поднялся
    b = Job(slots, "b", "P2")                       # 2,0 + 1,5 ≤ 4,0 — стартует
    assert _until(lambda: b.started)
    assert b.grant["decision"]["profile_mb_source"] == "measured"
    assert b.grant["decision"]["measured_mb"] == int(2.0 * GB)
    m.now += 61
    m.available_gb = 2.1                            # человек открыл тяжёлое
    c = Job(slots, "c", "P3")
    time.sleep(0.15)
    assert not c.started
    hold = c.waits[-1]
    assert hold["reason"] == "memory_low" and hold["measured_mb"] == int(2.4 * GB)   # ACL-1603: максимум, не среднее
    assert hold["available_mb"] == int(2.1 * GB)
    m.available_gb = 6.0                            # закрыл — место есть
    m.now += 10                                     # замер устарел, перечитан
    assert _until(lambda: c.started), "память освободилась, а профиль не стартовал"
    for j in (a, b, c):
        j.finish()


def test_a_failed_measurement_falls_back_to_the_estimate():
    m = Machine(available_gb=4.9, usage=RuntimeError("access denied"))
    slots = m.slots()
    a = Job(slots, "a", "P1")
    m.now += 61
    b = Job(slots, "b", "P2")
    time.sleep(0.15)
    assert not b.started
    hold = b.waits[0]
    assert hold["profile_mb_source"] == "estimate" and hold["measure"] == "failed"
    assert hold["profile_mb"] == int(3.5 * GB)
    m.available_gb = 5.0
    assert _until(lambda: b.started)
    a.finish()
    b.finish()


def test_a_profile_not_found_by_the_probe_keeps_the_estimate():
    m = Machine(available_gb=4.0, usage={})
    slots = m.slots()
    a = Job(slots, "a", "P1")
    m.now += 61
    b = Job(slots, "b", "P2")
    time.sleep(0.15)
    assert not b.started and b.waits[0]["profile_mb_source"] == "estimate"
    a.finish()
    b.finish()


def test_the_override_never_measures():
    m = Machine(available_gb=0.5, usage={"P1": 99 * GB}, settings=_S(override=2, warmup_s=0))
    slots = m.slots()
    a = Job(slots, "a", "P1")
    b = Job(slots, "b", "P2")
    assert _until(lambda: a.started and b.started)
    assert m.asked == []
    a.finish()
    b.finish()


# ── замер в своём потоке, с ограничением по времени ──────────────────────

def test_the_background_probe_never_blocks_and_a_hung_probe_falls_back():
    now = [0.0]
    gate = threading.Event()
    calls = []

    def slow(profiles):
        calls.append(profiles)
        gate.wait(5)
        return {"P1": 3000}

    probe = ps.ProfileUsage(slow, clock=lambda: now[0], timeout_s=3.0)
    t0 = time.monotonic()
    first = probe.read(["P1"])
    assert time.monotonic() - t0 < 0.5, "замер держал решение"
    assert first["status"] == "pending" and first.get("per_profile") is None
    now[0] = 4.0
    assert probe.read(["P1"])["status"] == "timeout"
    assert len(calls) == 1, "зависший замер не запускаем второй раз поверх"
    gate.set()
    assert _until(lambda: probe.read(["P1"])["status"] == "ok")
    assert probe.read(["P1"])["per_profile"] == {"P1": 3000}


def test_a_finished_background_probe_wakes_the_queue():
    woke = threading.Event()
    probe = ps.ProfileUsage(lambda profiles: {"P1": 3000}, clock=lambda: 0.0, on_ready=woke.set)
    probe.read(["P1"])
    assert woke.wait(2)


# ── как это видит задание публикации ─────────────────────────────────────

def test_the_waiting_job_carries_what_the_dashboard_shows():
    from src.web import publish_jobs as pj

    queue = pj._queue_view({"reason": "memory_low", "ahead": 0, "limit": 2, "running": 2,
                            "available_mb": int(2.1 * GB), "profile_mb": int(3.4 * GB),
                            "profile_mb_source": "measured"})
    assert queue["reason"] == "slot" and queue["hold"] == "memory_low"
    assert queue["available_mb"] == int(2.1 * GB) and queue["profile_mb"] == int(3.4 * GB)
    assert pj._queue_view({"reason": "profile", "ahead": 1})["reason"] == "profile"


def test_each_hold_decision_goes_to_telemetry(monkeypatch):
    from src.fb_ads_rpa import run_telemetry as rt
    from src.web import publish_jobs as pj

    rows = []
    monkeypatch.setattr(rt, "record", lambda step, ok, **kw: rows.append((step, ok, kw)))
    pj._record_slot_decision(rt, "CAMP_A", "Doc19", {
        "allow": False, "reason": "memory_low", "available_mb": 2150, "profile_mb": 3480,
        "measured_mb": 3480, "profile_mb_source": "measured", "per_profile_mb": [3480],
        "leak": {"550e8400-e29b-41d4-a716-446655440000": 3480}})
    step, ok, kw = rows[0]
    assert step == "job_phase" and ok is True and kw["note"] == "slot_decision"
    assert kw["campaign"] == "CAMP_A" and kw["diag"]["account"] == "Doc19"
    assert kw["diag"]["decision"]["reason"] == "memory_low"
    assert kw["diag"]["decision"]["measured_mb"] == 3480


# ── ревью PR #425: никогда не перегружать, без id в телеметрии ──────────

def test_the_growth_of_running_profiles_is_reserved():
    s = _S()
    # Средняя 3,4; один профиль сейчас 2,0 — ему ещё расти на 1,4 ГБ.
    d = ps.decide(2, 0, mem(6.0), s, measured_mb=int(3.4 * GB),
                  current_mb=[int(2.0 * GB), int(3.4 * GB)])
    assert d["growth_mb"] == int(3.4 * GB) - int(2.0 * GB)
    assert d["need_mb"] == int(3.4 * GB) + s.reserve_mb + d["growth_mb"]
    assert d["allow"] is False and d["reason"] == "memory_low"
    # Без роста (все уже на средней) те же 6 ГБ пускают.
    flat = ps.decide(2, 0, mem(6.0), s, measured_mb=int(3.4 * GB),
                     current_mb=[int(3.4 * GB), int(3.4 * GB)])
    assert flat["growth_mb"] == 0 and flat["allow"] is True


def test_a_young_profile_measured_small_does_not_lower_the_estimate():
    m = Machine(available_gb=5.5, usage={"P1": int(1.0 * GB)},
                settings=_S(warmup_s=60, settle_s=300))
    slots = m.slots()
    a = Job(slots, "a", "P1")
    m.now += 61                                     # прогрелся, но кампании ещё не открыл
    b = Job(slots, "b", "P2")
    time.sleep(0.15)
    assert not b.started
    nxt = slots.snapshot()["next"]
    assert nxt["profile_mb"] == int(3.5 * GB), "молодой профиль опустил оценку"
    assert nxt["growth_mb"] == int(3.5 * GB) - int(1.0 * GB)
    assert nxt["reason"] == "memory_low"
    m.now += 300                                    # устоялся на 1,0 ГБ — теперь верим замеру
    m.available_gb = 5.5
    assert _until(lambda: b.started)
    assert b.grant["decision"]["profile_mb"] == _S().profile_floor_mb
    a.finish()
    b.finish()


def test_a_shrinking_profile_keeps_its_peak():
    m = Machine(available_gb=4.0, usage={"P1": int(3.4 * GB)})
    slots = m.slots()
    a = Job(slots, "a", "P1")
    m.now += 61
    b = Job(slots, "b", "P2")
    time.sleep(0.15)
    assert not b.started
    assert slots.snapshot()["next"]["measured_mb"] == int(3.4 * GB)
    m.usage["P1"] = int(1.0 * GB)                   # закрывается (ACL-1444) или сбросил вкладку
    m.now += 10                                     # замер перечитан
    nxt = slots.snapshot()["next"]
    assert nxt["measured_mb"] == int(3.4 * GB), "сжатие профиля опустило среднее"
    assert nxt["per_profile_mb"] == [int(1.0 * GB)]
    assert not b.started
    a.finish()
    assert _until(lambda: b.started)
    b.finish()


def test_no_profile_id_reaches_the_queue_decision_or_telemetry(monkeypatch):
    import json

    from src.fb_ads_rpa import run_telemetry as rt
    from src.web import publish_jobs as pj

    p1 = "550e8400-e29b-41d4-a716-446655440000"
    p2 = "6ba7b810-9dad-11d1-80b4-00c04fd430c8"
    m = Machine(available_gb=2.0, usage={p1: int(3.0 * GB)})
    slots = m.slots()
    a = Job(slots, "a", p1)
    m.now += 61
    b = Job(slots, "b", p2)
    time.sleep(0.15)
    hold = b.waits[-1]
    assert hold["per_profile_mb"] == [int(3.0 * GB)]
    rows = []
    monkeypatch.setattr(rt, "record", lambda step, ok, **kw: rows.append((step, ok, kw)))
    pj._record_slot_decision(rt, "CAMP_A", "Doc19", hold)
    pj._record_slot_decision(rt, "CAMP_A", "Doc19", {"reason": "memory_low", "leak": {p1: 1}})
    dumped = json.dumps([kw for _, _, kw in rows])
    assert p1 not in dumped and p2 not in dumped
    assert rows[0][2]["diag"]["decision"]["per_profile_mb"] == [int(3.0 * GB)]
    a.finish()
    b.finish()


def test_a_probe_hung_for_too_long_is_replaced_by_a_new_one():
    now = [0.0]
    gates = [threading.Event(), threading.Event()]
    calls = []

    def slow(profiles):
        gate = gates[min(len(calls), 1)]
        calls.append(profiles)
        gate.wait(5)
        return {"P1": 1000 * len(calls)}

    probe = ps.ProfileUsage(slow, clock=lambda: now[0], timeout_s=3.0, restart_factor=4)
    assert probe.read(["P1"])["status"] == "pending"
    now[0] = 4.0
    assert probe.read(["P1"])["status"] == "timeout" and len(calls) == 1
    now[0] = 13.0                                   # дольше 4 × 3 с — бросаем
    probe.read(["P1"])
    assert _until(lambda: len(calls) == 2), "зависший замер навсегда остановил адаптацию"
    gates[0].set()                                  # старый вернулся — новый всё ещё «идёт»
    assert _until(lambda: probe.read(["P1"])["status"] == "ok")
    assert probe._inflight_since is not None
    gates[1].set()
    assert _until(lambda: probe._inflight_since is None)


def test_the_settle_time_is_read_from_the_machine_env():
    assert ps.load_settings({}).settle_s == ps.DEFAULT_SETTLE_S == 300.0
    assert ps.load_settings({"ACL_PROFILE_SETTLE_S": "120"}).settle_s == 120.0
