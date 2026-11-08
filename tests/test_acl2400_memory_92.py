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
from tests.test_acl1812_warm_sessions import Rig, entry

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

    rig = Rig(tmp_path, memory=lambda: ps.MemoryReading(16000, 1000))   # default 300 s account gate wait
    rig.warm_up()
    started = time.monotonic()
    with pytest.raises(warm.WarmError) as ei:
        rig.checkout()
    _fail_if_slow(started)
    assert str(ei.value) == warm.MEMORY_HOLD_MESSAGE and ei.value.code in ("below_reserve", "memory_low")
    assert "safety reserve" not in str(ei.value)
    assert rig.gate.active() == []


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


def test_starting_a_closed_profile_reports_memory_at_once(tmp_path):
    rig = Rig(tmp_path, reg_memory=lambda: ps.MemoryReading(64000, 1600))
    res = rig.reg.open_one("Acc", entry("a" * 8 + "-0000-4000-8000-000000000000"))
    assert res["ok"] is False and res["code"] == "memory_low" and res["message"] == warm.MEMORY_HOLD_MESSAGE
    assert sum(rig.sleeps) == 0, "no waiting for memory"


def test_a_job_still_waits_queued_for_memory(tmp_path):
    # ACL-2278 is untouched: only actions nobody queued are refused at once (see test_acl2278_queue_not_fail).
    assert warm.START_MEMORY_WAIT_S > 0


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
