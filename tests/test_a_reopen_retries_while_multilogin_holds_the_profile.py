# -*- coding: utf-8 -*-
"""Переоткрытие окна, открытого руками, ждёт, пока Multilogin отпустит профиль (ACL-1606).

05.10 (FacebookElf403) робот мягко остановил окно через MLX API, но Multilogin
ещё мгновение держал профиль: запуск ответил «locked», код прочёл это как
«открыто руками» и сдался — окно владельца закрыто и не открыто. Теперь после
собственной остановки профиль помечается как профиль робота, а запуск
повторяется до ~30 с, пока замок не отпустит. Остаётся мягкая остановка
`allow_kill=False`, другие профили не трогаются, простой браузер не открывается.

MLX, процессы и сон подменены; настоящий Multilogin не трогается.
"""
from __future__ import annotations

import pytest

from src.fb_ads_rpa import browser as br
from src.fb_ads_rpa import profile_trouble as pt
from tests.test_a_hand_opened_window_is_reopened_at_once import (  # noqa: F401
    ACCOUNT, OTHER_PROFILE, PROFILE, _entry, _hand_row, machine, world)


@pytest.fixture
def locked(world, monkeypatch):
    """Запуск отвечает «locked», пока не кончится `world.lock_left`."""
    import src.meta_campaign_agent.multilogin_launcher as launcher_mod
    from src.meta_campaign_agent.multilogin_launcher import MultiloginLauncherError

    world.lock_left = 0
    world.start_calls = 0

    class _Locked:
        def __init__(self, cfg=None):
            pass

    def start_profile(self, folder_id, profile_id, *, headless=False):
        from src.meta_campaign_agent.multilogin_launcher import StartedProfile

        world.start_calls += 1
        world.ev.append(("start", profile_id))
        if world.lock_left > 0:
            world.lock_left -= 1
            raise MultiloginLauncherError("Can't lock profile: profile is locked")
        return StartedProfile(profile_id=profile_id, folder_id=folder_id, port="58906")

    def stop_profile(self, profile_id, folder_id=None):
        world.ev.append(("stop", profile_id))
        world.running[profile_id] = frozenset()

    _Locked.start_profile = start_profile
    _Locked.stop_profile = stop_profile
    monkeypatch.setattr(launcher_mod, "MultiloginLauncher", _Locked)
    world.launcher_cls = _Locked
    return world


def test_start_locked_twice_then_succeeds_opens_the_profile(locked):
    locked.lock_left = 2
    conn = locked.prov.open(_entry(), ACCOUNT)
    assert conn.page == "reopened-page"
    assert locked.start_calls == 3
    assert locked.ev == [("stop", PROFILE), ("start", PROFILE), ("start", PROFILE),
                         ("start", PROFILE), ("connect", PROFILE)], locked.ev
    assert "kill" not in locked.ev
    assert all(flag is False for _, flag in locked.kill_flags), locked.kill_flags
    # Паузы повторов — короткие, плюс одна пауза после остановки.
    assert locked.br_sleeps == [3, 1, 1], locked.br_sleeps
    step, ok, kw = _hand_row(locked)
    assert (ok, kw["note"]) == (True, "reopened")
    assert kw["diag"]["start_retries"] == 2 and kw["diag"]["start_waited_s"] == 2.0
    assert len([r for r in locked.rows if r[0] == "profile_hand_window"]) == 1


def test_start_that_keeps_failing_gives_a_clear_reason_and_kills_nothing(locked):
    locked.lock_left = 10 ** 6
    with pytest.raises(pt.ProfileStop) as stop:
        locked.prov.open(_entry(), ACCOUNT)
    assert stop.value.code == pt.RESTART_FAILED
    assert "Multilogin did not release the profile after closing its window" in stop.value.detail
    assert "open it again in Multilogin" in stop.value.detail
    assert "kill" not in locked.ev
    assert all(flag is False for _, flag in locked.kill_flags)
    assert locked.ev.count(("stop", PROFILE)) == 1, "лишние остановки"
    retries = len(br.HAND_WINDOW_START_BACKOFF_S)
    assert locked.start_calls == retries + 1
    assert sum(br.HAND_WINDOW_START_BACKOFF_S) in range(28, 36)
    step, ok, kw = _hand_row(locked)
    assert (ok, kw["note"]) == (False, "restart_failed")
    assert kw["diag"]["start_retries"] == retries
    assert locked.running[OTHER_PROFILE]


