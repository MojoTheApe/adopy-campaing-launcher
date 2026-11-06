# -*- coding: utf-8 -*-
"""ACL-2400 / ACL-2356: порог памяти 92 %, разовая миграция 75/85, прогноз «занято + новый профиль»."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import config_unify, paths  # noqa: E402
from src.web import profile_slots as ps  # noqa: E402

GB = 1024


def reading(total_gb: float, used_pct: float) -> ps.MemoryReading:
    total = int(total_gb * GB)
    return ps.MemoryReading(total_mb=total, available_mb=int(total * (100 - used_pct) / 100))


def test_default_is_92():
    assert ps.load_settings({}).max_used_pct == 92.0
    assert ps.keep_free_mb(16 * GB, ps.load_settings({})) == int(16 * GB * 0.08)


@pytest.fixture
def env_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "get_machine_env_path", lambda: str(tmp_path / ".env"))
    monkeypatch.delenv("ACL_MEMORY_MAX_USED_PCT", raising=False)
    monkeypatch.delenv("ACL_MEMORY_PCT_MIGRATED", raising=False)
    return tmp_path


def values(d):
    return {k: v.strip("\"'") for k, v in config_unify._env_values(d / ".env").items()}


@pytest.mark.parametrize("old", ["75", "85", "75.0"])
def test_old_values_move_to_92_once_with_a_backup(env_dir, old):
    (env_dir / ".env").write_text(f"ACL_MEMORY_MAX_USED_PCT={old}\nOTHER=1\n", encoding="utf-8")
    note = config_unify.migrate_memory_default(env_dir)
    assert "92" in note
    v = values(env_dir)
    assert v["ACL_MEMORY_MAX_USED_PCT"] == "92" and v["ACL_MEMORY_PCT_MIGRATED"] == "2400" and v["OTHER"] == "1"
    backups = list((env_dir / "backups").glob("env-memory-2400-*/.env"))
    assert len(backups) == 1 and f"ACL_MEMORY_MAX_USED_PCT={old}" in backups[0].read_text(encoding="utf-8")


def test_a_later_value_is_never_overwritten(env_dir):
    (env_dir / ".env").write_text("ACL_MEMORY_MAX_USED_PCT=75\n", encoding="utf-8")
    config_unify.migrate_memory_default(env_dir)
    paths.update_machine_env({"ACL_MEMORY_MAX_USED_PCT": "85"})   # owner sets it later
    assert config_unify.migrate_memory_default(env_dir) == ""
    assert values(env_dir)["ACL_MEMORY_MAX_USED_PCT"] == "85"


@pytest.mark.parametrize("body", ["ACL_MEMORY_MAX_USED_PCT=80\n", "ACL_MEMORY_MAX_USED_PCT=90\n", "OTHER=1\n"])
def test_other_values_and_absent_key_are_kept_and_get_only_the_marker(env_dir, body):
    (env_dir / ".env").write_text(body, encoding="utf-8")
    assert config_unify.migrate_memory_default(env_dir) == ""
    v = values(env_dir)
    assert v["ACL_MEMORY_PCT_MIGRATED"] == "2400"
    assert v.get("ACL_MEMORY_MAX_USED_PCT") == (body.split("=")[1].strip() if "PCT" in body else None)


def test_missing_env_file_is_not_an_error(env_dir):
    assert config_unify.migrate_memory_default(env_dir) == ""


# --- ACL-2356 ---------------------------------------------------------------

def test_projected_share_before_the_change_would_have_allowed_this():
    """16 GB, 70 % used, one profile running, 3.5 GB profile: 92 % share rule alone says yes (~92 % after)."""
    s = ps.SlotSettings(ceiling=5)
    mem = reading(16, 70)
    assert mem.available_mb >= ps.keep_free_mb(mem.total_mb, s) + 0          # old rule: allowed
    d = ps.decide(1, 0, mem, s)
    assert d["allow"] is False and d["projected"] is True and d["projected_pct"] > 85


def test_first_job_is_never_held_by_the_projected_check():
    s = ps.SlotSettings(ceiling=5)
    d = ps.decide(0, 0, reading(16, 88), s)
    assert d["allow"] is True and d["reason"] == "memory_ok" and "projected" not in d
    assert ps.decide(0, 0, reading(16, 93), s)["reason"] == "below_reserve"      # unchanged


def test_31gb_starts_three_or_four_profiles():
    s = ps.SlotSettings(ceiling=6)
    used, started = 40.0, 0
    for running in range(0, 6):
        if not ps.decide(running, 0, reading(31, used), s)["allow"]:
            break
        started += 1
        used += 3.5 / 31 * 100
    assert started == 3      # 40 -> 51 -> 63 -> 74 %; a fourth would end at 85.2 %


def test_16gb_two_with_a_measured_2_5gb_profile_one_more_refused():
    s = ps.SlotSettings(ceiling=5)
    mb = int(2.5 * GB)
    assert ps.decide(1, 0, reading(16, 45), s, measured_mb=mb)["allow"] is True
    assert ps.decide(2, 0, reading(16, 76), s, measured_mb=mb)["allow"] is False


def test_warming_profiles_are_counted():
    s = ps.SlotSettings(ceiling=5)
    mem = reading(16, 60)
    assert ps.decide(1, 0, mem, s)["allow"] is True       # 60 + 22 = 82 %
    assert ps.decide(2, 1, mem, s)["allow"] is False


def test_profile_open_skips_the_projected_check():
    s = ps.SlotSettings(ceiling=5)
    assert ps.decide(1, 0, reading(16, 80), s, profile_open=True)["reason"] == "profile_open"


def test_projected_helpers():
    mem = reading(16, 70)
    assert round(ps.projected_pct(mem, int(3.5 * GB)), 1) == 91.9
    assert ps.projected_short_mb(mem, int(3.5 * GB)) == pytest.approx(0.069 * 16 * GB, abs=60)
    assert ps.projected_short_mb(reading(16, 40), int(3.5 * GB)) == 0
