"""ACL-2818: пауза после остановки профиля по умолчанию 0 (решение владельца 23.10).

Без переменной окружения открытие сразу после закрытия проходит без ожидания и без
текста «can be started again»; `ACL_PROFILE_RESTART_COOLDOWN_S` по-прежнему переопределяет паузу.
"""
from __future__ import annotations

import pytest

from src.fb_api import stop_log, warm
from tests.test_acl1812_warm_sessions import PID, Rig, entry


@pytest.fixture(autouse=True)
def _no_cooldown_env(monkeypatch):
    monkeypatch.delenv(stop_log.RESTART_COOLDOWN_ENV, raising=False)


def test_default_restart_cooldown_is_zero():
    assert stop_log.DEFAULT_RESTART_COOLDOWN_S == 0.0
    assert stop_log.restart_cooldown_s() == 0.0
    assert warm.restart_cooldown_s() == 0.0


def test_stop_log_default_gives_no_wait_and_no_refusal(tmp_path):
    now = [1_800_000_000.0]
    log = stop_log.StopLog(tmp_path / "stops.json", wall=lambda: now[0])
    log.record(PID)
    now[0] += 1
    assert log.restart_wait_s(PID) == 0
    assert log.recent_stop_at(PID) is None
    assert stop_log.refusal(PID, log) == ""
    naps: list[float] = []
    assert stop_log.wait_or_refusal(PID, log, sleep=naps.append) == ""
    assert naps == [], "nothing to wait for"


def test_env_override_still_applies(tmp_path, monkeypatch):
    monkeypatch.setenv(stop_log.RESTART_COOLDOWN_ENV, "180")
    now = [1_800_000_000.0]
    log = stop_log.StopLog(tmp_path / "stops.json", wall=lambda: now[0])
    log.record(PID)
    now[0] += 100
    assert 70 < log.restart_wait_s(PID) <= 80
    assert stop_log.refusal(PID, log) != ""


def test_closed_profile_reopens_at_once_without_sync_wait(tmp_path):
    rig = Rig(tmp_path)
    rig.warm_up()
    assert rig.reg.close_one("Acc", entry())["ok"]
    res = rig.reg.open_one("Acc", entry())
    assert res["ok"] is True, res
    assert rig.started == [(PID, "F", "A")]
    assert rig.reg.restart_wait_s(PID) == 0


def test_profile_closed_by_hand_shows_no_restart_wait_text(tmp_path):
    rig = Rig(tmp_path)
    rig.warm_up()
    rig.checkout().release()
    rig.scan_result[PID] = ("none", None)
    chip = rig.reg.states([("Acc", PID)])["Acc"]
    assert chip["state"] == warm.CLOSED
    assert "can be started again" not in chip["title"]
    res = rig.reg.open_one("Acc", entry())
    assert res["ok"] is True, res
