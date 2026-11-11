"""ACL-2400: memory threshold 92 % everywhere, one-time update migration, immediate memory message.

Fakes and tmp dirs only: no real machine .env, no Multilogin, no sleeping.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from config import config_unify as cu
from src.fb_api import warm
from src.web import profile_slots as ps
from tests.test_acl1812_warm_sessions import PID, Rig, entry

KEY = "ACL_MEMORY_MAX_USED_PCT"


def _env(tmp_path: Path, text: str | None) -> Path:
    if text is not None:
        (tmp_path / ".env").write_text(text, encoding="utf-8", newline="")
    return tmp_path / ".env"


def test_default_is_92_and_loads_without_env():
    assert ps.DEFAULT_MAX_USED_PCT == 92.0
    assert ps.load_settings({}).max_used_pct == 92.0


@pytest.mark.parametrize("old", ["75", "85", "90", "50", "75.0"])
def test_update_migrates_an_older_value_to_92_once_and_backs_it_up(tmp_path, old):
    env = _env(tmp_path, f"A=1\n{KEY}={old}\nB=2\n")
    note = cu.migrate_memory_threshold(tmp_path, now=1_800_000_000.0)
    assert note and "92" in note
    assert env.read_text(encoding="utf-8") == f"A=1\n{KEY}=92\nB=2\n"
    backups = list((tmp_path / "backups").glob("memory-92-*/.env"))
    assert len(backups) == 1 and f"{KEY}={old}" in backups[0].read_text(encoding="utf-8")
    assert cu.MEMORY_PCT_MIGRATION_ID in json.loads((tmp_path / cu.MIGRATIONS_FILE).read_text(encoding="utf-8"))


def test_a_value_set_later_in_settings_is_not_overwritten(tmp_path):
    env = _env(tmp_path, f"{KEY}=75\n")
    cu.migrate_memory_threshold(tmp_path)
    env.write_text(f"{KEY}=80\n", encoding="utf-8")          # the owner changes it in Settings afterwards
    assert cu.migrate_memory_threshold(tmp_path) is None
    assert env.read_text(encoding="utf-8") == f"{KEY}=80\n"
    env.write_text(f"{KEY}=75\n", encoding="utf-8")          # even the old default, chosen on purpose
    assert cu.migrate_memory_threshold(tmp_path) is None
    assert env.read_text(encoding="utf-8") == f"{KEY}=75\n"


def test_no_change_when_the_key_is_absent_and_a_later_value_is_still_kept(tmp_path):
    env = _env(tmp_path, "A=1\n")
    assert cu.migrate_memory_threshold(tmp_path) is None
    assert env.read_text(encoding="utf-8") == "A=1\n"
    assert not (tmp_path / "backups").exists()
    env.write_text(f"A=1\n{KEY}=75\n", encoding="utf-8")
    assert cu.migrate_memory_threshold(tmp_path) is None     # already stamped: a later 75 stays
    assert f"{KEY}=75" in env.read_text(encoding="utf-8")


def test_no_env_file_at_all_is_fine(tmp_path):
    assert cu.migrate_memory_threshold(tmp_path) is None
    assert not (tmp_path / ".env").exists()


def test_values_at_or_above_92_commented_lines_and_junk_are_kept(tmp_path):
    for text in (f"{KEY}=92\n", f"{KEY}=95\n", f"# {KEY}=75\n", f"{KEY}=abc\n"):
        d = tmp_path / str(abs(hash(text)))
        d.mkdir()
        env = _env(d, text)
        assert cu.migrate_memory_threshold(d) is None
        assert env.read_text(encoding="utf-8") == text
        assert not (d / "backups").exists()


def test_prepare_config_dir_runs_the_migration(tmp_path, monkeypatch):
    env = _env(tmp_path, f"{KEY}=85\n")
    monkeypatch.setattr(cu.paths, "pin_config_dir_env", lambda: str(tmp_path))
    monkeypatch.setattr(cu.paths, "is_config_dir_explicit", lambda: True)
    monkeypatch.setattr(cu.paths, "is_virtualized_environment", lambda: False)
    result = cu.prepare_config_dir()
    assert env.read_text(encoding="utf-8") == f"{KEY}=92\n" and result.env_merged
    cu.prepare_config_dir()
    assert env.read_text(encoding="utf-8") == f"{KEY}=92\n"


def test_an_older_package_copy_does_not_bring_75_back():
    merged, keys = cu.merge_env_text_keys(f"{KEY}=92\n", f"{KEY}=75\n", shadow_newer=True)
    assert KEY not in keys and f"{KEY}=92" in merged
    merged, keys = cu.merge_env_text_keys(f"{KEY}=80\n", f"{KEY}=92\n", shadow_newer=True)
    assert KEY not in keys and f"{KEY}=80" in merged


def test_env_example_and_docs_show_92_not_75():
    root = Path(__file__).resolve().parents[1]
    text = (root / ".env.example").read_text(encoding="utf-8")
    assert re.search(rf"^#?\s*{KEY}=92\s*$", text, re.M)
    assert not re.search(rf"{KEY}=75\b", text)


# --- immediate message ---------------------------------------------------------------------------

def _fail_if_slow(started: float, limit: float = 5.0) -> None:
    import time

    assert time.monotonic() - started < limit, "the memory hold must be reported at once, not after a wait"


def test_a_held_action_gets_the_memory_message_at_once_without_waiting(tmp_path):
    import time

    rig = Rig(tmp_path, limit=2, memory=lambda: ps.MemoryReading(16000, 1000))   # default 300 s account gate wait
    rig.warm_up()
    other = "6ba7b810-9dad-11d1-80b4-00c04fd430c8"
    rig.warm_up(other, account="Acc2")
    first = rig.checkout(other, account="Acc2")   # ACL-2369: the first action on a warm profile is never held
    started = time.monotonic()
    with pytest.raises(warm.WarmError) as ei:
        rig.gate.acquire(PID, fail_on_memory=True)   # ACL-2369: the gate that guards a NEW profile
    _fail_if_slow(started)
    # 16000 MB at the rig's 90 %: 1600 MB must stay free, 1000 free -> 600 MB more
    assert str(ei.value) == warm.memory_hold_text("600 MB") and ei.value.code in ("below_reserve", "memory_low")
    assert "safety reserve" not in str(ei.value)
    assert rig.gate.active() == [other]
    first.release()


def test_a_busy_account_gate_still_waits_but_memory_does_not(tmp_path, monkeypatch):
    monkeypatch.setenv(warm.GATE_WAIT_ENV, "1")
    rig = Rig(tmp_path, limit=1)
    a, b = "a" * 8 + "-0000-4000-8000-000000000000", "b" * 8 + "-0000-4000-8000-000000000000"
    rig.warm_up(a)
    rig.warm_up(b)
    first = rig.checkout(a)
    with pytest.raises(warm.WarmError) as ei:
        rig.checkout(b)
    assert ei.value.code == "gate_wait"
    first.release()


def test_starting_a_closed_profile_reports_memory_after_the_short_grace(tmp_path):
    rig = Rig(tmp_path, reg_memory=lambda: ps.MemoryReading(64000, 1600))
    res = rig.reg.open_one("Acc", entry("a" * 8 + "-0000-4000-8000-000000000000"))
    assert res["ok"] is False and res["code"] == "memory_low" and res["message"].startswith(warm.MEMORY_HOLD_MESSAGE + " (free about ")
    # ACL-2470: a short grace (fake clock), not the 2 minutes of the old wait
    assert 10 <= sum(rig.sleeps) <= 11.5, "re-checked for the 10 s grace only"


def test_check_plan_failure_carries_the_memory_reason_not_the_class_name():
    from src.analysis_agent import account_spend_api as api

    text, code = api._failure("Acc", api.ApiReadError(warm.MEMORY_HOLD_MESSAGE, code="below_reserve"))
    assert text == warm.MEMORY_HOLD_MESSAGE and code == "below_reserve" and "ApiReadError" not in text


def test_the_read_session_turns_the_memory_refusal_into_the_message(monkeypatch):
    from types import SimpleNamespace

    from src.analysis_agent import account_spend as sp
    from src.analysis_agent import account_spend_api as api
    from tests.test_acl1812_warm_sessions import ACT, PID

    ent = SimpleNamespace(profile_id=PID, folder_id="", multilogin_instance="A", ad_account_id=ACT)
    monkeypatch.setattr(sp, "_set_wait_note", lambda account, note: None)
    monkeypatch.setattr(sp, "_entry_for", lambda account: ent)

    class Reg:
        def is_warm(self, pid):
            return True

        def checkout(self, pid, **kw):
            raise warm.WarmError(warm.MEMORY_HOLD_MESSAGE, "below_reserve")

    monkeypatch.setattr(warm, "_REGISTRY", Reg())
    api._COOLDOWN.clear()
    with pytest.raises(api.ApiReadError) as ei:
        with api._session("Acc"):
            pass
    assert str(ei.value) == warm.MEMORY_HOLD_MESSAGE and ei.value.code == "below_reserve"


# --- ACL-2470: 10 s grace before the refusal (fake clocks, no real sleeping) ------------------------

OTHER = "6ba7b810-9dad-11d1-80b4-00c04fd430c8"


def _second_action_rig(rig):
    """ACL-2369: the first action on a warm profile is never held; only the SECOND simultaneous one is."""
    rig.warm_up()
    rig.warm_up(OTHER, account="Acc2")
    return rig.checkout(OTHER, account="Acc2")


def test_gate_memory_dip_that_recovers_within_the_grace_lets_the_second_action_go(tmp_path):
    rig = Rig(tmp_path, limit=2)
    first = _second_action_rig(rig)
    rig.gate_skew[0] = 0
    reads = []

    def dipping():
        reads.append(rig.gate_skew[0])
        # low for the first 4 s of the grace, then back to plenty
        return ps.MemoryReading(16000, 1000 if rig.gate_skew[0] < 4 else 9000)

    rig.gate._memory = dipping
    rig.gate.acquire(PID, fail_on_memory=True)
    assert sorted(rig.gate.active()) == sorted([PID, OTHER]) and 0 < rig.gate_skew[0] < 10 and len(reads) >= 3
    rig.gate.release(PID)
    first.release()


def test_gate_sustained_shortage_refuses_the_second_action_after_at_most_ten_seconds(tmp_path):
    rig = Rig(tmp_path, limit=2, memory=lambda: ps.MemoryReading(16000, 1000))
    first = _second_action_rig(rig)
    rig.gate_skew[0] = 0
    rig.gate_pauses.clear()
    with pytest.raises(warm.WarmError) as ei:
        rig.gate.acquire(PID, fail_on_memory=True)
    # 16000 MB at 90 %: 1600 MB must stay free, 1000 free -> 600 MB more
    assert str(ei.value) == warm.memory_hold_text("600 MB") and ei.value.code in ("below_reserve", "memory_low")
    assert 9.9 <= rig.gate_skew[0] <= 10.1 and all(0 < p <= 2 for p in rig.gate_pauses)
    assert rig.gate.active() == [OTHER]
    first.release()


def test_gate_refusal_log_of_the_second_action_keeps_the_numbers_of_acl_2400(tmp_path, caplog):
    import logging

    rig = Rig(tmp_path, limit=2, memory=lambda: ps.MemoryReading(16000, 1000))
    first = _second_action_rig(rig)
    with caplog.at_level(logging.INFO), pytest.raises(warm.WarmError):
        rig.gate.acquire(PID, fail_on_memory=True)
    first.release()
    assert any("memory holds the action" in r.getMessage() and "GB free" in r.getMessage() for r in caplog.records)


def test_gate_busy_account_wait_is_not_shortened_by_the_grace(tmp_path, monkeypatch):
    monkeypatch.setenv(warm.GATE_WAIT_ENV, "1")
    rig = Rig(tmp_path, limit=1)
    a, b = "a" * 8 + "-0000-4000-8000-000000000000", "b" * 8 + "-0000-4000-8000-000000000000"
    rig.warm_up(a)
    rig.warm_up(b)
    first = rig.checkout(a)
    with pytest.raises(warm.WarmError) as ei:
        rig.checkout(b)
    assert ei.value.code == "gate_wait" and rig.gate_pauses == []
    first.release()


def test_start_memory_dip_that_recovers_within_the_grace_lets_the_profile_start(tmp_path):
    rig = Rig(tmp_path)
    rig.reg._memory = lambda: ps.MemoryReading(64000, 1600 if sum(rig.sleeps) < 5 else 40000)
    res = rig.reg.open_one("Acc", entry("a" * 8 + "-0000-4000-8000-000000000000"))
    assert res["ok"] is True and 0 < sum(rig.sleeps) < 10


def test_a_queued_job_still_waits_past_the_grace(tmp_path):
    rig = Rig(tmp_path)
    free = [1600]

    def mem():
        if sum(rig.sleeps) > 60:
            free[0] = 40000
        return ps.MemoryReading(64000, free[0])

    rig.reg._memory = mem
    with warm.job_wait(lambda: False, lambda view: None):
        res = rig.reg._wait_for_memory(lambda: False)
    assert res == "" and sum(rig.sleeps) > 60
