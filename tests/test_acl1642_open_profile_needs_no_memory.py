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


def test_open_profile_needs_no_memory_reading_at_all():
    """Память не прочиталась: открытому профилю она и не нужна, держит только потолок."""
    settings = dataclasses.replace(ps.load_settings({}), ceiling=ps.FALLBACK_LIMIT + 2)
    open_ = ps.decide(2, 0, None, settings, profile_open=True)
    assert open_["allow"] and open_["reason"] == "profile_open"
    closed = ps.decide(2, 0, None, settings)
    assert not closed["allow"] and closed["reason"] == "memory_unknown_full"
    full = ps.decide(settings.ceiling, 0, None, settings, profile_open=True)
    assert not full["allow"] and full["reason"] == "ceiling"


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
    # Замер видит оба профиля: оставленное окно p1 не снимается как «закрыто руками».
    usage = ps.ProfileUsage(lambda profiles: {p: 4 * GB for p in profiles},
                            clock=lambda: m.now, background=False)
    probe = ps.OpenProfileProbe(m._check, clock=lambda: m.now, background=False)
    slots = ps.ProfileSlots(memory=lambda: mem(m.available_gb), settings=lambda: m.settings,
                            clock=lambda: m.now, tick_s=0.02, usage=usage, open_probe=probe)
    slots.acquire("A", "p1")
    slots.keep("A", "acc-a")          # окно осталось открытым на проверку
    slots.acquire("B", "p2")
    m.now += 10
    # потолок 2 занят оставленным окном p1 и идущим p2; новое задание на p1
    # берёт место оставленного окна. Посчитай мы его дважды — упёрлись бы в потолок.
    grant = slots.acquire("A2", "p1")
    assert grant["decision"]["reason"] == "profile_open"
    snap = slots.snapshot()
    assert sorted(r["profile"] for r in snap["running"]) == ["p1", "p2"]
    assert not any(r["kept"] for r in snap["running"])


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


def _enqueue(slots, *jobs):
    """Поставить задания в очередь, не запуская потоков: порядок задаём сами."""
    with slots._cond:
        for job_id, profile in jobs:
            slots._waiting[job_id] = {"profile": profile, "since": 0.0}


def test_nobody_jumps_a_head_that_is_not_blocked_on_memory():
    m = Machine(open_profiles={"p4"}, ceiling=4)
    slots = m.slots()
    slots.acquire("A", "p1")
    m.available_gb = 5.0
    _enqueue(slots, ("H", "p3"), ("L", "p4"))
    with slots._cond:
        real = slots._decide_now
        # голова стоит НЕ на памяти (потолок/обзор окон): обгонять её нельзя
        slots._decide_now = lambda profile="": (
            {"allow": False, "reason": "ceiling"} if profile == "p3" else real(profile))
        assert slots._may_start_ahead("L", "H") is False
        verdict = slots._verdict("L")
        slots._decide_now = real
        assert verdict["reason"] == "order" and not verdict["behind_memory"]
        # а когда голова стоит на памяти — обгон разрешён (потолок 4 не мешает)
        assert slots._may_start_ahead("L", "H") is True


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
    m = Machine(open_profiles={"p3", "p4"}, ceiling=5)   # потолок не держит второго
    slots = m.slots()
    slots.acquire("A", "p1")
    m.available_gb = 5.0
    _enqueue(slots, ("H", "p2"), ("L1", "p3"), ("L2", "p4"))
    with slots._cond:
        second = slots._verdict("L2")
        first = slots._verdict("L1")
    assert second["allow"] is False and second["reason"] == "order"
    assert first["allow"] is True and first["started_ahead"] is True


def test_an_open_profile_job_still_reserves_its_growth_like_any_starting_job():
    """Ревью #644, B2: только что стартовавший открытый профиль ещё может вырасти."""
    m = Machine(open_profiles={"pO"}, warmup_s=90.0, ceiling=3)
    slots = m.slots()
    need_one = m.settings.profile_mb + m.settings.reserve_mb
    m.available_gb = (need_one + 100) / GB   # памяти ровно на один профиль
    assert slots.acquire("J", "pO")["decision"]["reason"] == "profile_open"
    assert slots._warming() == 1
    head = slots._decide_now("pC")           # закрытый профиль сразу следом
    assert head["allow"] is False and head["reason"] == "memory_low"
    assert head["need_mb"] == 2 * m.settings.profile_mb + m.settings.reserve_mb


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


