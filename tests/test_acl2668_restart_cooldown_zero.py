"""ACL-2668, решение владельца 16.11: пауза перед повторным запуском профиля — 0 у всех.

Остановленный профиль можно открыть сразу. Переменная `ACL_PROFILE_RESTART_COOLDOWN_S`
осталась только аварийным рычагом: число > 0 снова включает ожидание/отказ.
"""
from __future__ import annotations

from pathlib import Path

from src.analysis_agent import account_spend as sp
from src.fb_api import stop_log

ROOT = Path(__file__).resolve().parents[1]


class _Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


def _log(tmp_path, clock):
    log_ = stop_log.StopLog(path=tmp_path / "profile_stops.json", wall=clock)
    return log_


def test_default_cooldown_is_zero(monkeypatch):
    monkeypatch.delenv(stop_log.RESTART_COOLDOWN_ENV, raising=False)
    assert stop_log.DEFAULT_RESTART_COOLDOWN_S == 0.0
    assert stop_log.restart_cooldown_s() == 0.0
    assert stop_log.restart_max_wait_s() == 0.0


def test_just_stopped_profile_can_start_at_once(tmp_path, monkeypatch):
    monkeypatch.delenv(stop_log.RESTART_COOLDOWN_ENV, raising=False)
    clock = _Clock()
    log_ = _log(tmp_path, clock)
    log_.record("P1")
    slept = []
    assert stop_log.refusal("P1", log_) == ""
    assert stop_log.wait_or_refusal("P1", log_, sleep=slept.append) == ""
    assert slept == [] and log_.recent_stop_at("P1") is None and log_.restart_wait_s("P1") == 0.0


def test_env_stays_an_emergency_override(tmp_path, monkeypatch):
    monkeypatch.setenv(stop_log.RESTART_COOLDOWN_ENV, "180")
    monkeypatch.setenv(stop_log.RESTART_MAX_WAIT_ENV, "0")
    clock = _Clock()
    log_ = _log(tmp_path, clock)
    log_.record("P1")
    assert "Nothing was started" in stop_log.refusal("P1", log_)


def test_no_three_minute_wait_in_texts():
    assert "3 minutes" not in sp.HAND_OPEN_MESSAGE
    for rel in ("src/web/static/acm_commands.js", "src/web/static/warm_sessions.js", "src/fb_api/warm.py"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert "about 3 minutes" not in text, rel
