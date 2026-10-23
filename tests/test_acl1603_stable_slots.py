# -*- coding: utf-8 -*-
"""Стабильная сборка, этап 0 (ACL-1603): два профиля, запас памяти, без дублей.

05.10 «Publish all» на ~20 аккаунтов: очередь пустила три профиля при 2,4 ГБ
свободных из 32, вкладки Meta зависли, а второй «Publish all» поставил десять
заданий на аккаунты, у которых задания уже были. Здесь: не больше двух
профилей разом при любой памяти, старт только при «профиль + 3 ГБ запаса»,
падение памяти посреди прогона закрывает новые старты, дубль задания
отказывается, чтения (ACL-1580) делят место Publish и не виснут под потолком,
потолок меняется настройкой машины.
"""
from __future__ import annotations

import asyncio
import dataclasses
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


def mem(available_gb: float) -> ps.MemoryReading:
    return ps.MemoryReading(total_mb=32 * GB, available_mb=int(available_gb * GB))


def _until(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


def _settings(**changes) -> ps.SlotSettings:
    """Умолчания ACL-1603 (из пустого окружения) с точечными правками."""
    return dataclasses.replace(ps.load_settings({}), **changes)


class Machine:
    """Подставная машина: свободная память и часы — от теста; настройки — по умолчанию."""

    def __init__(self, available_gb, settings=None):
        self.available_gb = available_gb
        self.now = 1000.0
        self.settings = settings or _settings(warmup_s=0)

    def slots(self, tick_s=0.02):
        probe = ps.ProfileUsage(lambda profiles: {}, clock=lambda: self.now, background=False)
        return ps.ProfileSlots(memory=lambda: mem(self.available_gb),
                               settings=lambda: self.settings, clock=lambda: self.now,
                               tick_s=tick_s, usage=probe)


class Job:
    def __init__(self, slots, job_id, profile, *, share_profile=False):
        self.id, self.slots = job_id, slots
        self.waits: list[dict] = []
        self.grant = None
        self.done = threading.Event()
        self.cancel = threading.Event()
        threading.Thread(target=self._run, args=(profile, share_profile), daemon=True).start()
        assert _until(lambda: self.done.is_set()
                      or job_id in {w["job_id"] for w in slots.snapshot()["waiting"]})

    def _run(self, profile, share_profile):
        self.grant = self.slots.acquire(self.id, profile, cancelled=self.cancel.is_set,
                                        on_wait=self.waits.append, share_profile=share_profile)
        self.done.set()

    @property
    def started(self):
        return self.done.is_set() and self.grant is not None

    def finish(self):
        self.cancel.set()
        self.slots.wake()
        if self.grant is not None and not self.grant.get("shared"):
            self.slots.release(self.id)


# ── не больше двух разом ─────────────────────────────────────────────────

def test_the_defaults_are_two_profiles_a_three_gb_reserve_and_a_floor_of_three_and_a_half():
    s = ps.load_settings({})
    assert s.ceiling == 2 and s.reserve_mb == 3 * GB and s.profile_floor_mb == int(3.5 * GB)
    assert s.override is None


def test_never_more_than_two_run_at_once_however_much_memory_there_is():
    m = Machine(available_gb=128)
    slots = m.slots()
    jobs = [Job(slots, f"j{i}", f"P{i}") for i in range(6)]
    assert _until(lambda: sum(j.started for j in jobs) == 2)
    time.sleep(0.2)
    assert sum(j.started for j in jobs) == 2
    assert len(slots.snapshot()["running"]) == 2
    waiting = [j for j in jobs if not j.started]
    # Первый в очереди упирается в потолок, остальные ждут своей очереди.
    assert all(w.waits and w.waits[0]["reason"] in ("ceiling", "order") for w in waiting)
    head = min(waiting, key=lambda w: int(w.id[1:]))
    assert head.waits[0]["reason"] == "ceiling" and head.waits[0]["cap"] == 2
    # Освободилось место — следующий идёт, но снова не больше двух.
    next(j for j in jobs if j.started).finish()
    assert _until(lambda: sum(j.started for j in jobs) == 3)
    time.sleep(0.1)
    assert len(slots.snapshot()["running"]) == 2
    for j in jobs:
        j.finish()


# ── запас памяти ─────────────────────────────────────────────────────────

def test_a_start_is_refused_when_free_memory_is_below_profile_plus_reserve():
    s = ps.load_settings({})
    low = ps.decide(1, 0, mem(6.4), s)                  # 3,5 + 3 = 6,5
    assert low["allow"] is False and low["reason"] == "memory_low"
    assert low["need_mb"] == int(6.5 * GB) and low["cap"] == 2 and low["reserve_mb"] == 3 * GB
    assert ps.decide(1, 0, mem(6.5), s)["allow"] is True


def test_the_measured_use_has_a_floor_of_three_and_a_half_gb():
    s = ps.load_settings({})
    light = ps.decide(1, 0, mem(20), s, measured_mb=2 * GB)
    assert light["profile_mb"] == int(3.5 * GB) and light["profile_mb_source"] == "measured"
    heavy = ps.decide(1, 0, mem(6.9), s, measured_mb=4 * GB)   # 4 + 3 = 7
    assert heavy["allow"] is False and heavy["profile_mb"] == 4 * GB


def test_the_queue_takes_the_max_of_the_measurements_not_the_average():
    now = [1000.0]
    usage = ps.ProfileUsage(lambda profiles: {"P1": 3 * GB, "P2": 5 * GB}, clock=lambda: now[0],
                            background=False)
    settings = _settings(ceiling=3, warmup_s=10, settle_s=10)
    slots = ps.ProfileSlots(memory=lambda: mem(20), settings=lambda: settings,
                            clock=lambda: now[0], tick_s=0.02, usage=usage)
    a = Job(slots, "a", "P1")
    b = Job(slots, "b", "P2")
    now[0] += 20
    assert slots._decide_now()["measured_mb"] == 5 * GB            # среднее было бы 4
    for j in (a, b):
        j.finish()


def test_a_memory_drop_mid_run_stops_new_starts_until_it_recovers():
    m = Machine(available_gb=40, settings=_settings(ceiling=3, warmup_s=0))
    slots = m.slots()
    a = Job(slots, "a", "P1")
    b = Job(slots, "b", "P2")
    assert a.started and b.started
    m.available_gb = 2.0                            # человек открыл тяжёлое: меньше запаса
    c = Job(slots, "c", "P3")
    time.sleep(0.2)
    assert not c.started
    assert c.waits[-1]["reason"] == "below_reserve" and c.waits[-1]["available_mb"] == 2 * GB
    m.available_gb = 40                             # память вернулась
    assert _until(lambda: c.started), "память вернулась, а профиль не стартовал"
    for j in (a, b, c):
        j.finish()


def test_a_profile_that_is_warming_up_is_counted_so_the_next_start_is_rechecked():
    m = Machine(available_gb=9.5, settings=_settings(ceiling=3, warmup_s=90))
    slots = m.slots()
    a = Job(slots, "a", "P1")
    b = Job(slots, "b", "P2")       # 9,5 ГБ < 3,5 (b) + 3,5 (a ещё греется) + 3 запас
    time.sleep(0.2)
    assert a.started and not b.started and b.waits[0]["warming"] == 1
    m.now += 91                     # a прогрелся и занял свою память; 9,5 ≥ 3,5 + 3
    assert _until(lambda: b.started)
    a.finish()
    b.finish()


# ── настройка машины ─────────────────────────────────────────────────────

def test_the_cap_is_set_in_the_machine_env(monkeypatch):
    for key in ("ACL_MAX_PARALLEL_PROFILES", "ACL_MAX_PARALLEL_CEILING"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ACL_MAX_PROFILES", "3")
    assert ps.load_settings().ceiling == 3
    monkeypatch.setenv("ACL_MAX_PROFILES", "1")
    assert ps.load_settings().ceiling == 1
    monkeypatch.setenv("ACL_MAX_PROFILES", "junk")
    assert ps.load_settings().ceiling == 2
    # Новое имя главнее прежних.
    assert ps.load_settings({"ACL_MAX_PROFILES": "1", "ACL_MAX_PARALLEL_PROFILES": "4"}).ceiling == 1


def test_a_cap_of_one_from_the_env_runs_one_even_with_plenty_of_memory(monkeypatch):
    monkeypatch.setenv("ACL_MAX_PROFILES", "1")
    monkeypatch.delenv("ACL_MAX_PARALLEL_PROFILES", raising=False)
    m = Machine(available_gb=100, settings=dataclasses.replace(ps.load_settings(), warmup_s=0))
    slots = m.slots()
    a = Job(slots, "a", "P1")
    b = Job(slots, "b", "P2")
    time.sleep(0.15)
    assert a.started and not b.started
    assert b.waits[0]["reason"] == "ceiling" and b.waits[0]["cap"] == 1
    a.finish()
    assert _until(lambda: b.started)
    b.finish()


def test_the_legacy_manual_number_no_longer_skips_the_memory_check():
    s = ps.load_settings({"ACL_MAX_PARALLEL_PROFILES": "4"})
    assert s.override is None and s.ceiling == 4
    assert ps.decide(1, 0, mem(1.0), s)["allow"] is False


# ── чтения (ACL-1580) под потолком ───────────────────────────────────────

def test_a_read_shares_a_publishs_slot_even_when_both_slots_are_taken():
    m = Machine(available_gb=64)
    slots = m.slots()
    a = Job(slots, "pub-a", "P1")
    b = Job(slots, "pub-b", "P2")
    assert a.started and b.started
    read = Job(slots, "read-a", "P1", share_profile=True)
    assert _until(lambda: read.started), "чтение должно делить место Publish, а не ждать его"
    assert read.grant["shared"] is True
    assert len(slots.snapshot()["running"]) == 2
    for j in (read, a, b):
        j.finish()


def test_a_read_on_another_profile_waits_for_a_slot_and_then_runs_without_deadlock():
    m = Machine(available_gb=64)
    slots = m.slots()
    a = Job(slots, "pub-a", "P1")
    b = Job(slots, "pub-b", "P2")
    read = Job(slots, "read-c", "P3", share_profile=True)
    time.sleep(0.15)
    assert not read.started and read.waits[0]["reason"] == "ceiling"
    a.finish()
    assert _until(lambda: read.started)
    read.finish()
    b.finish()


def test_a_waiting_read_does_not_block_a_read_on_a_profile_that_is_already_running():
    """Чтение P3 ждёт место; чтение P1 (занят Publish) идёт мимо очереди."""
    m = Machine(available_gb=64)
    slots = m.slots()
    a = Job(slots, "pub-a", "P1")
    b = Job(slots, "pub-b", "P2")
    waiting_read = Job(slots, "read-c", "P3", share_profile=True)
    sharing = Job(slots, "read-a", "P1", share_profile=True)
    assert _until(lambda: sharing.started)
    assert not waiting_read.started
    for j in (sharing, a, b):
        j.finish()
    assert _until(lambda: waiting_read.started)
    waiting_read.finish()


# ── дубли заданий ────────────────────────────────────────────────────────

@pytest.fixture
def clean_jobs(monkeypatch):
    monkeypatch.setattr(pj, "use_rpa_publish", lambda: True)
    with pj._jobs_lock:
        before = dict(pj._jobs)
        pj._jobs.clear()
    yield
    with pj._jobs_lock:
        pj._jobs.clear()
        pj._jobs.update(before)


@pytest.mark.parametrize("status", ["pending", "queued", "running", "cancelling"])
def test_a_second_job_for_an_account_that_is_already_in_the_queue_is_refused(clean_jobs, status):
    first = pj.create_job("Doc19", "CL", [{}])
    with pj._jobs_lock:
        pj._jobs[first]["status"] = status
    with pytest.raises(pj.DuplicateJobError) as err:
        pj.create_job(" doc19 ", "CL", [{}])          # регистр и пробелы не спасают
    assert str(err.value) == "doc19 is already in the queue"
    assert err.value.job_id == first
    assert len(pj._jobs) == 1


def test_another_account_and_a_finished_job_do_not_count_as_duplicates(clean_jobs):
    first = pj.create_job("Doc19", "CL", [{}])
    pj.create_job("KLD", "CL", [{}])
    with pj._jobs_lock:
        pj._jobs[first]["status"] = "done"
    pj.create_job("Doc19", "CL", [{}])
    assert len(pj._jobs) == 3


def test_two_simultaneous_requests_for_one_account_make_one_job(clean_jobs):
    made, refused = [], []

    def go():
        try:
            made.append(pj.create_job("Doc19", "CL", [{}]))
        except pj.DuplicateJobError:
            refused.append(1)

    threads = [threading.Thread(target=go) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(made) == 1 and len(refused) == 7


def test_the_endpoint_answers_409_with_the_account_so_publish_all_can_list_it(monkeypatch):
    import httpx
    from src.web import app as app_mod

    monkeypatch.setattr(app_mod, "_parse_build_upload_form",
                        lambda form: ("CL", "Doc19", [{"name": "x"}]))

    def refuse(*a, **kw):
        raise pj.DuplicateJobError("Doc19", "job-1")

    monkeypatch.setattr(app_mod, "start_job", refuse)

    async def post():
        transport = httpx.ASGITransport(app=app_mod.app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            return await client.post("/publish-jobs", data={"user_confirmed": "1"})

    res = asyncio.run(post())
    assert res.status_code == 409, res.text
    body = res.json()
    assert body["error"] == "Doc19 is already in the queue" and body["duplicate"] is True
    assert body["account"] == "Doc19"


def test_publish_all_lists_the_accounts_it_skipped_as_duplicates():
    html = (ROOT / "src" / "web" / "templates" / "report.html").read_text(encoding="utf-8")
    assert "data.duplicate" in html and "state: 'dup'" in html
    assert "Skipped as duplicate (already in the queue)" in html


# ── что видит дешборд и телеметрия ───────────────────────────────────────

def test_the_dashboard_sees_each_accounts_state(clean_jobs, monkeypatch):
    monkeypatch.setattr(pj, "_slots", Machine(available_gb=64).slots())
    ids = {name: pj.create_job(name, "CL", [{}]) for name in ("A", "B", "C")}
    with pj._jobs_lock:
        pj._jobs[ids["A"]]["status"] = "running"
        pj._jobs[ids["B"]]["status"] = "queued"
        pj._jobs[ids["B"]]["queue"] = pj._queue_view({"reason": "memory_low", "available_mb": 1})
        pj._jobs[ids["C"]]["status"] = "queued"
        pj._jobs[ids["C"]]["queue"] = pj._queue_view({"reason": "ceiling", "cap": 2})
    states = {a["account"]: a["state"] for a in pj.slots_snapshot()["accounts"]}
    assert states == {"A": "running", "B": "waiting_memory", "C": "waiting_slot"}
    assert pj._queue_view({"reason": "below_reserve"})["state"] == "waiting_memory"
    assert pj._queue_view({"reason": "profile"})["state"] == "waiting_profile"


def test_the_decision_for_telemetry_carries_cap_free_memory_measured_use_and_reason():
    s = ps.load_settings({})
    d = ps.decide(1, 0, mem(5.0), s, measured_mb=4 * GB)
    row = pj._decision_for_telemetry(d)
    for key in ("cap", "available_mb", "measured_mb", "profile_mb", "reserve_mb", "reason"):
        assert key in row, key
    assert row["reason"] == "memory_low" and row["cap"] == 2


# ── раунд 2 ревью: открытые окна и первый профиль ───────────────────────

def _slots_seeing_all_profiles(available_gb=128):
    """Очередь, чей замер видит каждый профиль открытым (3,5 ГБ у каждого)."""
    machine = Machine(available_gb=available_gb)
    probe = ps.ProfileUsage(lambda profiles: {p: int(3.5 * GB) for p in profiles},
                            clock=lambda: machine.now, background=False)
    return ps.ProfileSlots(memory=lambda: mem(machine.available_gb), settings=lambda: machine.settings,
                           clock=lambda: machine.now, tick_s=0.02, usage=probe)


def _mb(mb: int) -> ps.MemoryReading:
    return ps.MemoryReading(total_mb=32 * GB, available_mb=mb)


@pytest.mark.parametrize("free_mb", [2810, 400, 6000])
def test_the_first_profile_waits_for_memory_too(free_mb):
    """05.10 16:27: первый профиль пускали при 2810 МБ свободных."""
    d = ps.decide(0, 0, _mb(free_mb), ps.load_settings({}))
    assert d["allow"] is False
    assert d["reason"] == ("below_reserve" if free_mb < 3 * GB else "memory_low")
    assert pj._queue_view(d)["state"] == "waiting_memory"
    assert d["need_mb"] == int(6.5 * GB)


def test_the_first_profile_starts_with_profile_plus_reserve_and_waits_until_then():
    m = Machine(available_gb=2.81)
    slots = m.slots()
    a = Job(slots, "a", "P1")
    time.sleep(0.15)
    assert not a.started and a.waits[0]["reason"] == "below_reserve"
    m.available_gb = 6.5
    assert _until(lambda: a.started)
    a.finish()


def test_windows_kept_open_after_a_failure_still_hold_their_slots():
    """Два оставленных окна + третий/четвёртый Mimic — ровно сбой 05.10."""
    slots = _slots_seeing_all_profiles()
    a = Job(slots, "a", "P1")
    b = Job(slots, "b", "P2")
    slots.keep("a")
    slots.keep("b")                                  # оба задания кончились, окна открыты
    c = Job(slots, "c", "P3")
    d = Job(slots, "d", "P4")
    time.sleep(0.2)
    assert not c.started and not d.started
    assert c.waits[0]["reason"] == "review_windows" and c.waits[0]["kept"] == 2
    assert pj._queue_view(c.waits[0])["state"] == "waiting_review"
    assert pj._queue_view(c.waits[0])["kept"] == 2
    slots.release("a")                               # человек закрыл одно окно
    assert _until(lambda: c.started)
    time.sleep(0.1)
    assert not d.started
    for j in (c, d):
        j.finish()
    slots.release("b")


def test_a_new_job_on_a_profile_with_a_kept_window_reuses_it_instead_of_waiting():
    slots = _slots_seeing_all_profiles()
    Job(slots, "a", "P1")
    Job(slots, "b", "P2")
    slots.keep("a")
    slots.keep("b")
    again = Job(slots, "a2", "P1")                   # тот же профиль, окно уже открыто
    assert _until(lambda: again.started)
    assert [r["profile"] for r in slots.snapshot()["running"]].count("P1") == 1
    again.finish()


def test_a_kept_window_closed_by_hand_in_multilogin_stops_holding_its_slot():
    now = [1000.0]
    found = {"P1": 3 * GB}
    usage = ps.ProfileUsage(lambda profiles: {p: v for p, v in found.items() if p in profiles},
                            clock=lambda: now[0], background=False)
    settings = _settings(warmup_s=10, settle_s=10)
    slots = ps.ProfileSlots(memory=lambda: mem(128), settings=lambda: settings,
                            clock=lambda: now[0], tick_s=0.02, usage=usage)
    a = Job(slots, "a", "P1")
    Job(slots, "b", "P2")
    slots.keep("a")
    slots.keep("b")
    now[0] += 30
    found.clear()                                    # окон в Multilogin больше нет
    now[0] += 30
    c = Job(slots, "c", "P3")
    assert _until(lambda: c.started), "закрытые руками окна навсегда заняли очередь"
    c.finish()
    assert a


def _fake_run(monkeypatch, *, closes: bool, cancel_after_grant: bool = False):
    # Замер видит профиль открытым: иначе «окно закрыто руками» снимет место
    # само, и тест не отличил бы `close_profile` без `release` (ревью N1).
    slots = _slots_seeing_all_profiles()
    monkeypatch.setattr(pj, "_slots", slots)
    monkeypatch.setattr(pj, "use_rpa_publish", lambda: True)
    monkeypatch.setattr(pj, "_clear_inherited_asyncio_loop", lambda: None)
    import contextlib
    monkeypatch.setattr(pj, "keep_display_awake", lambda *a, **k: contextlib.nullcontext())
    monkeypatch.setattr(pj, "_watch_screen_lock", lambda *a, **k: contextlib.nullcontext())
    monkeypatch.setattr(pj, "profile_key_for", lambda label: label)

    def fake_rpa(job_id, slots_, **kw):
        if closes:
            with pj._jobs_lock:
                pj._jobs[job_id]["profile_closed"] = True
        pj._finish(job_id)

    monkeypatch.setattr(pj, "_run_rpa", fake_rpa)
    job_id = pj.create_job("Doc19", "CL", [{}])
    if cancel_after_grant:
        real_acquire = slots.acquire

        def acquire_then_cancel(*a, **kw):
            grant = real_acquire(*a, **kw)
            with pj._jobs_lock:
                pj._jobs[job_id]["cancel_requested"] = True
            return grant

        monkeypatch.setattr(slots, "acquire", acquire_then_cancel)
    pj._run_job(job_id, [], account_label="Doc19", country="CL", via_multilogin=True,
                activate=False, dry_run=False, hand_off=True)
    return slots, job_id


def test_a_finished_job_that_left_its_window_open_keeps_the_slot_until_the_profile_is_closed(
        clean_jobs, monkeypatch):
    slots, job_id = _fake_run(monkeypatch, closes=False)
    assert [(r["profile"], r["kept"]) for r in slots.snapshot()["running"]] == [("Doc19", True)]

    from src.fb_ads_rpa import rpa_publisher as rp
    monkeypatch.setattr(rp, "close_account_profile", lambda label: None)
    got = pj.close_profile(job_id)                   # кнопка Close profile
    assert got["ok"] is True
    assert slots.snapshot()["running"] == []


def test_a_job_that_closed_its_profile_releases_the_slot(clean_jobs, monkeypatch):
    slots, _ = _fake_run(monkeypatch, closes=True)
    assert slots.snapshot()["running"] == []


# ── точное совпадение аккаунта и текст отказа ────────────────────────────

def test_an_account_whose_name_only_starts_the_same_is_not_a_duplicate(clean_jobs):
    pj.create_job("FacebookDoc12", "CL", [{}])
    pj.create_job("FacebookDoc121", "CL", [{}])      # префикс — другой аккаунт
    pj.create_job("FacebookDoc1", "CL", [{}])
    with pytest.raises(pj.DuplicateJobError):
        pj.create_job("facebookdoc12 ", "CL", [{}])


def test_the_refusal_message_uses_the_trimmed_label(clean_jobs):
    pj.create_job("Doc19", "CL", [{}])
    with pytest.raises(pj.DuplicateJobError) as err:
        pj.create_job("  Doc19 ", "CL", [{}])
    assert str(err.value) == "Doc19 is already in the queue"


# ── раунд 3 ревью ────────────────────────────────────────────────────────

def test_no_slot_is_kept_for_a_job_that_never_reached_the_browser(clean_jobs, monkeypatch):
    slots, job_id = _fake_run(monkeypatch, closes=False, cancel_after_grant=True)
    assert pj._jobs[job_id].get("started_at") is None
    assert slots.snapshot()["running"] == []


def test_kept_entries_are_not_counted_as_warming_up():
    now = [1000.0]
    settings = _settings(warmup_s=90)
    slots = ps.ProfileSlots(memory=lambda: mem(128), settings=lambda: settings, clock=lambda: now[0],
                            tick_s=0.02,
                            usage=ps.ProfileUsage(lambda p: {}, clock=lambda: now[0], background=False))
    Job(slots, "a", "P1")
    assert slots._warming() == 1                     # только что стартовал — греется
    slots.keep("a", "Doc19")
    assert slots._warming() == 0                     # окно оставлено: память уже занята и измерима


def test_the_waiting_line_names_the_kept_accounts_and_the_dashboard_can_release_one():
    slots = _slots_seeing_all_profiles()
    Job(slots, "a", "P1")
    Job(slots, "b", "P2")
    slots.keep("a", "KLD_BFL_B94")
    slots.keep("b", "Doc21")
    c = Job(slots, "c", "P3")
    time.sleep(0.15)
    view = pj._queue_view(c.waits[0])
    assert view["kept_accounts"] == ["Doc21", "KLD_BFL_B94"]
    html = (ROOT / "src" / "web" / "templates" / "report.html").read_text(encoding="utf-8")
    assert "waiting: review windows still open for " in html and "data-cb-release-kept" in html
    assert slots.release_kept(" kld_bfl_b94 ") == 1          # без панели задания
    assert _until(lambda: c.started)
    assert slots.release_kept("nobody") == 0
    c.finish()


def test_the_release_endpoint_frees_a_kept_slot_and_404s_for_an_unknown_account(monkeypatch):
    import httpx
    from src.web import app as app_mod

    slots = _slots_seeing_all_profiles()
    monkeypatch.setattr(pj, "_slots", slots)
    Job(slots, "a", "P1")
    slots.keep("a", "Doc19")

    async def post(account):
        transport = httpx.ASGITransport(app=app_mod.app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            return await client.post("/publish-jobs/kept/release", data={"account": account})

    ok = asyncio.run(post("Doc19"))
    assert ok.status_code == 200 and ok.json() == {"ok": True, "released": 1}
    assert slots.snapshot()["running"] == []
    assert asyncio.run(post("Doc19")).status_code == 404


def test_pruning_a_kept_window_of_the_waiting_profile_does_not_subtract_twice():
    """N4: окно того же профиля и так не считается; убрав его, running не падает ниже."""
    now = [1000.0]
    found = {"P2": 3 * GB}                           # P1 в Multilogin не найден — окно закрыто
    usage = ps.ProfileUsage(lambda profiles: {p: v for p, v in found.items() if p in profiles},
                            clock=lambda: now[0], background=False)
    settings = _settings(warmup_s=10, settle_s=10, ceiling=3)
    slots = ps.ProfileSlots(memory=lambda: mem(128), settings=lambda: settings, clock=lambda: now[0],
                            tick_s=0.02, usage=usage)
    Job(slots, "a", "P1")
    Job(slots, "b", "P2")
    slots.keep("a", "A")
    now[0] += 30
    got = slots._decide_now("P1")
    assert got["running"] == 1, got                   # только P2; не 0 и не -1


def test_the_first_profile_wait_message_mentions_the_reserve():
    html = (ROOT / "src" / "web" / "templates" / "report.html").read_text(encoding="utf-8")
    assert "Waiting for memory: " in html and "GB reserve)" in html
    d = ps.decide(0, 0, _mb(int(5.4 * GB)), ps.load_settings({}))
    view = pj._queue_view(d)
    assert (view["need_mb"], view["reserve_mb"], view["profile_mb"]) == (int(6.5 * GB), 3 * GB, int(3.5 * GB))


# ── раунд 4 ревью ────────────────────────────────────────────────────────

def test_the_release_click_handler_survives_a_document_without_event_listeners():
    """Песочница тестов страницы даёт `document` без addEventListener (B3)."""
    html = (ROOT / "src" / "web" / "templates" / "report.html").read_text(encoding="utf-8")
    assert "if (typeof document.addEventListener === 'function') document.addEventListener('click'" in html


def test_releasing_the_account_of_a_running_job_is_a_404_and_keeps_the_slot(monkeypatch):
    import httpx
    from src.web import app as app_mod

    slots = _slots_seeing_all_profiles()
    monkeypatch.setattr(pj, "_slots", slots)
    running = Job(slots, "a", "P1")                  # идёт, не оставлен

    async def post():
        transport = httpx.ASGITransport(app=app_mod.app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            return await client.post("/publish-jobs/kept/release", data={"account": "P1"})

    assert asyncio.run(post()).status_code == 404
    assert [(r["job_id"], r["kept"]) for r in slots.snapshot()["running"]] == [("a", False)]
    running.finish()


def test_the_production_queue_reads_the_real_host_memory():
    """По тождеству, не вызовом: autouse-подмена памяти в conftest не должна это скрыть (R17)."""
    import importlib
    import inspect

    src = inspect.getsource(pj)
    line = [ln for ln in src.splitlines() if ln.startswith("_slots = ProfileSlots(")][0]
    assert "memory" not in line                                  # без своей памяти (ACL-2113: только evictor)
    default = inspect.signature(ps.ProfileSlots.__init__).parameters["memory"].default
    # conftest подменяет умолчание на время теста; настоящее — в исходнике модуля.
    assert "memory: Callable[[], MemoryReading | None] = read_memory" in inspect.getsource(ps.ProfileSlots.__init__)
    assert default is not None and importlib.import_module("src.web.profile_slots").read_memory is ps.read_memory
