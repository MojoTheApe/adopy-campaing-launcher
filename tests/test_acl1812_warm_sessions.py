"""ACL-1812: тёплые сессии — профили открыты весь день, у каждого одна вкладка робота.

Только подделки: ни Meta, ни Multilogin, ни настоящий CDP. Проверяется:
три действия берут одну вкладку без перезагрузки; мёртвая или зависшая вкладка
заменяется один раз; вход и checkpoint останавливают действие с прежней
причиной; замок профиля не пускает два действия в одну вкладку; открытые
«тёплые» профили не занимают ворота памяти; открытое руками окно не закрывается
никогда; перезапуск раньше 3 минут отказывает; «Close profiles» останавливает
только профили, которые поднял дешборд; состояние строк не зовёт ни Meta, ни MLX,
ни CDP; при `rpa` всё выключено.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.analysis_agent import account_spend as sp  # noqa: E402
from src.analysis_agent import account_spend_api as api  # noqa: E402
from src.fb_api import graph as g  # noqa: E402
from src.fb_api import profile as prof  # noqa: E402
from src.fb_api import token_capture as tc  # noqa: E402
from src.fb_api import warm  # noqa: E402
from src.fb_api.cdp import CdpError, PageTab  # noqa: E402
from src.web import profile_slots as ps  # noqa: E402

ACT = "act_123456789"
PID = "550e8400-e29b-41d4-a716-446655440000"
ADS_URL = "https://adsmanager.facebook.com/adsmanager/manage/campaigns?act=123456789"
LOGIN_URL = "https://www.facebook.com/login/?next=%2Fadsmanager&access_token=SECRETVALUE456"
CHECKPOINT_URL = "https://www.facebook.com/checkpoint/1501092823525282/"
OK_STATE = {"kind": "ads_manager", "has_token": True, "source": "network"}


# ───────────────────────── подделки ─────────────────────────

class FakeTab:
    def __init__(self, n: int):
        self.target_id = f"T{n}"
        self.url = ""
        self.closed = False
        self.navigations = 0
        self.reloads = 0
        self.live = "ok"        # ok | hung | error
        self.closed_targets: list[str] = []

    def browser_url(self, timeout=5.0):
        return "" if self.closed else self.url

    def close(self):
        self.closed = True

    def close_target(self, target_id):
        self.closed_targets.append(target_id)
        return True


class FakeGraph:
    def __init__(self, tab: FakeTab):
        self.tab = tab
        self.fresh_state = dict(OK_STATE)

    def install(self):
        pass

    def navigate(self, act, timeout=60.0):
        self.tab.navigations += 1
        self.tab.url = f"https://adsmanager.facebook.com/adsmanager/manage/campaigns?act={act[4:]}"

    def wait_ready(self, timeout=90.0):
        return dict(self.fresh_state)

    def live_state(self, timeout=10.0):
        if self.tab.live == "hung":
            raise g.PageUnresponsive("the Ads Manager tab stopped answering")
        if self.tab.live == "error":
            raise CdpError("Runtime.evaluate: target closed")
        return dict(OK_STATE)

    def reacquire(self, timeout=90.0):
        self.tab.reloads += 1
        return dict(OK_STATE)


class Rig:
    """Реестр с подставными процессами, вкладками, MLX и часами."""

    def __init__(self, tmp_path, *, limit=1, memory=None, usage=None):
        self.tabs: list[FakeTab] = []
        self.scan_result: dict = {}
        self.started: list[tuple] = []
        self.stopped: list[str] = []
        self.start_started_now = True
        self.start_error: Exception | None = None
        self.now = [1_800_000_000.0]
        self.sleeps: list[float] = []
        self.slot_calls: list[tuple] = []
        self.grant_ok = True
        self.store = warm.WarmStore(tmp_path / "warm_sessions.json")
        self.gate = warm.HeavyGate(
            limit=lambda: limit, memory=memory or (lambda: ps.MemoryReading(64000, 40000)),
            settings=lambda: ps.SlotSettings(), usage=usage if usage is not None else FakeUsage(), tick_s=0.01)
        self.reg = warm.WarmRegistry(
            store=self.store, wall=lambda: self.now[0], scan=self._scan, tab_factory=self._tab,
            start_profile=self._start, close_window=self._stop, slot_queue=lambda: FakeQueue(self),
            gate=self.gate, sleep=self.sleeps.append, stagger=lambda: 7.0)

    def _scan(self, ids):
        return {pid: self.scan_result.get(pid, ("none", None)) for pid in ids}

    def _tab(self, url):
        tab = FakeTab(len(self.tabs) + 1)
        self.tabs.append(tab)
        return tab

    def _start(self, pid, folder, instance):
        if self.start_error:
            raise self.start_error
        self.started.append((pid, folder, instance))
        self.scan_result[pid] = ("port", 9333)
        return prof.OpenedProfile(pid, folder, instance, "9333", None, self.start_started_now, None,
                                  started_now=self.start_started_now)

    def _stop(self, entry, label):
        self.stopped.append(label)
        self.scan_result[entry.profile_id] = ("none", None)

    def make(self, tab):
        graph = FakeGraph(tab)
        self.last_graph = graph
        return graph

    def checkout(self, pid=PID, *, act=ACT, mode="read", account="Acc"):
        return self.reg.checkout(pid, account=account, act=act, mode=mode, make_graph=self.make)

    def warm_up(self, pid=PID, *, port=9333, dashboard=True, account="Acc"):
        self.scan_result[pid] = ("port", port)
        return self.reg.register(pid, account=account, port=port, dashboard_started=dashboard)


class FakeUsage:
    def __init__(self, per_profile=None):
        self.per_profile = per_profile

    def read(self, profiles):
        if self.per_profile is None:
            return {"status": "pending"}
        return {"status": "ok", "per_profile": {p: self.per_profile for p in profiles}}


class FakeQueue:
    def __init__(self, rig):
        self.rig = rig

    def acquire(self, job_id, profile, *, cancelled=None, on_wait=None, share_profile=False):
        self.rig.slot_calls.append(("acquire", job_id, profile))
        return {"shared": False} if self.rig.grant_ok else None

    def release(self, job_id):
        self.rig.slot_calls.append(("release", job_id))


def entry(pid=PID, label="Acc"):
    return SimpleNamespace(profile_id=pid, folder_id="F", multilogin_instance="A", ad_account_id=ACT)


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


# ───────────────────────── одна вкладка на все действия ─────────────────────────

def test_three_actions_reuse_one_tab_without_reload(rig):
    rig.warm_up()
    for mode in ("read", "commands:" + ACT, "read"):
        lease = rig.checkout(mode=mode)
        lease.release()
    assert len(rig.tabs) == 1, "one robot tab for all three actions"
    tab = rig.tabs[0]
    assert tab.navigations == 1, "Ads Manager is loaded once, not before every action"
    assert tab.reloads == 0 and not tab.closed
    assert rig.gate.active() == []


def test_tab_is_not_closed_after_the_action_and_profile_is_not_stopped(rig):
    rig.warm_up()
    rig.checkout().release()
    assert rig.tabs[0].closed is False
    assert rig.stopped == [] and rig.started == []


def test_other_ad_account_in_the_same_profile_loads_once_more(rig):
    rig.warm_up()
    rig.checkout(act=ACT).release()
    rig.checkout(act="act_987654321").release()
    assert len(rig.tabs) == 1 and rig.tabs[0].navigations == 2


# ───────────────────────── мёртвая и зависшая вкладка ─────────────────────────

def test_dead_tab_is_replaced_once(rig):
    rig.warm_up()
    rig.checkout().release()
    first = rig.tabs[0]
    first.closed = True              # цель исчезла: браузер не знает такой вкладки
    rig.checkout().release()
    assert len(rig.tabs) == 2 and rig.tabs[1].navigations == 1
    rig.checkout().release()         # и дальше живёт уже новая
    assert len(rig.tabs) == 2


def test_dead_tab_is_not_replaced_twice_when_the_new_one_fails(rig):
    rig.warm_up()
    rig.checkout().release()
    rig.tabs[0].live = "error"
    real_make = rig.make

    def make(tab):
        graph = real_make(tab)
        graph.fresh_state = {"kind": "other", "has_token": False}   # новая тоже без Graph-сессии
        return graph

    rig.make = make
    with pytest.raises(g.PageFailed):
        rig.checkout()
    assert len(rig.tabs) == 2, "exactly one replacement, no loop"
    assert rig.gate.active() == []


def test_hung_tab_is_replaced_once(rig):
    rig.warm_up()
    rig.checkout().release()
    rig.tabs[0].live = "hung"
    rig.checkout().release()
    assert len(rig.tabs) == 2 and rig.tabs[0].closed


def test_tab_on_a_foreign_page_is_replaced(rig):
    rig.warm_up()
    rig.checkout().release()
    rig.tabs[0].url = "about:blank"
    rig.checkout().release()
    assert len(rig.tabs) == 2


# ───────────────────────── вход и checkpoint ─────────────────────────

def test_logged_out_tab_stops_with_its_reason_and_is_not_replaced(rig):
    rig.warm_up()
    rig.checkout().release()
    rig.tabs[0].url = LOGIN_URL
    with pytest.raises(g.LoggedOut) as ei:
        rig.checkout()
    assert "SECRETVALUE" not in str(ei.value)
    assert len(rig.tabs) == 1, "a login page is a stop, not a reason to open another tab"
    assert rig.gate.active() == []
    assert rig.reg.session(PID).logged_out is True


def test_checkpoint_tab_stops(rig):
    rig.warm_up()
    rig.checkout().release()
    rig.tabs[0].url = CHECKPOINT_URL
    with pytest.raises(g.Checkpoint) as ei:
        rig.checkout()
    assert "1501092823525282" not in str(ei.value)
    assert len(rig.tabs) == 1


def test_fresh_tab_that_lands_on_login_stops_without_retry(rig):
    rig.warm_up()
    real_make = rig.make

    def make(tab):
        graph = real_make(tab)
        graph.fresh_state = {"kind": "login", "has_token": False}
        return graph

    rig.make = make
    with pytest.raises(g.LoggedOut):
        rig.checkout()
    assert len(rig.tabs) == 1


def test_session_applies_the_existing_cooldown_for_a_logged_out_tab(monkeypatch, tmp_path):
    rig = Rig(tmp_path)
    rig.warm_up()
    rig.checkout().release()
    rig.tabs[0].url = LOGIN_URL
    monkeypatch.setattr(warm, "_REGISTRY", rig.reg)
    ent = SimpleNamespace(profile_id=PID, folder_id="", multilogin_instance="A", ad_account_id=ACT)
    monkeypatch.setattr(sp, "_entry_for", lambda account: ent)
    monkeypatch.setattr(sp, "_set_wait_note", lambda *a: None)
    monkeypatch.setattr(api, "_make_graph", lambda tab, **kw: rig.make(tab))
    api._COOLDOWN.clear()
    with pytest.raises(g.LoggedOut):
        with api._session("Acc"):
            pytest.fail("the body must not run on a logged-out tab")
    left, why = api._cooldown_state(sp._profile_key("Acc"))
    assert why == "logged_out" and left > 0
    # Следующий заход отказывает ДО вкладки, текстом о входе.
    with pytest.raises(api.ApiReadError) as ei:
        with api._session("Acc"):
            pass
    assert ei.value.code == sp.LOGGED_OUT_CODE


# ───────────────────────── замок профиля ─────────────────────────

def test_profile_lock_serialises_actions_on_one_tab(rig):
    rig.warm_up()
    inside = []
    overlap = []

    def action(name):
        lease = rig.checkout(account=name)
        inside.append(name)
        overlap.append(len(inside))
        time.sleep(0.15)
        inside.remove(name)
        lease.release()

    threads = [threading.Thread(target=action, args=(f"A{i}",)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert overlap == [1, 1, 1], "two actions were inside one tab at the same time"
    assert len(rig.tabs) == 1


def test_nested_checkout_in_the_same_thread_is_refused_not_deadlocked(rig):
    rig.warm_up()
    lease = rig.checkout()
    with pytest.raises(warm.WarmError) as ei:
        rig.checkout()
    assert ei.value.code == "nested"
    lease.release()


# ───────────────────────── ворота памяти ─────────────────────────

def test_idle_warm_profiles_do_not_block_the_gate(tmp_path, monkeypatch):
    monkeypatch.setenv(warm.GATE_WAIT_ENV, "2")      # зависание не должно ждать 5 минут
    rig = Rig(tmp_path, limit=1)
    pids = [f"{i:08d}-0000-4000-8000-000000000000" for i in range(6)]
    for pid in pids:
        rig.warm_up(pid)
    assert rig.gate.active() == []
    started = time.monotonic()
    lease = rig.checkout(pids[0])
    assert time.monotonic() - started < 1.0, "an action waited behind idle warm profiles"
    assert rig.gate.active() == [pids[0]]
    lease.release()


def test_one_heavy_action_at_a_time_by_default(tmp_path):
    rig = Rig(tmp_path, limit=1)
    a, b = "a" * 8 + "-0000-4000-8000-000000000000", "b" * 8 + "-0000-4000-8000-000000000000"
    rig.warm_up(a)
    rig.warm_up(b)
    first = rig.checkout(a)
    got = []
    t = threading.Thread(target=lambda: got.append(rig.checkout(b)))
    t.start()
    time.sleep(0.2)
    assert got == [], "the second heavy action must wait while the first one runs"
    first.release()
    t.join(3)
    assert len(got) == 1
    got[0].release()


def test_two_heavy_actions_when_configured_and_memory_is_free(tmp_path):
    rig = Rig(tmp_path, limit=2, memory=lambda: ps.MemoryReading(64000, 40000))
    a, b = "a" * 8 + "-0000-4000-8000-000000000000", "b" * 8 + "-0000-4000-8000-000000000000"
    rig.warm_up(a)
    rig.warm_up(b)
    first, second = rig.checkout(a), rig.checkout(b)
    assert sorted(rig.gate.active()) == [a, b]
    first.release()
    second.release()


def test_second_action_waits_for_memory_and_uses_the_measured_profile_size(tmp_path):
    free = [4000]
    rig = Rig(tmp_path, limit=3, memory=lambda: ps.MemoryReading(16000, free[0]),
              usage=FakeUsage(per_profile=6000))
    a, b = "a" * 8 + "-0000-4000-8000-000000000000", "b" * 8 + "-0000-4000-8000-000000000000"
    rig.warm_up(a)
    rig.warm_up(b)
    first = rig.checkout(a)
    free[0] = 7000                          # хватило бы по оценке 3,5 ГБ + запас, но замер — 6 ГБ
    got = []
    t = threading.Thread(target=lambda: got.append(rig.checkout(b)))
    t.start()
    time.sleep(0.2)
    assert got == [], "the measured size (6 GB + reserve) was ignored"
    free[0] = 12000
    t.join(3)
    assert len(got) == 1
    got[0].release()
    first.release()


def test_gate_wait_times_out_with_a_clear_message(tmp_path, monkeypatch):
    monkeypatch.setenv(warm.GATE_WAIT_ENV, "1")
    rig = Rig(tmp_path, limit=1)
    a, b = "a" * 8 + "-0000-4000-8000-000000000000", "b" * 8 + "-0000-4000-8000-000000000000"
    rig.warm_up(a)
    rig.warm_up(b)
    first = rig.checkout(a)
    with pytest.raises(warm.WarmError) as ei:
        rig.checkout(b)
    assert ei.value.code == "gate_wait" and "Nothing was called" in str(ei.value)
    first.release()


def test_max_heavy_is_clamped_to_one_to_three(monkeypatch):
    for raw, want in [("", 1), ("0", 1), ("2", 2), ("3", 3), ("9", 3), ("x", 1)]:
        monkeypatch.setenv(warm.MAX_HEAVY_ENV, raw)
        assert warm.max_heavy() == want, raw


# ───────────────────────── окно, открытое руками ─────────────────────────

def test_hand_opened_profile_is_never_closed(rig):
    rig.scan_result[PID] = ("no_port", None)
    res = rig.reg.close_one("Acc", entry())
    assert res["ok"] is False and res["code"] == "open_by_hand"
    assert rig.stopped == []
    # Даже если запись о нём есть в реестре (профиль потом переоткрыли руками).
    rig.reg.register(PID, account="Acc", port=9333, dashboard_started=True)
    res = rig.reg.close_one("Acc", entry())
    assert res["ok"] is False and rig.stopped == []


def test_open_on_a_hand_opened_profile_says_what_to_do_and_starts_nothing(rig):
    rig.scan_result[PID] = ("no_port", None)
    res = rig.reg.open_one("Acc", entry())
    assert res["ok"] is False and res["code"] == "open_by_hand"
    assert res["message"] == warm.HAND_MESSAGE
    assert rig.started == [] and rig.stopped == []


def test_hand_opened_profile_shows_the_hand_chip(rig):
    rig.scan_result[PID] = ("no_port", None)
    chip = rig.reg.states([("Acc", PID)])["Acc"]
    assert chip["state"] == warm.BY_HAND and chip["text"] == "Opened by hand"
    assert chip["title"] == warm.HAND_MESSAGE


# ───────────────────────── перезапуск не раньше 3 минут ─────────────────────────

def test_restart_cooldown_refuses_a_start_inside_three_minutes(rig):
    rig.reg.record_stop(PID)
    rig.now[0] += 100
    res = rig.reg.open_one("Acc", entry())
    assert res["ok"] is False and res["code"] == "restart_cooldown"
    assert "2 min" in res["message"] or "1 min" in res["message"]
    assert rig.started == [] and rig.slot_calls == [], "no memory slot, no MLX call"
    rig.now[0] += 81                      # всего 181 с
    res = rig.reg.open_one("Acc", entry())
    assert res["ok"] is True and rig.started == [(PID, "F", "A")]


def test_cooldown_survives_a_dashboard_restart(rig, tmp_path):
    rig.warm_up()
    rig.reg.close_one("Acc", entry())
    again = warm.WarmRegistry(store=rig.store, wall=lambda: rig.now[0], scan=rig._scan,
                              start_profile=rig._start, slot_queue=lambda: FakeQueue(rig),
                              gate=rig.gate, sleep=rig.sleeps.append)
    assert again.restart_wait_s(PID) > 170


def test_cooldown_is_configurable(rig, monkeypatch):
    monkeypatch.setenv(warm.RESTART_COOLDOWN_ENV, "10")
    rig.reg.record_stop(PID)
    rig.now[0] += 11
    assert rig.reg.restart_wait_s(PID) == 0


def test_a_profile_seen_gone_starts_the_cooldown(rig):
    rig.warm_up()
    rig.scan_result[PID] = ("none", None)       # человек закрыл окно
    chip = rig.reg.states([("Acc", PID)])["Acc"]
    assert chip["state"] == warm.CLOSED and "again in about" in chip["title"]
    assert rig.reg.restart_wait_s(PID) > 170
    assert rig.reg.session(PID) is None


# ───────────────────────── открыть профили ─────────────────────────

def test_open_marks_the_profile_dashboard_started_and_frees_the_memory_slot(rig):
    res = rig.reg.open_one("Acc", entry())
    assert res["ok"] and res["started"] and res["code"] == "started"
    sess = rig.reg.session(PID)
    assert sess.dashboard_started is True and sess.port == 9333
    assert [c[0] for c in rig.slot_calls] == ["acquire", "release"], "an idle warm profile keeps no slot"


def test_open_releases_the_slot_when_mlx_fails(rig):
    rig.start_error = RuntimeError("MLX is down access_token=EAAB" + "x" * 30)
    res = rig.reg.open_one("Acc", entry())
    assert res["ok"] is False and res["code"] == "start_failed"
    assert "EAAB" not in res["message"]
    assert [c[0] for c in rig.slot_calls] == ["acquire", "release"]
    assert rig.reg.session(PID) is None


def test_open_without_memory_is_refused_politely(rig):
    rig.grant_ok = False
    res = rig.reg.open_one("Acc", entry())
    assert res["ok"] is False and res["code"] == "memory_low" and rig.started == []


def test_already_open_profile_is_kept_warm_but_not_marked_as_ours(rig):
    rig.scan_result[PID] = ("port", 9444)
    res = rig.reg.open_one("Acc", entry())
    assert res["ok"] and res["code"] == "already_open" and rig.started == []
    assert rig.reg.session(PID).dashboard_started is False


def test_starts_are_staggered_only_between_real_starts(rig):
    pids = [f"{i:08d}-0000-4000-8000-000000000000" for i in range(4)]
    rig.scan_result[pids[1]] = ("port", 9555)       # этот уже открыт: пауза после него не нужна
    items = [(f"A{i}", entry(pid)) for i, pid in enumerate(pids)]
    results = rig.reg.open_many(items)
    assert [r["code"] for r in results.values()] == ["started", "already_open", "started", "started"]
    assert rig.sleeps == [7.0, 7.0], rig.sleeps    # после 1-го и после 3-го настоящего запуска


def test_default_stagger_is_between_five_and_ten_seconds(tmp_path):
    reg = warm.WarmRegistry(store=warm.WarmStore(tmp_path / "w.json"), scan=lambda ids: {})
    values = [reg._stagger() for _ in range(200)]
    assert min(values) >= 5.0 and max(values) <= 10.0 and len(set(values)) > 1


# ───────────────────────── Close profiles ─────────────────────────

def test_close_stops_only_profiles_the_dashboard_started(rig):
    ours = "a" * 8 + "-0000-4000-8000-000000000000"
    adopted = "b" * 8 + "-0000-4000-8000-000000000000"
    hand = "c" * 8 + "-0000-4000-8000-000000000000"
    stranger = "d" * 8 + "-0000-4000-8000-000000000000"
    restarted = "e" * 8 + "-0000-4000-8000-000000000000"
    rig.warm_up(ours, port=9001)
    rig.warm_up(adopted, port=9002, dashboard=False)
    rig.scan_result[hand] = ("no_port", None)
    rig.scan_result[stranger] = ("port", 9004)      # открыт с портом, но реестр о нём не знает
    rig.warm_up(restarted, port=9005)
    rig.scan_result[restarted] = ("port", 9999)     # перезапущен не нами: другой порт
    labels = {"ours": ours, "adopted": adopted, "hand": hand, "stranger": stranger, "restarted": restarted}
    results = {k: rig.reg.close_one(k, entry(v)) for k, v in labels.items()}
    assert rig.stopped == ["ours"], rig.stopped
    assert results["ours"]["ok"] is True
    for k in ("adopted", "hand", "stranger", "restarted"):
        assert results[k]["ok"] is False, k
    assert results["adopted"]["code"] == "not_ours" and results["hand"]["code"] == "open_by_hand"


def test_close_is_refused_while_an_action_runs_on_the_profile(rig):
    rig.warm_up()
    lease = rig.checkout()
    res = rig.reg.close_one("Acc", entry())
    assert res["ok"] is False and res["code"] == "busy" and rig.stopped == []
    lease.release()
    assert rig.reg.close_one("Acc", entry())["ok"] is True


def test_close_failure_leaves_the_window_and_the_record(rig):
    rig.warm_up()

    def boom(entry_, label):
        raise RuntimeError("MLX 500")

    rig.reg._close_window = boom
    res = rig.reg.close_one("Acc", entry())
    assert res["ok"] is False and res["code"] == "stop_failed"
    assert rig.reg.session(PID) is not None and rig.reg.restart_wait_s(PID) == 0


def test_close_needs_a_confirmation_on_the_endpoint(monkeypatch):
    from src.web import warm_api

    monkeypatch.setattr(warm_api, "_guard", lambda: None)
    data, code = warm_api.close_profiles(["Acc"], confirm=False)
    assert code == 400 and data["code"] == "confirm"


# ───────────────────────── состояние строк: только локально ─────────────────────────

def test_states_make_no_calls_to_meta_mlx_or_the_browser(rig):
    def forbidden(*a, **k):
        raise AssertionError("the state of a row must be read locally")

    rig.reg._tab_factory = forbidden
    rig.reg._start_profile = forbidden
    rig.reg._close_window = forbidden
    rig.reg._slot_queue = forbidden
    a, b, c, d = ("a" * 8, "b" * 8, "c" * 8, "d" * 8)
    ids = [x + "-0000-4000-8000-000000000000" for x in (a, b, c, d)]
    rig.warm_up(ids[0])
    rig.warm_up(ids[1])
    rig.reg.session(ids[1]).logged_out = True
    rig.scan_result[ids[2]] = ("no_port", None)
    got = rig.reg.states([("W", ids[0]), ("L", ids[1]), ("H", ids[2]), ("C", ids[3]), ("N", "")])
    assert got["W"]["state"] == warm.WARM
    assert got["W"]["text"] == "Warm · since " + time.strftime("%H:%M", time.localtime(rig.now[0]))
    assert got["L"]["state"] == warm.LOGGED_OUT and got["L"]["text"] == "Logged out"
    assert got["H"]["text"] == "Opened by hand"
    assert got["C"]["text"] == "Closed"
    assert got["N"]["state"] == warm.UNKNOWN


def test_state_endpoint_does_not_touch_the_registry_actions(monkeypatch, tmp_path):
    from src.web import warm_api

    rig = Rig(tmp_path)
    rig.warm_up()
    monkeypatch.setenv("ACL_FB_BACKEND", "api")
    monkeypatch.setattr(warm, "_REGISTRY", rig.reg)
    monkeypatch.setattr(warm_api, "_profile_id", lambda label: PID)
    rig.reg._tab_factory = lambda url: pytest.fail("state opened a tab")
    out = warm_api.state(["Acc"])
    assert out["available"] is True and out["accounts"]["Acc"]["state"] == warm.WARM


def test_gate_busy_shows_working_on_the_chip(rig):
    rig.warm_up()
    lease = rig.checkout()
    assert "working" in rig.reg.states([("Acc", PID)])["Acc"]["text"]
    lease.release()


def test_registry_survives_a_restart_and_checks_the_process_first(rig, tmp_path):
    rig.warm_up(port=9333)
    again = warm.WarmRegistry(store=rig.store, wall=lambda: rig.now[0], scan=rig._scan,
                              tab_factory=rig._tab, gate=rig.gate)
    sess = again.session(PID)
    assert sess and sess.dashboard_started and sess.port == 9333 and sess.verified is False
    rig.scan_result[PID] = ("port", 9777)     # за время простоя профиль перезапустили не мы
    with pytest.raises(warm.WarmError) as ei:
        again.checkout(PID, account="Acc", act=ACT, mode="read", make_graph=rig.make)
    assert ei.value.code == "profile_restarted"
    assert again.session(PID) is None and rig.tabs == []


def test_a_leftover_tab_of_the_previous_run_is_closed_when_the_new_one_opens(rig):
    rig.warm_up()
    rig.reg.session(PID).target_id = "T-old"
    rig.reg.session(PID).verified = False
    rig.checkout().release()
    assert rig.tabs[0].closed_targets == ["T-old"], "the previous run's tab must not pile up"


def test_closed_profile_gives_a_clear_refusal_and_forgets_the_record(rig):
    rig.warm_up()
    rig.checkout().release()
    rig.tabs[0].closed = True
    rig.scan_result[PID] = ("none", None)
    rig.reg.session(PID).verified = False
    with pytest.raises(warm.WarmError) as ei:
        rig.checkout()
    assert ei.value.code == "profile_closed" and "Open profiles for today" in str(ei.value)
    assert rig.reg.session(PID) is None and rig.reg.restart_wait_s(PID) > 170


# ───────────────────────── только за ACL_FB_BACKEND=api ─────────────────────────

def test_everything_is_switched_off_without_the_api_backend(monkeypatch):
    from src.web import warm_api

    monkeypatch.delenv("ACL_FB_BACKEND", raising=False)
    out = warm_api.state(["Acc"])
    assert out["available"] is False and out["accounts"] == {}
    data, code = warm_api.start_open(["Acc"])
    assert code == 409 and data["code"] == "rpa_backend"
    data, code = warm_api.close_profiles(["Acc"], True)
    assert code == 409 and data["code"] == "rpa_backend"
    monkeypatch.setenv("ACL_FB_BACKEND", "rpa")
    assert warm_api.start_open(["Acc"])[1] == 409


def test_rpa_modules_know_nothing_about_warm_sessions():
    root = Path(__file__).resolve().parents[1] / "src"
    for path in list((root / "fb_ads_rpa").glob("*.py")) + list((root / "meta_campaign_agent").glob("*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        assert "fb_api.warm" not in text and "warm_sessions" not in text, path.name


def test_open_endpoint_needs_loopback(monkeypatch):
    from src.web import warm_api

    monkeypatch.setenv("ACL_FB_BACKEND", "api")
    monkeypatch.delenv("ACL_BIND_HOST", raising=False)
    data, code = warm_api.start_open(["Acc"])
    assert code == 403 and data["code"] == "not_loopback"


# ───────────────────────── через настоящие PageTab / InPageGraph ─────────────────────────

class RealishCdp:
    """Соединение к браузеру: настоящая вкладка `PageTab`, поддельный CDP."""

    def __init__(self):
        from tests.test_acl1756_fb_api import FakeCdp, FakePage

        self.page = FakePage()
        self.inner = FakeCdp(self.page)
        self.url = ADS_URL
        self.target_info_asked = 0

    def call(self, method, params=None, *, session_id="", timeout=5.0):
        if method == "Target.getTargetInfo":
            self.target_info_asked += 1
            return {"targetInfo": {"targetId": "T-mine", "url": self.url}}
        if method == "Page.navigate":
            self.url = (params or {}).get("url", self.url)
        return self.inner.call(method, params, session_id=session_id, timeout=timeout)

    def close(self):
        self.inner.close()

    @property
    def sent(self):
        return self.inner.sent


def test_real_tab_and_helper_three_reads_one_tab_no_reload(monkeypatch, tmp_path):
    cdp = RealishCdp()
    rig = Rig(tmp_path)
    rig.reg._tab_factory = lambda url: PageTab.open(url, connect=lambda u, timeout: cdp)
    rig.warm_up()
    ent = SimpleNamespace(profile_id=PID, folder_id="", multilogin_instance="A", ad_account_id=ACT)
    monkeypatch.setattr(sp, "_entry_for", lambda account: ent)
    monkeypatch.setattr(sp, "_set_wait_note", lambda *a: None)
    monkeypatch.setattr(warm, "_REGISTRY", rig.reg)
    monkeypatch.setattr(api, "_make_graph", lambda tab, **kw: tc.InPageGraph(tab, sleep=lambda s: None))
    monkeypatch.setattr(api, "_open_gated", lambda *a, **k: pytest.fail("a warm profile is not opened again"))
    monkeypatch.setattr(api, "GraphClient", lambda tr, **kw: g.GraphClient(tr, sleep=lambda s: None, **kw))
    api._COOLDOWN.clear()
    for commands in (False, True, False):
        with api._session("Acc", commands=commands) as sess:
            assert sess.act == ACT
    methods = [m for m, _p, _s in cdp.sent]
    assert methods.count("Target.createTarget") == 1, "a second tab was opened"
    assert methods.count("Page.navigate") == 1 and methods.count("Page.reload") == 0
    assert methods.count("Target.closeTarget") == 0, "the warm tab must stay open"
    assert cdp.target_info_asked >= 2, "the browser is asked about the tab before the 2nd and 3rd action"


def test_publisher_uses_the_warm_tab_and_leaves_it_open(monkeypatch, tmp_path):
    from src.web import publish_api

    rig = Rig(tmp_path)
    rig.warm_up()
    monkeypatch.setattr(warm, "_REGISTRY", rig.reg)
    monkeypatch.setattr(publish_api, "_resolve_entry", lambda label: SimpleNamespace(
        ad_account_id=ACT, profile_id=PID, folder_id="", multilogin_instance="A"))
    monkeypatch.setattr(publish_api, "_open_profile", lambda *a: pytest.fail("a warm profile is not opened again"))
    monkeypatch.setattr(publish_api, "_open_tab", lambda url: pytest.fail("no own tab for a warm profile"))
    monkeypatch.setattr(publish_api, "_make_graph", lambda tab, act, *, writes=True: rig.make(tab))
    monkeypatch.setattr(publish_api, "preflight_account", lambda client, act: SimpleNamespace())
    monkeypatch.setattr(publish_api, "TargetingResolver", lambda client: object())
    monkeypatch.setattr(publish_api, "GraphClient", lambda graph, **kw: SimpleNamespace(graph=graph, **kw))
    monkeypatch.setattr(publish_api, "_cooldown", lambda label: (0.0, ""))
    for _ in range(2):
        pub = publish_api.ApiPublisher(dry_run=True)
        pub.open("Acc")
        assert rig.gate.active() == [PID]
        assert pub.finish_job("Acc", clean=True) == ""
        assert rig.gate.active() == []
    assert len(rig.tabs) == 1 and rig.tabs[0].navigations == 1 and not rig.tabs[0].closed
    assert rig.stopped == []


def test_commands_on_a_warm_tab_keep_the_object_update_only_policy(monkeypatch, tmp_path):
    cdp = RealishCdp()
    rig = Rig(tmp_path)
    rig.reg._tab_factory = lambda url: PageTab.open(url, connect=lambda u, timeout: cdp)
    rig.warm_up()
    ent = SimpleNamespace(profile_id=PID, folder_id="", multilogin_instance="A", ad_account_id=ACT)
    monkeypatch.setattr(sp, "_entry_for", lambda account: ent)
    monkeypatch.setattr(sp, "_set_wait_note", lambda *a: None)
    monkeypatch.setattr(warm, "_REGISTRY", rig.reg)
    real = tc.InPageGraph

    def make(tab, commands_act=None):
        kw = {"allow_object_updates": True, "write_act": commands_act} if commands_act else {}
        return real(tab, sleep=lambda s: None, **kw)

    monkeypatch.setattr(api, "_make_graph", make)
    monkeypatch.setattr(api, "GraphClient", lambda tr, **kw: g.GraphClient(tr, sleep=lambda s: None, **kw))
    api._COOLDOWN.clear()
    with api._session("Acc") as read:
        assert read.client.allow_writes is False
    with api._session("Acc", commands=True) as cmd:
        assert cmd.client.allow_writes is True
        with pytest.raises(g.GraphStop):                       # создание закрыто и на тёплой вкладке
            cmd.client.post(f"/{ACT}/campaigns", {"name": "x", "status": "PAUSED"})
    with api._session("Acc") as read_again:                    # и чтение после команд снова без записи
        assert read_again.client.allow_writes is False
    assert [m for m, _p, _s in cdp.sent].count("Target.createTarget") == 1


def test_a_stop_in_the_action_cools_down_and_drops_a_hung_tab(monkeypatch, tmp_path):
    rig = Rig(tmp_path)
    rig.warm_up()
    monkeypatch.setattr(warm, "_REGISTRY", rig.reg)
    ent = SimpleNamespace(profile_id=PID, folder_id="", multilogin_instance="A", ad_account_id=ACT)
    monkeypatch.setattr(sp, "_entry_for", lambda account: ent)
    monkeypatch.setattr(sp, "_set_wait_note", lambda *a: None)
    monkeypatch.setattr(api, "_make_graph", lambda tab, **kw: rig.make(tab))
    api._COOLDOWN.clear()
    with api._session("Acc") as sess:
        sess.client.stopped = g.PageUnresponsive("the Ads Manager tab stopped answering")
    left, why = api._cooldown_state(sp._profile_key("Acc"))
    assert why == "page_unresponsive" and left > 590
    assert rig.tabs[0].closed, "a hung tab is not kept for the next action"
    assert rig.gate.active() == []


# ───────────────────────── маршруты и экран ─────────────────────────

class _Req:
    def __init__(self, body=None, params=None):
        self._body, self.query_params = body or {}, params

    async def json(self):
        return self._body


class _Params:
    def __init__(self, labels):
        self._labels = labels

    def getlist(self, key):
        return self._labels if key == "account" else []


def test_routes_are_registered_and_refuse_under_rpa(monkeypatch):
    import asyncio
    import json

    from src.web import app as web

    monkeypatch.delenv("ACL_FB_BACKEND", raising=False)
    paths = {(r.path, tuple(sorted(r.methods))) for r in web.app.routes if hasattr(r, "methods")}
    for want in (("/warm/state", ("GET",)), ("/warm/open", ("POST",)),
                 ("/warm/job/{job_id}", ("GET",)), ("/warm/close", ("POST",))):
        assert want in paths, want
    got = json.loads(asyncio.run(web.warm_state(_Req(params=_Params(["Acc"])))).body)
    assert got["available"] is False and got["accounts"] == {}
    for resp in (asyncio.run(web.warm_open(_Req({"accounts": ["Acc"]}))),
                 asyncio.run(web.warm_close(_Req({"accounts": ["Acc"], "confirm": True})))):
        assert resp.status_code == 409 and json.loads(resp.body)["code"] == "rpa_backend"


def test_open_job_runs_in_the_background_and_reports_each_account(monkeypatch, tmp_path):
    from src.web import warm_api

    rig = Rig(tmp_path)
    monkeypatch.setattr(warm, "_REGISTRY", rig.reg)
    monkeypatch.setattr(warm_api, "_guard", lambda: None)
    monkeypatch.setattr(warm_api, "_entry", lambda label: entry() if label == "Acc" else None)
    data, code = warm_api.start_open(["Acc", "Nope"])
    assert code == 202
    deadline = time.time() + 5
    while time.time() < deadline:
        job, _ = warm_api.job_status(data["job_id"])
        if job["state"] == "done":
            break
        time.sleep(0.05)
    assert job["state"] == "done"
    assert job["results"]["Acc"]["state"] == "ok" and job["results"]["Acc"]["code"] == "started"
    assert job["results"]["Nope"]["code"] == "not_in_map"
    assert rig.started == [(PID, "F", "A")]
    assert warm_api.job_status("missing")[1] == 404


def test_report_page_wiring_is_isolated_and_has_no_timers():
    root = Path(__file__).resolve().parents[1] / "src" / "web"
    page = (root / "templates" / "report.html").read_text(encoding="utf-8")
    js = (root / "static" / "warm_sessions.js").read_text(encoding="utf-8")
    assert page.count('<script src="/static/warm_sessions.js"></script>') == 1
    assert "setInterval" not in js, "no timers: the screen never polls on its own"
    assert js.count("setTimeout") == 1, "the only wait is the local open-job status"
    assert "window.confirm(" in js and "confirm: true" in js
    for url in ("/warm/state", "/warm/open", "/warm/close", "/warm/job/"):
        assert url in js
    for forbidden in ("/account-spend", "/campaign-inventory", "/campaign-commands", "/publish"):
        assert forbidden not in js, f"opening a list must not trigger {forbidden}"
    for text in ("Open profiles for today", "Close profiles", "Opened by hand"):
        assert text in js or text in warm.HAND_MESSAGE or text == "Opened by hand"


def test_chip_texts_for_every_state_come_from_the_server(rig):
    texts = {"Warm · since", "Open", "Closed", "Logged out", "Opened by hand", "Closing…"}
    src = Path(warm.__file__).read_text(encoding="utf-8")
    for t in texts:
        assert t in src, t
