# -*- coding: utf-8 -*-
"""ACL-1642: задание на уже открытый профиль не просит память нового профиля.

06.10: пять профилей открыты (~6 ГБ), 0 идёт, 7 ждут, `memory_low`, 5,4 ГБ
свободно против 6,6 нужных — владельцу приходилось закрывать окна, чтобы
робот открыл те же самые. Здесь: открытый профиль память не спрашивает (но
потолок действует), не считается дважды с оставленным окном, пропускается
вперёд головы очереди, ждущей память; проверка «открыт ли» только читает,
кэшируется и не держит замок очереди; закрытый профиль — как раньше.
Браузер и Multilogin не нужны: проверка и память подставные.
"""
from __future__ import annotations

import dataclasses
import sys
import threading
import time
from pathlib import Path

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


class Machine:
    """Подставная машина: память, часы и «какие профили открыты» — от теста."""

    def __init__(self, available_gb=20.0, open_profiles=(), **settings):
        self.available_gb = available_gb
        self.now = 1000.0
        self.open = set(open_profiles)
        self.checked: list[list[str]] = []
        self.settings = dataclasses.replace(ps.load_settings({}), **{"warmup_s": 0, **settings})

    def _check(self, profiles):
        self.checked.append(list(profiles))
        return {p for p in profiles if p in self.open}

    def slots(self, tick_s=0.02):
        usage = ps.ProfileUsage(lambda profiles: {}, clock=lambda: self.now, background=False)
        probe = ps.OpenProfileProbe(self._check, clock=lambda: self.now, background=False)
        return ps.ProfileSlots(memory=lambda: mem(self.available_gb), settings=lambda: self.settings,
                               clock=lambda: self.now, tick_s=tick_s, usage=usage, open_probe=probe)


class Job:
    """Задание в своём потоке: `acquire` блокирует, пока не дадут место."""

    def __init__(self, slots, job_id, profile):
        self.job_id, self.grant, self.holds, self.done = job_id, None, [], False
        self._cancel = False

        def run():
            self.grant = slots.acquire(job_id, profile, cancelled=lambda: self._cancel,
                                       on_wait=lambda v: self.holds.append(dict(v)))
            self.done = True

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()

    def cancel(self, slots):
        self._cancel = True
        slots.wake()
        self.thread.join(2)


def test_open_profile_starts_despite_low_memory():
    m = Machine(open_profiles={"p2"})
    slots = m.slots()
    assert slots.acquire("A", "p1")["decision"]["allow"]
    m.available_gb = 2.0   # ниже запаса: закрытому профилю нельзя
    grant = slots.acquire("B", "p2")
    assert grant["decision"]["reason"] == "profile_open"
    assert grant["decision"]["profile_open"] is True
    assert not grant["decision"].get("started_ahead")


def test_closed_profile_behaves_as_before():
    m = Machine(open_profiles={"p2"})
    slots = m.slots()
    slots.acquire("A", "p1")
    m.available_gb = 2.0
    job = Job(slots, "C", "p3")
    assert _until(lambda: job.holds)
    assert job.holds[0]["reason"] == "below_reserve" and job.holds[0]["profile_open"] is False
    m.available_gb = 5.0   # выше запаса, но меньше «профиль + запас»
    slots.wake()
    assert _until(lambda: any(h["reason"] == "memory_low" for h in job.holds))
    assert not job.done
    job.cancel(slots)


def test_ceiling_still_applies_to_an_open_profile():
    m = Machine(open_profiles={"p3"})
    slots = m.slots()
    slots.acquire("A", "p1")
    slots.acquire("B", "p2")
    job = Job(slots, "C", "p3")
    assert _until(lambda: job.holds)
    assert job.holds[0]["reason"] == "ceiling" and not job.done
    slots.release("A")
    assert _until(lambda: job.done)
    assert job.grant["decision"]["reason"] == "profile_open"