def test_find_open_profiles_reads_process_list_only(monkeypatch):
    import psutil
    from src.fb_ads_rpa import browser

    mimic = "/home/x/mlx/deps/mimic_140/chrome.exe"
    procs = [
        _ReadOnlyProc(mimic, ["chrome.exe", "--client-session-id=ABC-1"]),
        _ReadOnlyProc(mimic, ["chrome.exe", "--client-session-id=another-profile"]),
        _ReadOnlyProc("/opt/google/chrome/chrome.exe", ["chrome.exe", "--client-session-id=zzz-9"]),
    ]
    monkeypatch.setattr(psutil, "process_iter", lambda attrs: procs)
    monkeypatch.setattr(browser, "_find_mlx_profile_dir", lambda profile: None)
    opened = ps.find_open_profiles(["abc-1", "zzz-9", "none"])
    assert opened == {"abc-1"}


def test_queue_uses_the_robot_running_check_with_resolved_folder(monkeypatch, tmp_path):
    import psutil
    from src.fb_ads_rpa import browser

    monkeypatch.setattr(psutil, "process_iter", lambda attrs: [])
    folder = tmp_path / "profile-folder"
    monkeypatch.setattr(browser, "_find_mlx_profile_dir",
                        lambda profile: folder if profile == "p1" else None)
    seen = []

    def running(self, directory, profile_id=""):
        seen.append((directory, profile_id))
        return profile_id == "p1"

    monkeypatch.setattr(browser.MultiloginDirectCDPProvider, "profile_is_running", running)
    assert ps.find_open_profiles(["p1", "p2", "", "p1"]) == {"p1"}
    assert seen == [(str(folder), "p1"), ("", "p2")]


def test_queue_recognizes_the_same_short_folder_as_the_robot(monkeypatch, tmp_path):
    import psutil
    from src.fb_ads_rpa import browser, profile_dir

    folder = tmp_path / "long-profile-folder"
    short = str(tmp_path / "SHORT~1")
    monkeypatch.setattr(browser, "_find_mlx_profile_dir", lambda profile: folder)
    monkeypatch.setattr(profile_dir, "short_path", lambda path: short)
    proc = _ReadOnlyProc("/mlx/mimic/chrome.exe", ["chrome.exe", "--user-data-dir=" + short])
    monkeypatch.setattr(psutil, "process_iter", lambda attrs: [proc])
    assert ps.find_open_profiles(["profile-id"]) == {"profile-id"}


def test_superseded_probe_cannot_replace_a_newer_closed_answer():
    release_old = threading.Event()
    first_started = threading.Event()
    clock = {"now": 0.0}
    calls = {"n": 0}
    wakes = []

    def check(profiles):
        calls["n"] += 1
        if calls["n"] == 1:
            first_started.set()
            assert release_old.wait(3)
            return set(profiles)
        return set()

    probe = ps.OpenProfileProbe(check, clock=lambda: clock["now"], ttl_s=0.5,
                                max_age_s=0.5, restart_s=1.0,
                                on_ready=lambda: wakes.append(True))
    try:
        assert probe.read("p1") is None
        assert first_started.wait(1)
        clock["now"] = 2.0
        probe.read("p1")  # the hung first check is replaced
        assert _until(lambda: probe.checks == 1)
        assert probe.read("p1") is False
        release_old.set()
        assert _until(lambda: probe.checks == 2)
        assert probe.read("p1") is False
        assert _until(lambda: len(wakes) == 1)
    finally:
        release_old.set()


