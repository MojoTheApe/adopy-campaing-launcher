# -*- coding: utf-8 -*-
"""ACL-2790: профиль, поднятый чтением, не закрывается, пока на аккаунте стоит действие.

Первая часть — решение «закрыть / оставить» на подменённой очереди. Вторая (раунд 3) — живая
`ProfileSlots` с `kept_idle_close_s` по умолчанию (0 с — ACL-2686) и потолком 2: окно, оставленное «для
удобства» после API-чтения или команды покупателя, уступает место любому заданию сразу.
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.analysis_agent import account_spend as sp  # noqa: E402
from src.fb_ads_rpa import browser as rpa_browser  # noqa: E402
from tests.profile_lease_fakes import fake_profile  # noqa: E402

PID = fake_profile("acl2790")
ACC = "Acc 1"


class Queue:
    def __init__(self):
        self.kept = []
        self.released = []
        self.preemptible = None

    def keep(self, slot_id, label="", kind="review", *, preemptible=False):
        self.kept.append((slot_id, kind))
        self.preemptible = preemptible

    def release(self, slot_id):
        self.released.append(slot_id)

    def others_on(self, *a, **k):
        return False

    def grants(self, *a, **k):
        return 0


@pytest.fixture
def world(monkeypatch):
    q = Queue()
    stops = []
    monkeypatch.setattr(sp, "_slot_queue", lambda: q)
    monkeypatch.setattr(sp, "_record", lambda *a, **k: None)
    monkeypatch.setattr(sp, "_profile_key", lambda account, *a, **k: PID)
    monkeypatch.setattr(sp, "may_open_closed_profile", lambda: False)
    monkeypatch.setattr(rpa_browser, "stop_profile_on_request", lambda entry, account: stops.append(account))
    from src.fb_api import stop_log
    monkeypatch.setattr(stop_log, "record_stop", lambda pid: None)
    sp._QUEUED_ACTIONS.clear()
    yield q, stops
    sp._QUEUED_ACTIONS.clear()


def opened(api=True):
    return sp._ReadOpen(account=ACC, started=True, slot_id="read:1", profile_id=PID, entry=object(),
                        started_mono=0.0, api=api)


def test_queued_action_blocks_the_close(world):
    q, stops = world
    with sp.action_queued(ACC):
        o = opened()
        sp._finish_started(o)
    assert stops == []
    assert q.kept == [("read:1", "convenience")] and q.released == []
    assert q.preemptible is True, "kept for convenience only: any job needing the slot evicts it"
    assert o.started is False


def test_queued_action_blocks_the_deferred_close_too(world):
    q, stops = world
    o = opened()
    with sp.action_queued(ACC):
        sp._close_started(o)
    assert stops == [] and q.kept == [("read:1", "convenience")]


def test_idle_profile_is_still_closed(world):
    q, stops = world
    sp._finish_started(opened())
    assert stops == [ACC] and q.kept == []


def test_close_resumes_after_the_action_ends(world):
    _q, stops = world
    with sp.action_queued(ACC):
        assert sp.action_pending(PID)
    assert not sp.action_pending(PID)
    sp._finish_started(opened())
    assert stops == [ACC]


def test_grace_end_keeps_the_window_when_an_action_waits(world, monkeypatch):
    q, stops = world
    o = opened()
    sp._PENDING_CLOSE[PID] = o
    monkeypatch.setattr(sp, "profile_lock", lambda key: __import__("contextlib").nullcontext())
    with sp.action_queued(ACC):
        sp._close_if_still_pending(o)
    assert stops == [] and q.kept == [("read:1", "convenience")]


def test_counter_is_per_profile_and_nests(world):
    with sp.action_queued(ACC), sp.action_queued(ACC):
        assert sp.action_pending(PID)
    assert sp._QUEUED_ACTIONS == {}
    assert not sp.action_pending("")


def test_rpa_read_closes_even_with_an_action_queued(world):
    """Путь RPA не меняется: окно чтения RPA закрывается, как раньше."""
    q, stops = world
    with sp.action_queued(ACC):
        sp._finish_started(opened(api=False))
    assert stops == [ACC] and q.kept == []


def test_buyer_api_read_keeps_a_preemptible_window_with_nothing_queued(world, monkeypatch):
    q, stops = world
    monkeypatch.setattr(sp, "may_open_closed_profile", lambda: True)
    sp._finish_started(opened(api=False), api=True)
    assert stops == [] and q.kept == [("read:1", "convenience")] and q.preemptible is True


def test_failed_buyer_api_read_still_closes(world, monkeypatch):
    q, stops = world
    monkeypatch.setattr(sp, "may_open_closed_profile", lambda: True)
    sp._finish_started(opened(api=False), api=True, problem=RuntimeError("boom"))
    assert stops == [ACC] and q.kept == []


def test_a_queue_that_cannot_keep_falls_back_to_the_close(world, monkeypatch):
    q, stops = world

    def broken(*a, **k):
        raise RuntimeError("queue gone")

    monkeypatch.setattr(q, "keep", broken)
    with sp.action_queued(ACC):
        sp._finish_started(opened())
    assert stops == [ACC], "a window the queue does not see is never left open"


# ───────────────── раунд 3: живая очередь, `kept_idle_close_s` по умолчанию ─────────────────

def _live_rig(monkeypatch, tmp_path, accounts):
    """Стенд test_acl1760 + настоящая `ProfileSlots` (потолок 2, 0 с) и вытеснитель как в проде.

    Вытеснитель, как `publish_jobs._evict_kept_window`, берёт замок профиля без ожидания:
    окно, которым ещё пользуется чтение/команда, закрыть нельзя («busy»).
    Память: `rig.low` — пока открыт хоть один профиль, свободно 500 МБ.
    """
    import dataclasses
    from types import SimpleNamespace

    from src.analysis_agent import account_spend_api as api
    from src.fb_ads_rpa.operations import profile_lock
    from src.fb_api import profile as prof
    from src.web import profile_slots as ps
    from tests.test_acl1760_api_read import ACT, World

    World(monkeypatch, tmp_path)
    from src.fb_api import backend

    monkeypatch.setenv(backend.BACKEND_ENV, "api")   # ACL-2785: the default backend (the stand sets rpa)
    settings = dataclasses.replace(ps.load_settings({}), warmup_s=0, ceiling=2)
    assert settings.kept_idle_close_s == ps.DEFAULT_KEPT_IDLE_CLOSE_S == 0.0   # ACL-2686: a kept window yields at once
    rig = SimpleNamespace(open=set(), starts=[], evicted=[], busy=[], settings=settings, api=api, low=False, tight=False)
    usage = ps.ProfileUsage(lambda profiles: {p: 3000 for p in profiles if p in rig.open},
                            clock=lambda: 1000.0, background=False)

    def evictor(job_id, label, profile, idle_s):
        lease = profile_lock(profile)
        if not lease.acquire(blocking=False):
            rig.busy.append(label)
            return False
        try:
            rig.open.discard(profile)
            rig.evicted.append(label)
            return True
        finally:
            lease.release()

    def memory():
        return ps.MemoryReading(total_mb=64000, available_mb=500 if (rig.low and rig.open) or (rig.tight and len(rig.open) >= 2) else 60000)

    ticks = iter(range(10 ** 9))
    rig.queue = ps.ProfileSlots(memory=memory, settings=lambda: settings,
                                clock=lambda: 1000.0 + next(ticks) * 0.001, tick_s=0.05,
                                usage=usage, evictor=evictor)
    monkeypatch.setattr(sp, "_slot_queue", lambda: rig.queue)
    profiles = {name: SimpleNamespace(profile_id=fake_profile(name), folder_id="", multilogin_instance="A",
                                      ad_account_id=ACT) for name in accounts}
    rig.pid = {name: e.profile_id for name, e in profiles.items()}
    monkeypatch.setattr(sp, "_entry_for", lambda account: profiles.get(account))
    monkeypatch.setattr(api, "_probe_local", lambda pid: ("port", 9222) if pid in rig.open else ("none", None))

    def start(pid, folder, instance):
        fresh = pid not in rig.open
        if fresh:
            rig.open.add(pid)
            rig.starts.append(pid)
        return prof.OpenedProfile(pid, folder, instance, "9222", None, fresh, None, started_now=fresh)

    monkeypatch.setattr(api, "_open_profile", start)
    from src.fb_api import token_capture as tc

    # Команды строят Graph с `commands_act` (ACL-1800); вызовов к Meta тест не делает.
    monkeypatch.setattr(api, "_make_graph", lambda tab, **kw: tc.InPageGraph(tab, sleep=lambda s: None))
    sp._QUEUED_ACTIONS.clear()
    return rig


def _command(account):
    """Команда, как её запускает дешборд (preview/execute): `campaign_commands.open_session`."""
    from src.web import campaign_commands

    with campaign_commands.open_session(account, commands=True):
        pass


def _in_thread(fn, timeout=20.0):
    out: dict = {}

    def run():
        try:
            out["value"] = fn()
        except BaseException as exc:   # noqa: BLE001 — показать в тесте
            out["error"] = exc

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout)
    assert not t.is_alive(), "blocked waiting for a slot"
    if "error" in out:
        raise out["error"]
    return out.get("value")


def _read(rig, account):
    from tests.test_acl1760_api_read import D

    return rig.api.read_via_api(account, D, D).state


def _kept(rig):
    return [r for r in rig.queue.snapshot()["running"] if r["kept"]]


def test_live_two_commands_then_read_all_never_blocks(monkeypatch, tmp_path):
    """(a) Команды на Acc1, Acc2, потом Read all Acc3..Acc5 — ни одно чтение не ждёт 600 с."""
    rig = _live_rig(monkeypatch, tmp_path, [f"Acc{i}" for i in range(1, 6)])
    for name in ("Acc1", "Acc2"):
        _in_thread(lambda name=name: _command(name))
    assert len(_kept(rig)) == 2 and rig.evicted == [], "both command windows kept for convenience"

    results: dict[str, str] = {}
    rig.tight = True   # ACL-2686: kept windows take no slot; only a memory shortage closes them

    def read_all():
        threads = [threading.Thread(target=lambda n=n: results.__setitem__(n, _read(rig, n)), daemon=True)
                   for n in ("Acc3", "Acc4", "Acc5")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(20)

    _in_thread(read_all, timeout=25)
    assert results == {"Acc3": "ok", "Acc4": "ok", "Acc5": "ok"}
    assert {"Acc1", "Acc2"} <= set(rig.evicted), "kept windows evicted at once, no idle wait"
    assert len(rig.open) <= rig.settings.ceiling


def test_live_read_then_command_reuses_the_window(monkeypatch, tmp_path):
    """(b) Read Acc1, потом команда на Acc1 (нажата после чтения, ничего не стояло) — без повторного запуска."""
    rig = _live_rig(monkeypatch, tmp_path, ["Acc1"])
    assert _in_thread(lambda: _read(rig, "Acc1")) == "ok"
    assert rig.starts == [rig.pid["Acc1"]] and rig.pid["Acc1"] in rig.open
    _in_thread(lambda: _command("Acc1"))
    assert rig.starts == [rig.pid["Acc1"]], "the command attached to the kept window, no reopen"
    assert rig.evicted == []


def test_live_memory_pressure_closes_a_preemptible_window(monkeypatch, tmp_path):
    """(c) Нехватка памяти: новое задание закрывает «уступающее» окно сразу (не через 600 с)."""
    rig = _live_rig(monkeypatch, tmp_path, ["Acc1", "Acc2"])
    assert _in_thread(lambda: _read(rig, "Acc1")) == "ok"
    assert rig.evicted == [] and len(_kept(rig)) == 1
    rig.low = True

    def need_slot():
        try:
            return rig.queue.acquire("publish-acl2790", rig.pid["Acc2"])
        finally:
            rig.queue.release("publish-acl2790")

    grant = _in_thread(need_slot)
    assert rig.evicted == ["Acc1"], "the preemptible window was closed for memory"
    assert grant["closed_for_room"] and grant["closed_for_room"][0]["label"] == "Acc1"


def test_live_round2_repro_action_sessions_then_plain_read(monkeypatch, tmp_path):
    """(d) Ревью раунда 2: две сессии внутри `action_queued` (Acc1, Acc2), потом чтение Acc3 — не ждёт."""
    from src.fb_api.connection import user_started_action

    rig = _live_rig(monkeypatch, tmp_path, ["Acc1", "Acc2", "Acc3"])

    def action_session(name):
        with sp.action_queued(name), user_started_action(), rig.api._session(name, commands=True):
            pass

    for name in ("Acc1", "Acc2"):
        _in_thread(lambda name=name: action_session(name))
    assert len(_kept(rig)) == 2
    rig.tight = True   # ACL-2686: kept windows take no slot; only a memory shortage closes them
    assert _in_thread(lambda: _read(rig, "Acc3"), timeout=10) == "ok"
    assert rig.evicted == ["Acc1"], "the least recently used kept window made room"


def test_live_window_still_under_its_lock_is_retried_not_parked(monkeypatch, tmp_path):
    """Окно ещё под замком профиля — следующий круг пробует снова, а не ждёт
    `EVICT_RETRY_S` (60 с) или `EVICT_GAP_S` (10 с)."""
    from src.fb_ads_rpa.operations import profile_lock

    rig = _live_rig(monkeypatch, tmp_path, ["Acc1", "Acc2", "Acc3"])
    for name in ("Acc1", "Acc2"):
        assert _in_thread(lambda name=name: _read(rig, name)) == "ok"
    free = threading.Event()
    held = []

    def holder(pid):
        with profile_lock(pid):
            held.append(pid)
            free.wait(10)

    for name in ("Acc1", "Acc2"):
        threading.Thread(target=holder, args=(rig.pid[name],), daemon=True).start()
    while len(held) < 2:
        threading.Event().wait(0.01)
    # `threading.Timer` стенд World подменяет — освобождаем замки обычным потоком.
    threading.Thread(target=lambda: (threading.Event().wait(0.5), free.set()), daemon=True).start()
    rig.tight = True   # ACL-2686: kept windows take no slot; only a memory shortage closes them
    assert _in_thread(lambda: _read(rig, "Acc3"), timeout=8) == "ok"
    assert rig.busy and rig.evicted, "tried while busy, closed as soon as the lock was free"


def test_explicit_rpa_backend_keeps_the_old_close(world, monkeypatch):
    """ACL-2785: бэкенд — из `src.fb_api.backend`; явный `rpa` — окна не оставляются."""
    from src.fb_api import backend

    q, stops = world
    monkeypatch.setattr(sp, "may_open_closed_profile", lambda: True)
    monkeypatch.setenv(backend.BACKEND_ENV, "rpa")
    with sp.action_queued(ACC):
        sp._finish_started(opened(api=False), api=True)
    assert stops == [ACC] and q.kept == []