def test_kept_window_is_not_counted_twice():
    m = Machine(open_profiles={"p1", "p2"})
    slots = m.slots()
    slots.acquire("A", "p1")
    slots.keep("A", "acc-a")          # окно осталось открытым на проверку
    slots.acquire("B", "p2")
    m.available_gb = 2.0
    # потолок 2 занят: оставленное окно p1 + идущий p2; новое задание на p1
    # берёт место оставленного окна, а не просит третье.
    grant = slots.acquire("A2", "p1")
    assert grant["decision"]["reason"] == "profile_open"
    snap = slots.snapshot()
    assert len(snap["running"]) == 2 and not any(r["kept"] for r in snap["running"])


def test_a_later_open_profile_job_starts_ahead_of_a_memory_blocked_head():
    m = Machine(open_profiles={"p3"})
    slots = m.slots()
    slots.acquire("A", "p1")
    m.available_gb = 5.0
    head = Job(slots, "H", "p2")             # закрытый профиль — голова, ждёт память
    assert _until(lambda: head.holds and head.holds[0]["reason"] == "memory_low")
    later = Job(slots, "L", "p3")            # открытый — идёт вперёд
    assert _until(lambda: later.done)
    assert later.grant["decision"]["started_ahead"] is True
    assert later.grant["decision"]["reason"] == "profile_open"
    assert not head.done
    assert [w["job_id"] for w in slots.snapshot()["waiting"]] == ["H"]
    # голова не голодает: память вернулась — стартует
    m.available_gb = 20.0
    slots.release("A")
    assert _until(lambda: head.done)
    assert not head.grant["decision"].get("started_ahead")


def test_nobody_jumps_a_head_blocked_by_the_ceiling():
    m = Machine(open_profiles={"p4"})
    slots = m.slots()
    slots.acquire("A", "p1")
    slots.acquire("B", "p2")
    head = Job(slots, "H", "p3")
    assert _until(lambda: head.holds and head.holds[0]["reason"] == "ceiling")
    later = Job(slots, "L", "p4")
    assert _until(lambda: later.holds)
    assert later.holds[0]["reason"] == "order" and not later.done
    head.cancel(slots)
    later.cancel(slots)


def test_two_jobs_on_one_open_profile_do_not_both_start():
    m = Machine(open_profiles={"p2"})
    slots = m.slots()
    slots.acquire("A", "p1")
    m.available_gb = 2.0
    first = Job(slots, "B1", "p2")
    assert _until(lambda: first.done)
    second = Job(slots, "B2", "p2")
    assert _until(lambda: second.holds)
    assert second.holds[0]["reason"] == "profile" and not second.done
    second.cancel(slots)


def test_among_open_profile_jobs_the_earlier_one_goes_first():
    m = Machine(open_profiles={"p3", "p4"})
    slots = m.slots()
    slots.acquire("A", "p1")
    m.available_gb = 5.0
    head = Job(slots, "H", "p2")
    assert _until(lambda: head.holds)
    first = Job(slots, "L1", "p3")
    assert _until(lambda: first.done)
    # потолок 2 теперь занят A и L1: второй открытый ждёт, а не обгоняет по гонке
    second = Job(slots, "L2", "p4")
    assert _until(lambda: second.holds)
    assert not second.done
    head.cancel(slots)
    second.cancel(slots)


def test_an_open_profile_job_is_not_counted_as_warming():
    m = Machine(open_profiles={"p2"}, warmup_s=90.0, ceiling=3)
    slots = m.slots()
    slots.acquire("A", "p1")
    m.now += 100        # A прогрелся
    m.available_gb = 2.0
    slots.acquire("B", "p2")   # открытый: память уже занята
    snap_warming = slots._warming()
    assert snap_warming == 0   # свою память он не «вот-вот займёт»


def test_detection_is_cached_and_only_reads():
    m = Machine(open_profiles={"p1"})
    probe = ps.OpenProfileProbe(m._check, clock=lambda: m.now, background=False)
    assert probe.read("p1") is True and probe.read("p1") is True and probe.read("p1") is True
    assert probe.checks == 1
    m.open.discard("p1")
    assert probe.read("p1") is True            # ещё в пределах кэша
    m.now += ps.OPEN_TTL_S + 0.1
    assert probe.read("p1") is False and probe.checks == 2
    assert probe.read("") is None