def test_open_profile_low_memory_does_not_evict_an_idle_kept_window():
    m = Machine(open_profiles={"p1", "p2"}, kept_idle_close_s=1.0)
    usage = ps.ProfileUsage(lambda profiles: {p: 4 * GB for p in profiles},
                            clock=lambda: m.now, background=False)
    probe = ps.OpenProfileProbe(m._check, clock=lambda: m.now, background=False)
    evictions = []
    slots = ps.ProfileSlots(memory=lambda: mem(m.available_gb), settings=lambda: m.settings,
                            clock=lambda: m.now, usage=usage, open_probe=probe,
                            evictor=lambda *args: evictions.append(args) or True)
    slots.acquire("A", "p1")
    slots.keep("A", "account-a", kind=ps.KEPT_CONVENIENCE)
    m.now += 2
    m.available_gb = 1.0
    grant = slots.acquire("B", "p2")
    assert grant["decision"]["reason"] == "profile_open"
    assert grant["closed_for_room"] == [] and evictions == []
    kept = next(r for r in slots.snapshot()["running"] if r["profile"] == "p1")
    assert kept["kept"] and kept["kept_kind"] == ps.KEPT_CONVENIENCE


def test_reused_open_review_window_retains_its_current_kind():
    m = Machine(open_profiles={"p1", "p2"})
    usage = ps.ProfileUsage(lambda profiles: {p: 4 * GB for p in profiles},
                            clock=lambda: m.now, background=False)
    probe = ps.OpenProfileProbe(m._check, clock=lambda: m.now, background=False)
    slots = ps.ProfileSlots(memory=lambda: mem(m.available_gb), settings=lambda: m.settings,
                            clock=lambda: m.now, usage=usage, open_probe=probe)
    slots.acquire("A", "p1")
    slots.keep("A", "account-a", kind=ps.KEPT_REVIEW)
    slots.acquire("B", "p2")
    m.available_gb = 1.0
    grant = slots.acquire("A2", "p1")
    assert grant["decision"]["reason"] == "profile_open"
    assert grant["reused_kept"] == ps.KEPT_REVIEW
    assert grant["closed_for_room"] == []
    assert len(slots.snapshot()["running"]) == 2


def test_an_open_profile_still_waits_while_its_kept_window_is_closing():
    m = Machine(open_profiles={"p1"})
    slots = m.slots()
    slots.acquire("A", "p1")
    slots.keep("A", "account-a", kind=ps.KEPT_CONVENIENCE)
    _enqueue(slots, ("B", "p1"))
    with slots._cond:
        slots._running["A"]["closing"] = True
        verdict = slots._verdict("B")
    assert not verdict["allow"] and verdict["reason"] == "profile"


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
    assert "press Release" not in html            # Release памяти не освобождает
    assert "Waiting behind a job that waits for memory" in html
    assert "GB for profiles still starting or growing" in html
    assert "Started ahead: its profile was already open" in html
    # Все три места, где очередь пишет «ждёт память», берут один текст с подсказкой.
    assert "queued — waiting for memory'" not in html
    assert html.count("cbMemoryWait(") >= 4
    src = (ROOT / "src" / "web" / "profile_slots.py").read_text(encoding="utf-8")
    for forbidden in ("terminate(", ".kill(", "close_profile", "stop_profile"):
        assert forbidden not in src


def test_a_probe_thread_that_cannot_start_means_closed_not_an_error(monkeypatch):
    """CI Windows (PR #936): поток не стартовал — задание идёт как с закрытым профилем, не падает."""
    probe = ps.OpenProfileProbe(lambda profiles: {"p1"}, ttl_s=0.5)

    def fail(*a):
        raise RuntimeError("cannot start new thread")

    monkeypatch.setattr(threading.Thread, "start", fail)
    assert probe.read("p1") is None
    assert probe.read("p1") is None   # в окне отката новых попыток нет
    monkeypatch.undo()
    slots = ps.ProfileSlots(memory=lambda: mem(48.0), open_probe=probe)
    assert slots.acquire("A", "p1")["decision"]["allow"]


