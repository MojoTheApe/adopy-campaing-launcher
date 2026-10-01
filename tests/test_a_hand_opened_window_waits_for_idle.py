# -*- coding: utf-8 -*-
"""Окно, открытое руками, ждёт тишины ввода, а не останавливается сразу (ACL-1427).

ACL-1388 закрывает и поднимает такое окно только после минуты без ввода на
машине. Но публикацию начинает щелчок Publish — он же сбрасывает счётчик, и
проверка через пару секунд почти всегда видела «был ввод»: стоп каждый раз.

Решение владельца 01.10: когда не прошло ТОЛЬКО условие тишины, робот ждёт до
2 минут, перепроверяя все условия; дождался — закрывает и поднимает окно как в
ACL-1388; ввод шёл все 2 минуты — прежний стоп. Другие условия не ждутся.
Пока ждёт — строка в панели задания. Stop задания прерывает ожидание сразу.
Часы, сон, Win32, процессы и MLX здесь подменены.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import browser as br  # noqa: E402
from src.fb_ads_rpa import hand_window as hw  # noqa: E402
from src.fb_ads_rpa import profile_trouble as pt  # noqa: E402

PROFILE = "550e8400-e29b-41d4-a716-446655440000"
HAND_PID = 4242
ACCOUNT = "Facebook_6540"
WAIT_NOTICE = ("Waiting for the computer to be idle to reopen Facebook_6540's hand-opened window "
               "\u2014 don't use the mouse or keyboard for a minute")
REOPEN_NOTICE = "Closed the hand-opened window of Facebook_6540 and reopened it for the robot"


@pytest.fixture
def machine(monkeypatch):
    """Машина с поддельными часами. Окну 20 минут, на переднем плане проводник;
    ввод (щелчок Publish и дальше) идёт до `input_until` секунд от старта."""
    monkeypatch.setenv(hw.ENV_ENABLED, "1")
    for name in (hw.ENV_MIN_AGE, hw.ENV_MIN_IDLE, hw.ENV_IDLE_WAIT):
        monkeypatch.delenv(name, raising=False)
    m = SimpleNamespace(t=0.0, input_until=0.0, fg=9999, fg_at=None, cancel_at=None,
                        cancelled=False, sleeps=[], assessed=0)

    def now():
        return m.t

    def sleep(s):
        m.sleeps.append(s)
        m.t += s
        if m.cancel_at is not None and m.t >= m.cancel_at:
            m.cancelled = True

    def idle():
        m.assessed += 1
        return max(m.t - m.input_until, 0.0)

    def fg():
        if m.fg_at is not None and m.t >= m.fg_at:
            return HAND_PID
        return m.fg

    monkeypatch.setattr(hw, "_monotonic", now)
    monkeypatch.setattr(hw, "_sleep", sleep)
    monkeypatch.setattr(hw, "_is_windows", lambda: True)
    # Возраст окна считается по time.time(): «создано» — относительно него.
    monkeypatch.setattr(hw, "_process_create_times", lambda pids: [hw.time.time() - 20 * 60])
    monkeypatch.setattr(hw, "_foreground_pid", fg)
    monkeypatch.setattr(hw, "_idle_seconds", idle)
    monkeypatch.setattr(hw, "_ancestors", lambda pid: [])
    return m


# ── hand_window.wait_until_safe ────────────────────────────────────────────

def test_the_wait_is_two_minutes_and_follows_the_machine_env(monkeypatch):
    assert hw.IDLE_WAIT_S == 120
    monkeypatch.delenv(hw.ENV_IDLE_WAIT, raising=False)
    assert hw.idle_wait_s() == 120
    monkeypatch.setenv(hw.ENV_IDLE_WAIT, "300")
    assert hw.idle_wait_s() == 300
    monkeypatch.setenv(hw.ENV_IDLE_WAIT, "garbage")
    assert hw.idle_wait_s() == 120


def test_input_that_stops_after_30_s_ends_the_wait_once_idle_for_a_minute(machine):
    machine.input_until = 30.0
    told: list = []
    got = hw.wait_until_safe({HAND_PID}, on_wait=told.append)
    assert got.verdict.safe and got.outcome == hw.WAIT_IDLE
    assert 90.0 <= got.waited_s <= 90.0 + hw.IDLE_POLL_S
    assert len(told) == 1, "строку в панели повторяли на каждой проверке"
    assert max(machine.sleeps) <= hw.CANCEL_SLICE_S


def test_input_for_the_whole_two_minutes_stops_as_before(machine):
    machine.input_until = 10_000.0
    got = hw.wait_until_safe({HAND_PID})
    assert not got.verdict.safe and got.verdict.reason == hw.RECENT_INPUT
    assert got.outcome == hw.WAIT_TIMEOUT
    assert got.waited_s == pytest.approx(120.0)
    assert got.diag() == {"waited_s": 120.0, "wait_outcome": hw.WAIT_TIMEOUT}


def test_another_failing_gate_stops_at_once_without_waiting(machine):
    machine.input_until = 10_000.0
    machine.fg = HAND_PID
    told: list = []
    got = hw.wait_until_safe({HAND_PID}, on_wait=told.append)
    assert got.verdict.reason == hw.IN_FOREGROUND and got.outcome == hw.WAIT_NONE
    assert machine.sleeps == [] and told == [] and got.waited_s == 0.0


def test_the_other_gates_are_rechecked_during_the_wait(machine):
    machine.input_until = 10_000.0
    machine.fg_at = 20.0     # человек вывел окно профиля вперёд
    got = hw.wait_until_safe({HAND_PID})
    assert got.verdict.reason == hw.IN_FOREGROUND and got.outcome == hw.WAIT_GATE
    assert got.waited_s < 20.0 + hw.IDLE_POLL_S + 0.01


def test_an_already_idle_machine_does_not_wait(machine):
    machine.t = 500.0
    got = hw.wait_until_safe({HAND_PID})
    assert got.verdict.safe and got.outcome == hw.WAIT_NONE and machine.sleeps == []


def test_zero_wait_on_this_machine_is_the_old_immediate_stop(machine, monkeypatch):
    monkeypatch.setenv(hw.ENV_IDLE_WAIT, "0")
    machine.input_until = 30.0
    got = hw.wait_until_safe({HAND_PID})
    assert got.verdict.reason == hw.RECENT_INPUT and got.outcome == hw.WAIT_NONE
    assert machine.sleeps == []


def test_stop_during_the_wait_cancels_it_promptly(machine):
    machine.input_until = 10_000.0
    machine.cancel_at = 10.0
    with hw.watch_cancel(lambda: machine.cancelled):
        got = hw.wait_until_safe({HAND_PID})
    assert got.outcome == hw.WAIT_CANCELLED and not got.verdict.safe
    assert got.waited_s <= 10.0 + hw.CANCEL_SLICE_S


def test_outside_a_job_there_is_no_cancel_check(machine):
    assert hw._cancelled() is False
    with hw.watch_cancel(lambda: True):
        assert hw._cancelled() is True
    assert hw._cancelled() is False


# ── Провайдер: ждать, потом закрыть мягко и поднять — или стоп ─────────────

class _Launcher:
    def __init__(self, world):
        self.world = world

    def start_profile(self, folder_id, profile_id, *, headless=False):
        from src.meta_campaign_agent.multilogin_launcher import StartedProfile

        self.world.ev.append("start")
        return StartedProfile(profile_id=profile_id, folder_id=folder_id, port="58906")

    def stop_profile(self, profile_id, folder_id=None):
        self.world.ev.append(("stop", profile_id))
        self.world.pids = frozenset()


@pytest.fixture
def world(machine, monkeypatch):
    """Профиль открыт руками (процесс 4242, порта автоматизации нет)."""
    import src.meta_campaign_agent.multilogin_config as cfg_mod
    import src.meta_campaign_agent.multilogin_launcher as launcher_mod
    from src.fb_ads_rpa import run_telemetry as rt

    w = SimpleNamespace(ev=[], pids=frozenset({HAND_PID}), rows=[], notices=[], m=machine)
    monkeypatch.setattr(br, "_ROBOT_PROFILES", {})
    monkeypatch.setattr(br.time, "sleep", lambda s: None)
    monkeypatch.setattr(br, "_find_mlx_profile_dir", lambda pid: "C:/mlx/p")
    monkeypatch.setattr(br, "_profile_pids", lambda udd, pid: w.pids)
    monkeypatch.setattr(br, "_kill_chrome_for_user_data_dir", lambda *a, **k: w.ev.append("kill"))
    monkeypatch.setattr(cfg_mod, "get_multilogin_instance", lambda inst: SimpleNamespace(instance_id=inst))
    monkeypatch.setattr(launcher_mod, "MultiloginLauncher", lambda cfg: _Launcher(w))
    monkeypatch.setattr(rt, "record", lambda step, ok=None, **kw: w.rows.append((step, ok, kw)))
    import sentry_sdk
    monkeypatch.setattr(sentry_sdk, "add_breadcrumb", lambda **kw: None)

    prov = br.MultiloginAttachOnlyProvider()
    monkeypatch.setattr(prov._direct, "_try_attach_existing", lambda *a, **kw: None)
    monkeypatch.setattr(prov._direct, "_profile_process_running", lambda *a: bool(w.pids))
    monkeypatch.setattr(prov, "_connect_started",
                        lambda started, pid: w.ev.append("connect") or SimpleNamespace(page="reopened-page"))
    w.prov = prov
    return w


def _entry():
    return SimpleNamespace(profile_id=PROFILE, folder_id="f-1", multilogin_instance="A")


def _open(world):
    from src.fb_ads_rpa.creative_compress import observe_preparation

    with observe_preparation(world.notices.append):
        return world.prov.open(_entry(), ACCOUNT)


def test_publish_click_then_quiet_reopens_the_window(world):
    world.m.input_until = 30.0
    conn = _open(world)
    assert conn.page == "reopened-page"
    assert world.ev == [("stop", PROFILE), "start", "connect"]
    assert "kill" not in world.ev
    assert world.notices == [WAIT_NOTICE, REOPEN_NOTICE]
    step, ok, kw = world.rows[-1]
    assert (step, ok, kw["note"]) == ("profile_hand_window", True, "reopened")
    assert kw["diag"]["wait_outcome"] == hw.WAIT_IDLE
    assert 90.0 <= kw["diag"]["waited_s"] <= 93.0
    assert PROFILE[:8] not in repr(world.rows)


def test_input_for_two_minutes_stops_with_the_old_message(world):
    world.m.input_until = 10_000.0
    with pytest.raises(pt.ProfileStop) as stop:
        _open(world)
    assert stop.value.code == pt.OPEN_BY_HAND and stop.value.detail == "no_automation_port"
    assert world.ev == [], "окно тронули, хотя ввод не прекращался"
    assert world.notices == [WAIT_NOTICE]
    step, ok, kw = world.rows[-1]
    assert (step, ok, kw["note"]) == ("profile_hand_window", False, hw.RECENT_INPUT)
    assert kw["diag"]["wait_outcome"] == hw.WAIT_TIMEOUT and kw["diag"]["waited_s"] == 120.0


def test_a_window_in_front_stops_at_once(world):
    world.m.input_until = 10_000.0
    world.m.fg = HAND_PID
    with pytest.raises(pt.ProfileStop) as stop:
        _open(world)
    assert stop.value.code == pt.OPEN_BY_HAND
    assert world.ev == [] and world.notices == [] and world.m.sleeps == []
    kw = world.rows[-1][2]
    assert kw["note"] == hw.IN_FOREGROUND
    assert kw["diag"]["wait_outcome"] == hw.WAIT_NONE and kw["diag"]["waited_s"] == 0.0


def test_job_stop_during_the_wait_cancels_it_through_the_publisher(world, monkeypatch):
    """Stop задания (`RpaPublisher.cancel` из другого потока) доходит до
    ожидания: публикатор ставит `watch_cancel` вокруг открытия браузера."""
    from src.fb_ads_rpa.rpa_publisher import RpaPublisher
    import src.fb_ads_rpa.rpa_publisher as pub_mod

    monkeypatch.setattr(pub_mod, "get_mlx_attach_only", lambda: True)
    pub = RpaPublisher.__new__(RpaPublisher)
    pub.hand_off = pub.dry_run = False
    pub.selectors_path = None
    pub._conn = None
    pub._entry = None
    pub._cancelled = False
    pub._provider = world.prov
    monkeypatch.setattr(pub, "_resolve_entry", lambda label: _entry())

    world.m.input_until = 10_000.0
    real_sleep = hw._sleep

    def sleep(s):
        real_sleep(s)
        if world.m.t >= 10.0:
            pub._cancelled = True   # Stop в панели на 10-й секунде

    monkeypatch.setattr(hw, "_sleep", sleep)
    from src.fb_ads_rpa.creative_compress import observe_preparation

    with observe_preparation(world.notices.append), pytest.raises(pt.ProfileStop) as stop:
        pub._ensure_browser(ACCOUNT)
    assert stop.value.code == pt.OPEN_BY_HAND
    assert world.m.t <= 10.0 + hw.CANCEL_SLICE_S, "Stop ждал конца двух минут"
    assert world.ev == []
    kw = world.rows[-1][2]
    assert kw["note"] == "cancelled" and kw["diag"]["wait_outcome"] == hw.WAIT_CANCELLED
    assert hw._cancelled() is False, "проверка Stop пережила открытие браузера"