def test_stale_or_failing_detection_means_closed():
    m = Machine(open_profiles={"p1"})
    calls = {"n": 0}

    def flaky(profiles):
        calls["n"] += 1
        raise OSError("no access")

    probe = ps.OpenProfileProbe(flaky, clock=lambda: m.now, background=False)
    assert probe.read("p1") is None            # сбой: профиль считаем закрытым
    ok = ps.OpenProfileProbe(m._check, clock=lambda: m.now, background=False)
    assert ok.read("p1") is True
    m.now += ps.OPEN_MAX_AGE_S + 1             # ответ состарился, а новый не прочитан
    ok._check = flaky
    assert ok.read("p1") is None


class _ReadOnlyProc:
    """Процесс, у которого можно только читать `info`; всё остальное — провал теста."""

    def __init__(self, exe, cmdline):
        self.info = {"name": "chrome.exe", "exe": exe, "cmdline": cmdline}

    def __getattr__(self, name):
        raise AssertionError(f"open-profile check touched the process: {name}")


def test_find_open_profiles_reads_process_list_only():
    mimic = "/home/x/mlx/deps/mimic_140/chrome.exe"
    procs = [
        _ReadOnlyProc(mimic, ["chrome.exe", "--client-session-id=ABC-1"]),
        _ReadOnlyProc(mimic, ["chrome.exe", "--type=renderer", "--client-session-id=ZZZ-9"]),
        _ReadOnlyProc("/opt/google/chrome/chrome.exe", ["chrome.exe", "--client-session-id=zzz-9"]),
    ]
    opened = ps.find_open_profiles(["abc-1", "zzz-9", "none"], processes=lambda: procs)
    assert opened == {"abc-1"}


def test_slow_detection_never_blocks_the_queue():
    release = threading.Event()
    started = threading.Event()

    def hang(profiles):
        started.set()
        release.wait(5)
        return set(profiles)

    m = Machine()
    probe = ps.OpenProfileProbe(hang, background=True)
    usage = ps.ProfileUsage(lambda profiles: {}, clock=lambda: m.now, background=False)
    slots = ps.ProfileSlots(memory=lambda: mem(m.available_gb), settings=lambda: m.settings, usage=usage,
                            open_probe=probe, tick_s=0.02)
    slots.acquire("A", "p1")
    m.available_gb = 2.0
    t0 = time.monotonic()
    job = Job(slots, "B", "p2")                 # ждёт память, проверка висит
    assert started.wait(2)
    snap = slots.snapshot()                     # замок очереди свободен
    assert time.monotonic() - t0 < 1.0
    assert snap["waiting"][0]["job_id"] == "B" and not job.done
    release.set()                               # проверка ответила «открыт» и разбудила очередь
    assert _until(lambda: job.done)
    assert job.grant["decision"]["reason"] == "profile_open"


def test_queue_view_and_telemetry_carry_profile_open():
    view = pj._queue_view({"reason": "memory_low", "profile_open": False, "available_mb": 4096})
    assert view["profile_open"] is False and view["state"] == "waiting_memory"
    m = Machine(open_profiles={"p2"})
    slots = m.slots()
    slots.acquire("A", "p1")
    m.available_gb = 2.0
    decision = pj._decision_for_telemetry(slots.acquire("B", "p2")["decision"])
    assert decision["profile_open"] is True and decision["reason"] == "profile_open"


def test_dashboard_text_has_the_hint_and_the_started_ahead_note():
    html = (ROOT / "src" / "web" / "templates" / "report.html").read_text(encoding="utf-8")
    assert "a new profile needs " in html
    assert "press Release on a finished one" in html
    assert "Started ahead: its profile was already open" in html
    src = (ROOT / "src" / "web" / "profile_slots.py").read_text(encoding="utf-8")
    for forbidden in ("terminate(", ".kill(", "close_profile", "stop_profile"):
        assert forbidden not in src