def test_the_second_attempt_is_not_read_as_hand_opened(locked, monkeypatch):
    locked.lock_left = 1
    calls = []
    real = locked.prov._reopen_hand_opened
    monkeypatch.setattr(locked.prov, "_reopen_hand_opened",
                        lambda *a, **kw: calls.append(a[4]) or real(*a, **kw))
    conn = locked.prov.open(_entry(), ACCOUNT)
    assert conn.page == "reopened-page"
    assert calls == ["no_automation_port"], "переоткрытие вызвано повторно"
    # Метки «робота» после повтора нет, пока запуск не удался сам; удачный
    # запуск ставит её сам — окно к этому моменту открыто.
    assert not any(r[2].get("note") == "mlx_lock" for r in locked.rows)


def test_a_non_lock_start_failure_is_not_retried(locked, monkeypatch):
    from src.meta_campaign_agent.multilogin_launcher import MultiloginLauncherError

    def boom(self, folder_id, profile_id, *, headless=False):
        locked.start_calls += 1
        raise MultiloginLauncherError("HTTP 500 internal")

    monkeypatch.setattr(locked.launcher_cls, "start_profile", boom)
    with pytest.raises(pt.ProfileStop) as stop:
        locked.prov.open(_entry(), ACCOUNT)
    assert stop.value.code == pt.RESTART_FAILED
    assert locked.start_calls == 1
    assert locked.br_sleeps == [3]


def test_no_other_profile_is_touched(locked):
    locked.lock_left = 3
    locked.prov.open(_entry(), ACCOUNT)
    touched = {e[1] for e in locked.ev if isinstance(e, tuple)}
    assert touched == {PROFILE}, touched
    assert locked.running[OTHER_PROFILE]


def test_a_failed_reopen_then_a_hand_window_gets_no_stop_and_no_kill(locked, monkeypatch):
    """Ревью PR #607 (F1): отказ переоткрытия не оставляет метку «профиль робота»."""
    locked.lock_left = 10 ** 6
    with pytest.raises(pt.ProfileStop):
        locked.prov.open(_entry(), ACCOUNT)
    assert not br._robot_profile(PROFILE, f"C:/mlx/{PROFILE}")
    assert PROFILE not in br._ROBOT_PROFILES
    # Владелец открыл окно руками — теперь не отвечает ничего, кроме отказа.
    locked.running[PROFILE] = frozenset({4242})
    locked.ev.clear()
    locked.kill_flags.clear()
    with pytest.raises(pt.ProfileStop) as stop:
        locked.prov.open(_entry(), ACCOUNT, allow_kill=True, reopen_hand_window=False)
    assert stop.value.code == pt.OPEN_BY_HAND
    assert locked.ev == [], locked.ev
    assert "kill" not in locked.ev


def test_stop_pressed_during_the_retries_does_not_start_the_profile(locked):
    locked.lock_left = 10 ** 6
    seen = []

    def cancel():
        seen.append(locked.start_calls)
        return locked.start_calls >= 2

    locked.prov.cancel_check = cancel
    with pytest.raises(br.ReopenCancelled) as err:
        locked.prov.open(_entry(), ACCOUNT)
    assert "stopped by the user" in str(err.value)
    assert locked.start_calls == 2, "после Stop профиль запускали ещё раз"
    assert "kill" not in locked.ev
    step, ok, kw = _hand_row(locked)
    assert (ok, kw["note"]) == (False, "cancelled")