def test_a_failing_check_does_not_spin_the_queue():
    """Ревью #644, B1: сбой проверки + будящая очередь давали сотни потоков в секунду."""
    calls = {"n": 0}

    def failing(profiles):
        calls["n"] += 1
        raise OSError("access denied")

    m = Machine()
    usage = ps.ProfileUsage(lambda profiles: {}, background=False)
    probe = ps.OpenProfileProbe(failing, ttl_s=0.5)
    slots = ps.ProfileSlots(memory=lambda: mem(m.available_gb), settings=lambda: m.settings,
                            usage=usage, open_probe=probe, tick_s=0.02)
    slots.acquire("A", "p1")
    m.available_gb = 2.0
    stop = {"v": False}
    t = threading.Thread(target=lambda: slots.acquire("B", "p2", cancelled=lambda: stop["v"]),
                         daemon=True)
    t.start()
    time.sleep(1.2)
    stop["v"] = True
    slots.wake()
    t.join(3)
    # ≤ 2 проверок на TTL (0,5 с) за 1,2 с, плюс первая от «A»
    assert calls["n"] <= 6, calls["n"]


def test_an_open_answer_is_trusted_only_while_fresh():
    """Ревью #644, N2: закрытое руками окно не пускает профиль без памяти."""
    m = Machine(open_profiles={"p2"})
    slots = m.slots()
    slots.acquire("A", "p1")
    m.available_gb = 2.0
    assert slots._is_open("p2") is True
    m.open.discard("p2")                      # окно закрыли руками
    m.now += ps.OPEN_TTL_S + 0.1              # старый «открыт» уже не верим
    assert slots._is_open("p2") is False      # проверка перечиталась


def test_job_started_ahead_and_telemetry_carry_the_flags(monkeypatch):
    """Ревью #644, N4: `job["started_ahead"]` и строка `slot_queue`."""
    import contextlib

    from src.fb_ads_rpa import run_telemetry as rt

    m = Machine(open_profiles={"Doc19"})
    slots = m.slots()
    slots.acquire("A", "p1")
    m.available_gb = 5.0
    head = Job(slots, "H", "pH")              # голова ждёт память
    assert _until(lambda: head.holds)
    rows = []
    monkeypatch.setattr(rt, "record", lambda step, ok=None, **kw: rows.append((step, kw)))
    monkeypatch.setattr(pj, "_slots", slots)
    monkeypatch.setattr(pj, "use_rpa_publish", lambda: True)
    monkeypatch.setattr(pj, "_clear_inherited_asyncio_loop", lambda: None)
    monkeypatch.setattr(pj, "keep_display_awake", lambda *a, **k: contextlib.nullcontext())
    monkeypatch.setattr(pj, "_watch_screen_lock", lambda *a, **k: contextlib.nullcontext())
    monkeypatch.setattr(pj, "profile_key_for", lambda label: label)
    monkeypatch.setattr(pj, "_run_rpa", lambda job_id, slots_, **kw: pj._finish(job_id))
    job_id = pj.create_job("Doc19", "CL", [{}])
    try:
        pj._run_job(job_id, [], account_label="Doc19", country="CL", via_multilogin=True,
                    activate=False, dry_run=False, hand_off=True)
        assert pj._jobs[job_id]["started_ahead"] is True
        diag = next(kw["diag"] for _, kw in rows if kw.get("note") == "slot_queue")
        assert diag["profile_open"] is True and diag["started_ahead"] is True
        assert diag["decision"]["reason"] == "profile_open"
    finally:
        with pj._jobs_lock:
            pj._jobs.pop(job_id, None)
        head.cancel(slots)


def test_queue_view_tells_jobs_behind_a_memory_blocked_head():
    view = pj._queue_view({"reason": "order", "behind_memory": True, "ahead": 1})
    assert view["state"] == "waiting_memory" and view["behind_memory"] is True
    plain = pj._queue_view({"reason": "order", "ahead": 1})
    assert plain["state"] == "waiting_slot"


def test_a_job_behind_a_memory_blocked_head_says_so():
    m = Machine()
    slots = m.slots()
    slots.acquire("A", "p1")
    m.available_gb = 5.0
    _enqueue(slots, ("H", "p2"), ("X", "p3"))
    with slots._cond:
        verdict = slots._verdict("X")
    assert verdict["reason"] == "order" and verdict["behind_memory"] is True
