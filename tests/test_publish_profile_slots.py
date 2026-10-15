# -*- coding: utf-8 -*-
"""Сколько профилей публикуют разом — решает память машины (ACL-1321).

Владелец 26.09: «Publish all» с шестью аккаунтами на ноутбуке, где три
профиля Mimic уже съедают 85–89 % памяти, должен идти столько профилей
разом, сколько машина тянет, а остальные — ждать и стартовать сами.
Пол — один, потолок есть, на машине число можно задать руками.

Здесь — правило (`decide`), настройки из `.env`, очередь `ProfileSlots`
с подставной памятью и подставными заданиями и то, как её видит
`publish_jobs`: статус «queued», место в очереди, отмена ждущего, строка
`job_phase: slot_queue` в телеметрии и ответ `/publish-jobs/slots`.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.web import profile_slots as ps  # noqa: E402
from src.web import publish_jobs as pj  # noqa: E402

GB = 1024
# Правило очереди считается здесь при профиле 1,5 ГБ (так было до ACL-1424);
# оценка 3,5 ГБ и замер профилей — test_profile_slots_adaptive_memory.py.
OLD_PROFILE_MB = int(1.5 * GB)
# До ACL-1603 потолок был 4, запас 1,5 ГБ: механику очереди проверяем на них,
# новые умолчания (2 / 3 ГБ / 3,5 ГБ) — в test_acl1603_stable_slots.py.
OLD = dict(ceiling=4, reserve_mb=int(1.5 * GB), profile_floor_mb=int(1.5 * GB))
S = ps.SlotSettings(profile_mb=OLD_PROFILE_MB, **OLD)   # потолок 4, профиль 1,5 ГБ, запас 1,5 ГБ


def mem(available_gb: float, total_gb: float = 16) -> ps.MemoryReading:
    return ps.MemoryReading(total_mb=int(total_gb * GB), available_mb=int(available_gb * GB))


# ── правило ──────────────────────────────────────────────────────────────

def test_the_first_profile_waits_for_the_reserve_too():
    """ACL-1603 (ревью F2): «пол в один профиль» больше не пускает при любой памяти."""
    low = ps.decide(0, 0, mem(0.2), S)
    assert low["allow"] is False and low["reason"] == "below_reserve"
    ok = ps.decide(0, 0, mem(3.0), S)           # 1,5 + 1,5
    assert ok["allow"] and ok["reason"] == "memory_ok"


def test_another_profile_starts_only_with_room_for_it_and_the_reserve():
    assert ps.decide(1, 0, mem(3.0), S)["allow"] is True          # 1,5 + 1,5
    low = ps.decide(1, 0, mem(2.9), S)
    assert low["allow"] is False and low["reason"] == "memory_low"
    assert low["need_mb"] == 3 * GB and low["available_mb"] == int(2.9 * GB)


def test_a_profile_that_just_started_is_counted_before_it_takes_its_memory():
    """Шесть заданий в одну секунду видят одну и ту же свободную память."""
    assert ps.decide(1, 0, mem(3.5), S)["allow"] is True
    warming = ps.decide(1, 1, mem(3.5), S)
    assert warming["allow"] is False and warming["need_mb"] == int(4.5 * GB)


def test_the_ceiling_holds_even_on_a_big_machine():
    d = ps.decide(4, 0, mem(60), S)
    assert d["allow"] is False and d["reason"] == "ceiling"
    assert ps.decide(3, 0, mem(60), S)["limit"] == 4


def test_the_machine_setting_overrides_memory_both_ways():
    fixed = ps.SlotSettings(override=2)
    assert ps.decide(1, 1, mem(0.5), fixed)["allow"] is True       # памяти мало — всё равно два
    full = ps.decide(2, 0, mem(60), fixed)
    assert full["allow"] is False and full["reason"] == "override_full"
    assert ps.decide(5, 0, mem(60), ps.SlotSettings(override=6))["allow"] is True   # выше потолка


def test_unknown_memory_runs_two_at_most():
    assert ps.decide(1, 0, None, S)["allow"] is True
    d = ps.decide(2, 0, None, S)
    assert d["allow"] is False and d["reason"] == "memory_unknown_full"


def test_the_limit_shown_is_what_memory_allows_now():
    assert ps.decide(1, 0, mem(6.1), S)["limit"] == 4     # по памяти 1 + 3, потолок 4
    assert ps.decide(1, 0, mem(4.5), S)["limit"] == 3     # (4,5 − 1,5) / 1,5 = 2 ещё
    assert ps.decide(1, 0, mem(1.0), S)["limit"] == 1


# ── настройки машины ─────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("", ps.DEFAULT_CEILING), ("auto", ps.DEFAULT_CEILING), ("0", ps.DEFAULT_CEILING),
    ("3", 3), (" 2 ", 2), ("-4", 1), ("many", ps.DEFAULT_CEILING),
])
def test_the_legacy_manual_number_is_now_only_the_cap(raw, expected):
    """ACL-1603: ручное число больше не отменяет память — это потолок."""
    got = ps.load_settings({"ACL_MAX_PARALLEL_PROFILES": raw})
    assert got.override is None and got.ceiling == expected


def test_tuning_values_are_read_and_garbage_falls_back_to_defaults():
    got = ps.load_settings({"ACL_MAX_PARALLEL_CEILING": "6", "ACL_PROFILE_MEMORY_GB": "2,5",
                            "ACL_MEMORY_RESERVE_GB": "1", "ACL_PROFILE_WARMUP_S": "30"})
    assert (got.ceiling, got.profile_mb, got.reserve_mb, got.warmup_s) == (6, int(2.5 * GB), GB, 30.0)
    bad = ps.load_settings({"ACL_MAX_PARALLEL_CEILING": "0", "ACL_PROFILE_MEMORY_GB": "lots"})
    assert (bad.ceiling, bad.profile_mb) == (ps.DEFAULT_CEILING, int(ps.DEFAULT_PROFILE_MEMORY_GB * GB))


def test_a_setting_changed_while_running_applies_to_the_next_decision(monkeypatch):
    monkeypatch.setenv("ACL_MAX_PROFILES", "1")
    assert ps.load_settings().ceiling == 1
    monkeypatch.setenv("ACL_MAX_PROFILES", "3")
    assert ps.load_settings().ceiling == 3


def test_the_real_memory_probe_answers_on_this_machine():
    got = ps.read_memory()
    assert got is not None and 0 < got.available_mb <= got.total_mb


# ── очередь с подставной памятью ─────────────────────────────────────────

class Machine:
    """Подставная машина: свободная память и часы задаются тестом."""

    def __init__(self, available_gb: float, settings: ps.SlotSettings | None = None):
        self.available_gb = available_gb
        self.now = 1000.0
        self.settings = settings or ps.SlotSettings(warmup_s=0, profile_mb=OLD_PROFILE_MB, **OLD)

    _queue = None

    def memory(self):
        if self.available_gb is None:
            return None
        # ACL-1603: первый профиль тоже ждёт запаса; пока ничего не идёт, у
        # подставной машины памяти вдоволь (тесты проверяют следующие профили).
        if self._queue is not None and not self._queue._running:
            return mem(max(self.available_gb, 64))
        return mem(self.available_gb)

    def slots(self, tick_s: float = 0.05) -> ps.ProfileSlots:
        # Замер профилей подставной: «не нашли» — решает оценка из настроек.
        probe = ps.ProfileUsage(lambda profiles: {}, clock=lambda: self.now, background=False)
        self._queue = ps.ProfileSlots(memory=self.memory, settings=lambda: self.settings,
                                      clock=lambda: self.now, tick_s=tick_s, usage=probe)
        return self._queue


class Job:
    """Подставное задание: поток, который просит место и помнит, что ему сказали."""

    def __init__(self, slots: ps.ProfileSlots, job_id: str, profile: str):
        self.id, self.slots = job_id, slots
        self.cancel = threading.Event()
        self.waits: list[dict] = []
        self.grant = None
        self.done = threading.Event()
        threading.Thread(target=self._run, args=(profile,), daemon=True).start()
        # Следующее задание встаёт в очередь строго после этого.
        assert _until(lambda: self.done.is_set() or self.id in _waiting(slots))

    def _run(self, profile):
        self.grant = self.slots.acquire(
            self.id, profile, cancelled=self.cancel.is_set, on_wait=self.waits.append)
        self.done.set()

    @property
    def started(self) -> bool:
        return self.done.is_set() and self.grant is not None

    def finish(self):
        self.slots.release(self.id)


def _until(cond, timeout: float = 3.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return bool(cond())


def _waiting(slots) -> list[str]:
    return [w["job_id"] for w in slots.snapshot()["waiting"]]


def _running(slots) -> list[str]:
    return [r["job_id"] for r in slots.snapshot()["running"]]


def test_only_as_many_profiles_start_as_memory_allows_and_the_rest_wait():
    m = Machine(available_gb=4.6)          # хватает на второй профиль, не на третий
    slots = m.slots()
    a = Job(slots, "a", "P1")
    assert _until(lambda: a.started)
    m.available_gb = 3.1                   # первый занял своё
    b = Job(slots, "b", "P2")
    assert _until(lambda: b.started)
    m.available_gb = 1.6
    c = Job(slots, "c", "P3")
    time.sleep(0.2)
    assert not c.started and _running(slots) == ["a", "b"]
    assert c.waits and c.waits[0]["reason"] == "memory_low" and c.waits[0]["ahead"] == 0
    # Конец задания освобождает место — третий стартует сам.
    a.finish()
    m.available_gb = 3.1
    assert _until(lambda: c.started)
    assert c.grant["waited_s"] >= 0 and c.grant["first_hold"]["reason"] == "memory_low"
    b.finish(); c.finish()


def test_freed_memory_starts_the_next_one_without_any_job_finishing():
    m = Machine(available_gb=1.0)
    slots = m.slots()
    a = Job(slots, "a", "P1")
    b = Job(slots, "b", "P2")
    time.sleep(0.2)
    assert a.started and not b.started
    m.available_gb = 8                     # кто-то закрыл тяжёлую программу
    assert _until(lambda: b.started), "очередь не перепроверила память по таймеру"
    a.finish(); b.finish()


def test_the_first_profile_waits_on_a_machine_with_no_free_memory_and_starts_when_it_frees():
    m = Machine(available_gb=0.1)
    slots = ps.ProfileSlots(memory=lambda: mem(m.available_gb), settings=lambda: m.settings,
                            clock=lambda: m.now, tick_s=0.05,
                            usage=ps.ProfileUsage(lambda p: {}, clock=lambda: m.now, background=False))
    a = Job(slots, "a", "P1")
    time.sleep(0.15)
    assert not a.started and a.waits[0]["reason"] == "below_reserve"
    m.available_gb = 8
    assert _until(lambda: a.started)
    a.finish()


def test_the_ceiling_caps_a_machine_with_plenty_of_memory():
    m = Machine(available_gb=64, settings=ps.SlotSettings(ceiling=3, warmup_s=0))
    slots = m.slots()
    jobs = [Job(slots, f"j{i}", f"P{i}") for i in range(5)]
    assert _until(lambda: sum(j.started for j in jobs) == 3)
    time.sleep(0.2)
    assert [j.started for j in jobs] == [True, True, True, False, False]
    assert jobs[3].waits[0]["reason"] == "ceiling"
    for j in jobs:
        j.finish()
    assert _until(lambda: all(j.started for j in jobs))
    for j in jobs:
        j.finish()


def test_the_machine_override_is_obeyed_whatever_the_memory():
    m = Machine(available_gb=0.5, settings=ps.SlotSettings(override=2, warmup_s=0))
    slots = m.slots()
    jobs = [Job(slots, f"j{i}", f"P{i}") for i in range(3)]
    assert _until(lambda: sum(j.started for j in jobs) == 2)
    time.sleep(0.2)
    assert not jobs[2].started and jobs[2].waits[0]["reason"] == "override_full"
    for j in jobs:
        j.finish()


def test_waiting_jobs_start_in_the_order_they_were_sent():
    m = Machine(available_gb=64, settings=ps.SlotSettings(override=1, warmup_s=0))
    slots = m.slots()
    order: list[str] = []
    a = Job(slots, "a", "P1")
    assert _until(lambda: a.started)
    rest = [Job(slots, name, f"P-{name}") for name in ("b", "c", "d", "e")]
    assert [w["ahead"] for w in (j.waits[0] for j in rest)] == [0, 1, 2, 3]
    current = a
    for _ in rest:
        current.finish()
        assert _until(lambda: sum(j.started for j in rest) == len(order) + 1)
        current = next(j for j in rest if j.started and j.id not in order)
        order.append(current.id)
    current.finish()
    assert order == ["b", "c", "d", "e"]


def test_a_job_whose_profile_is_busy_waits_for_it_without_holding_back_others():
    """Два задания на один профиль — никогда разом; сосед на другом профиле
    не ждёт за ним, а оно первым берёт свой профиль, когда тот свободен."""
    m = Machine(available_gb=64, settings=ps.SlotSettings(ceiling=2, warmup_s=0))
    slots = m.slots()
    a = Job(slots, "a", "P1")
    assert _until(lambda: a.started)
    b = Job(slots, "b", "P1")             # тот же профиль
    c = Job(slots, "c", "P2")
    assert _until(lambda: c.started), "другой профиль ждал за занятым"
    time.sleep(0.2)
    assert not b.started and b.waits[0]["reason"] == "profile"
    d = Job(slots, "d", "P3")            # мест нет: a и c идут
    time.sleep(0.2)
    assert not d.started
    a.finish()
    assert _until(lambda: b.started), "профиль освободился, а его задание не стартовало"
    time.sleep(0.2)
    assert not d.started, "задание, пришедшее позже, обошло ждавшее свой профиль"
    b.finish(); c.finish()
    assert _until(lambda: d.started)
    d.finish()


def test_two_waiting_jobs_on_one_profile_keep_their_order():
    m = Machine(available_gb=64, settings=ps.SlotSettings(warmup_s=0))
    slots = m.slots()
    a = Job(slots, "a", "P1")
    b = Job(slots, "b", "P1")
    c = Job(slots, "c", "P1")
    a.finish()
    assert _until(lambda: b.started)
    time.sleep(0.2)
    assert not c.started
    b.finish()
    assert _until(lambda: c.started)
    c.finish()


def test_a_cancelled_waiting_job_leaves_the_queue_at_once():
    m = Machine(available_gb=64, settings=ps.SlotSettings(override=1, warmup_s=0))
    slots = m.slots(tick_s=30)            # по таймеру не проснётся: только по wake
    a = Job(slots, "a", "P1")
    b = Job(slots, "b", "P2")
    c = Job(slots, "c", "P3")
    b.cancel.set()
    slots.wake()
    assert _until(lambda: b.done.is_set(), timeout=2) and b.grant is None
    assert _waiting(slots) == ["c"]
    a.finish()
    assert _until(lambda: c.started, timeout=2)
    c.finish()


def test_a_just_started_profile_holds_back_the_next_until_it_has_warmed_up():
    m = Machine(available_gb=3.5, settings=ps.SlotSettings(warmup_s=90, profile_mb=OLD_PROFILE_MB, **OLD))
    slots = m.slots()
    a = Job(slots, "a", "P1")
    b = Job(slots, "b", "P2")
    time.sleep(0.2)
    assert a.started and not b.started
    assert b.waits[0]["reason"] == "memory_low" and b.waits[0]["warming"] == 1
    m.now += 91                            # первый профиль поднялся; память та же
    assert _until(lambda: b.started)
    a.finish(); b.finish()


# ── publish_jobs: как очередь видна заданию ──────────────────────────────

@pytest.fixture
def runner(monkeypatch):
    """`_run_job` без RPA и без реальной памяти: одно место разом."""
    machine = Machine(available_gb=64, settings=ps.SlotSettings(override=1, warmup_s=0))
    slots = machine.slots(tick_s=30)
    monkeypatch.setattr(pj, "_slots", slots)
    monkeypatch.setattr(pj, "use_rpa_publish", lambda: True)
    monkeypatch.setattr(pj, "_clear_inherited_asyncio_loop", lambda: None)
    monkeypatch.setattr(pj, "keep_display_awake", lambda *a, **k: contextlib.nullcontext())
    monkeypatch.setattr(pj, "_watch_screen_lock", lambda *a, **k: contextlib.nullcontext())
    monkeypatch.setattr(pj, "_write_draft_status", lambda *a, **k: None)
    monkeypatch.setattr(pj, "profile_key_for", lambda label: label)

    entered: dict[str, threading.Event] = {}
    release: dict[str, threading.Event] = {}

    def fake_rpa(job_id, slots_, *, account_label, **kw):
        entered[account_label].set()
        release[account_label].wait(timeout=5)
        with pj._jobs_lock:
            pj._jobs[job_id]["profile_closed"] = True    # чистый прогон закрыл профиль
        pj._finish(job_id)

    monkeypatch.setattr(pj, "_run_rpa", fake_rpa)
    made: list[str] = []
    threads: list[threading.Thread] = []

    def job(label: str) -> str:
        entered[label] = threading.Event()
        release[label] = threading.Event()
        job_id = f"acl1321-{label}"
        with pj._jobs_lock:
            pj._jobs[job_id] = {
                "job_id": job_id, "account_label": label, "status": "pending",
                "results": [], "errors": [], "done_count": 0, "total": 1,
                "cancel_requested": False, "creative_notices": [],
                "campaign_names": [f"CAMP_{label}"], "created_at": pj._now_iso(),
            }
        made.append(job_id)
        t = threading.Thread(target=pj._run_job, args=(job_id, []), kwargs={
            "account_label": label, "country": "CL", "via_multilogin": True,
            "activate": False, "dry_run": False, "hand_off": True,
        }, daemon=True)
        threads.append(t)
        t.start()
        assert _until(lambda: pj.get_job(job_id)["status"] != "pending")
        return job_id

    yield {"job": job, "entered": entered, "release": release, "slots": slots}
    for ev in release.values():
        ev.set()
    for t in threads:          # задания доигрывают до конца, потом их убираем
        t.join(timeout=10)
    with pj._jobs_lock:
        for job_id in made:
            pj._jobs.pop(job_id, None)


def test_a_job_over_the_limit_is_queued_with_its_place_and_then_runs(runner):
    from src.fb_ads_rpa import run_telemetry as rt

    r = runner
    first = r["job"]("Doc19")
    assert r["entered"]["Doc19"].wait(5)
    second = r["job"]("KLD")
    assert _until(lambda: (pj.get_job(second).get("queue") or {}).get("reason") == "slot")
    snap = pj.get_job(second)
    assert snap["status"] == "queued" and snap["queue"]["ahead"] == 0
    assert not r["entered"]["KLD"].is_set()
    assert pj.active_job_count() >= 2, "ждущее задание должно держать обновление ACL"

    r["release"]["Doc19"].set()
    assert r["entered"]["KLD"].wait(5), "место освободилось, а задание не стартовало"
    assert pj.get_job(first)["status"] == "done"
    snap = pj.get_job(second)
    assert snap["status"] == "running" and "queue" not in snap and snap["slot_wait_s"] >= 0
    r["release"]["KLD"].set()
    assert _until(lambda: pj.get_job(second)["status"] == "done")

    rows = [json.loads(line) for line in rt.TELEMETRY_PATH.read_text(encoding="utf-8").splitlines()]
    waits = {row["job"]: row for row in rows if row.get("step") == "job_phase" and row.get("note") == "slot_queue"}
    assert set(waits) >= {first, second}
    assert waits[second]["campaign"] == "CAMP_KLD" and "duration_s" in waits[second]
    diag = waits[second]["diag"]
    assert diag["account"] == "KLD" and diag["first_hold"]["reason"] == "override_full"
    assert diag["decision"]["allow"] is True and diag["decision"]["limit"] == 1
    assert waits[first]["diag"]["first_hold"] is None


def test_cancelling_a_queued_job_stops_it_without_ever_starting_it(runner):
    r = runner
    r["job"]("Doc19")
    assert r["entered"]["Doc19"].wait(5)
    second = r["job"]("KLD")
    assert _until(lambda: pj.get_job(second).get("queue"))
    assert pj.cancel_job(second)["ok"] is True
    # Очередь спит по таймеру 30 с — отмена будит её сама.
    assert _until(lambda: pj.get_job(second)["status"] == "cancelled", timeout=2)
    assert not r["entered"]["KLD"].is_set()
    assert r["slots"].snapshot()["waiting"] == []
    r["release"]["Doc19"].set()


def test_the_progress_answer_carries_the_place_in_the_queue(runner):
    from src.web.publish_progress import job_progress

    r = runner
    r["job"]("Doc19")
    assert r["entered"]["Doc19"].wait(5)
    r["job"]("KLD")
    third = r["job"]("DQ")
    assert _until(lambda: (pj.get_job(third).get("queue") or {}).get("ahead") == 1)
    got = job_progress(pj.get_job(third))
    assert got["status"] == "queued" and got["queue"]["reason"] == "slot" and got["queue"]["ahead"] == 1
    r["release"]["Doc19"].set()


def test_the_slots_endpoint_says_how_many_run_at_once(monkeypatch):
    import httpx
    from src.web.app import app

    machine = Machine(available_gb=5.0, settings=ps.SlotSettings())
    monkeypatch.setattr(pj, "_slots", ps.ProfileSlots(
        memory=lambda: mem(5.0), settings=lambda: machine.settings, tick_s=0.05,
        usage=ps.ProfileUsage(lambda p: {}, background=False)))

    async def get():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            return await client.get("/publish-jobs/slots")

    res = asyncio.run(get())
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["mode"] == "auto" and body["running"] == [] and body["waiting"] == []
    assert body["next"]["reason"] == "memory_low" and body["next"]["available_mb"] == 5 * GB
    assert body["settings"]["ceiling"] == ps.DEFAULT_CEILING == 2
