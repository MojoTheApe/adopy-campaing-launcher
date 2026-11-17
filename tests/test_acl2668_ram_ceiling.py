"""ACL-2668 (решение владельца 16.11): потолок «Profiles at once» по умолчанию — по памяти машины.

8 ГБ → 2, 16 ГБ → 4, 32 ГБ → 5, больше → 6; явное значение в Settings важнее; порог 92 % остаётся.
"""
from __future__ import annotations

import pytest

from src.web import profile_slots as ps
from src.web import run_limits as rl

GB = 1024


def _machine(monkeypatch, total_gb):
    monkeypatch.setattr(ps, "machine_total_mb", lambda: None if total_gb is None else int(total_gb * GB))


@pytest.mark.parametrize("total_gb,expected", [
    (None, 2), (0, 2), (7.8, 2), (11.9, 2), (15.8, 4), (23.9, 4), (31.7, 5), (47.9, 5), (63.8, 6), (128, 6),
])
def test_the_default_ceiling_follows_machine_memory(monkeypatch, total_gb, expected):
    _machine(monkeypatch, total_gb)
    assert ps.ceiling_for_memory(None if total_gb is None else int(total_gb * GB)) == expected
    monkeypatch.delenv("ACL_MAX_PROFILES", raising=False)
    monkeypatch.delenv("ACL_MAX_PARALLEL_PROFILES", raising=False)
    monkeypatch.delenv("ACL_MAX_PARALLEL_CEILING", raising=False)
    assert ps.load_settings().ceiling == expected
    assert rl.defaults()["ceiling"] == expected


def test_an_explicit_settings_value_wins_over_memory(monkeypatch):
    _machine(monkeypatch, 64)
    monkeypatch.setenv("ACL_MAX_PROFILES", "2")
    assert ps.load_settings().ceiling == 2
    _machine(monkeypatch, 8)
    monkeypatch.setenv("ACL_MAX_PROFILES", "5")
    assert ps.load_settings().ceiling == 5
    # мусор или "auto" — снова по памяти
    monkeypatch.setenv("ACL_MAX_PROFILES", "auto")
    assert ps.load_settings({"ACL_MAX_PROFILES": "many"}).ceiling == 2


def test_the_memory_share_gate_stays_on_top(monkeypatch):
    _machine(monkeypatch, 32)
    s = ps.load_settings({})
    assert s.ceiling == 5 and s.max_used_pct == ps.DEFAULT_MAX_USED_PCT == 92.0
    # память занята выше порога — новый профиль не стартует, даже если до потолка далеко
    busy = ps.MemoryReading(total_mb=32 * GB, available_mb=int(32 * GB * 0.05))
    decision = ps.decide(1, 0, busy, s)
    assert decision["allow"] is False and decision["reason"] != "ceiling"


def test_settings_show_the_effective_machine_value(monkeypatch):
    _machine(monkeypatch, 16)
    monkeypatch.delenv("ACL_MAX_PROFILES", raising=False)
    monkeypatch.setattr(ps, "read_memory", lambda: ps.MemoryReading(total_mb=16 * GB, available_mb=10 * GB))
    cur = rl.current()
    assert cur["ceiling"] == 4 and cur["defaults"]["ceiling"] == 4
    # подтверждение и предупреждение — только выше того, что машина получает по памяти
    assert cur["confirm_above"]["ceiling"] == 4
    assert not [w for w in cur["warnings"] if "profiles at once" in w]
    assert rl.warnings_for({"ceiling": 5, "memory_max_used_pct": 85}, 16 * GB)


def test_machine_memory_is_read_once(monkeypatch):
    real = ps.machine_total_mb.__wrapped__   # conftest подменил её на «память не прочиталась»
    calls = []

    def fake_read():
        calls.append(1)
        return ps.MemoryReading(total_mb=16 * GB, available_mb=8 * GB)

    monkeypatch.setattr(ps, "_machine_total_mb_cache", None)
    monkeypatch.setattr(ps, "read_memory", fake_read)
    assert real() == 16 * GB and real() == 16 * GB
    assert len(calls) == 1
