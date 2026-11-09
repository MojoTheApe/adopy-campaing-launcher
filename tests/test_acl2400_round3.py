"""ACL-2400 round 3: .env lock, byte-preserving migration, marker safety, queued jobs, real Check plan path.

Fakes and tmp dirs only: no real machine .env, no Multilogin, no Meta.
"""
from __future__ import annotations

import json
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from config import config_unify as cu
from src.fb_api import warm
from src.web import profile_slots as ps
from tests.test_acl1812_warm_sessions import Rig

KEY = "ACL_MEMORY_MAX_USED_PCT"


def _bytes_env(tmp_path: Path, data: bytes) -> Path:
    path = tmp_path / ".env"
    path.write_bytes(data)
    return path


def test_crlf_and_bom_survive_and_only_the_value_changes(tmp_path):
    before = b"\xef\xbb\xbfA=1\r\nACL_MEMORY_MAX_USED_PCT=75\r\nB=2\r\n"
    env = _bytes_env(tmp_path, before)
    assert cu.migrate_memory_threshold(tmp_path)
    assert env.read_bytes() == b"\xef\xbb\xbfA=1\r\nACL_MEMORY_MAX_USED_PCT=92\r\nB=2\r\n"
    backup = next((tmp_path / "backups").glob("memory-92-*/.env"))
    assert backup.read_bytes() == before


def test_bom_on_the_key_line_itself_survives(tmp_path):
    env = _bytes_env(tmp_path, b"\xef\xbb\xbfACL_MEMORY_MAX_USED_PCT=85\nB=2\n")
    cu.migrate_memory_threshold(tmp_path)
    assert env.read_bytes() == b"\xef\xbb\xbfACL_MEMORY_MAX_USED_PCT=92\nB=2\n"


def test_a_cp1251_file_comes_back_byte_identical_except_the_value(tmp_path):
    comment = "# память".encode("cp1251")
    env = _bytes_env(tmp_path, comment + b"\nACL_MEMORY_MAX_USED_PCT=75\nX=1")
    cu.migrate_memory_threshold(tmp_path)
    assert env.read_bytes() == comment + b"\nACL_MEMORY_MAX_USED_PCT=92\nX=1"


@pytest.mark.parametrize("line,expected", [
    (b"export ACL_MEMORY_MAX_USED_PCT=75\n", b"export ACL_MEMORY_MAX_USED_PCT=92\n"),
    (b"ACL_MEMORY_MAX_USED_PCT=75 # old\n", b"ACL_MEMORY_MAX_USED_PCT=92 # old\n"),
    (b'ACL_MEMORY_MAX_USED_PCT="85"\n', b'ACL_MEMORY_MAX_USED_PCT="92"\n'),
    (b"ACL_MEMORY_MAX_USED_PCT = '80'  # x\n", b"ACL_MEMORY_MAX_USED_PCT = '92'  # x\n"),
    (b"  ACL_MEMORY_MAX_USED_PCT=75", b"  ACL_MEMORY_MAX_USED_PCT=92"),
])
def test_every_form_dotenv_reads_is_migrated(tmp_path, line, expected):
    from dotenv import dotenv_values

    env = _bytes_env(tmp_path, line)
    assert float(dotenv_values(env)[KEY]) < 92
    assert cu.migrate_memory_threshold(tmp_path)
    assert env.read_bytes() == expected
    assert float(dotenv_values(env)[KEY]) == 92


def test_duplicates_all_below_92_change_and_the_last_wins(tmp_path):
    from dotenv import dotenv_values

    env = _bytes_env(tmp_path, b"ACL_MEMORY_MAX_USED_PCT=75\r\nACL_MEMORY_MAX_USED_PCT=85\r\n")
    cu.migrate_memory_threshold(tmp_path)
    assert env.read_bytes() == b"ACL_MEMORY_MAX_USED_PCT=92\r\nACL_MEMORY_MAX_USED_PCT=92\r\n"
    assert dotenv_values(env)[KEY] == "92"


def test_a_value_with_a_glued_hash_is_not_a_number_and_stays(tmp_path):
    env = _bytes_env(tmp_path, b"ACL_MEMORY_MAX_USED_PCT=75#x\n")
    assert cu.migrate_memory_threshold(tmp_path) is None
    assert env.read_bytes() == b"ACL_MEMORY_MAX_USED_PCT=75#x\n"


def test_read_edit_and_replace_happen_under_the_env_lock(tmp_path, monkeypatch):
    env = _bytes_env(tmp_path, f"{KEY}=75\n".encode())
    events: list[str] = []

    @contextmanager
    def fake_lock(path, timeout=None):
        events.append("lock:" + Path(path).name)
        yield
        events.append("unlock")

    real_write = cu.paths._atomic_write

    def spy(path, data):
        events.append("write:" + Path(path).name)
        return real_write(path, data)

    monkeypatch.setattr(cu.paths, "machine_file_lock", fake_lock)
    monkeypatch.setattr(cu.paths, "_atomic_write", spy)
    cu.migrate_memory_threshold(tmp_path)
    assert events[0] == "lock:.env.lock" and events[-1] == "unlock"
    assert events.index("write:.env") < events.index("unlock")
    assert env.read_bytes() == f"{KEY}=92\n".encode()


def test_a_write_by_another_process_before_the_lock_is_not_lost(tmp_path, monkeypatch):
    env = _bytes_env(tmp_path, f"{KEY}=75\n".encode())

    @contextmanager
    def lock_after_a_settings_save(path, timeout=None):
        env.write_bytes(f"{KEY}=75\nTOKEN=new\n".encode())     # a writer that held the lock first
        yield

    monkeypatch.setattr(cu.paths, "machine_file_lock", lock_after_a_settings_save)
    cu.migrate_memory_threshold(tmp_path)
    assert env.read_bytes() == f"{KEY}=92\nTOKEN=new\n".encode()


def test_a_broken_marker_counts_as_done_and_is_not_rewritten(tmp_path):
    env = _bytes_env(tmp_path, f"{KEY}=80\n".encode())
    marker = tmp_path / cu.MIGRATIONS_FILE
    marker.write_bytes(b"{not json")
    assert cu.migrate_memory_threshold(tmp_path) is None
    assert env.read_bytes() == f"{KEY}=80\n".encode() and marker.read_bytes() == b"{not json"
    marker.write_text("[]", encoding="utf-8")
    assert cu.migrate_memory_threshold(tmp_path) is None
    assert env.read_bytes() == f"{KEY}=80\n".encode()


def test_other_markers_are_kept_when_ours_is_added(tmp_path):
    _bytes_env(tmp_path, f"{KEY}=75\n".encode())
    (tmp_path / cu.MIGRATIONS_FILE).write_text(json.dumps({"other": {"at": "x"}}), encoding="utf-8")
    cu.migrate_memory_threshold(tmp_path)
    data = json.loads((tmp_path / cu.MIGRATIONS_FILE).read_text(encoding="utf-8"))
    assert set(data) == {"other", cu.MEMORY_PCT_MIGRATION_ID}


def test_a_failing_replace_leaves_env_no_temp_file_no_backup_and_is_retried(tmp_path, monkeypatch):
    before = f"{KEY}=75\n".encode()
    env = _bytes_env(tmp_path, before)
    real_write = cu.paths._atomic_write

    def boom(path, data):
        raise PermissionError("locked by another program")

    monkeypatch.setattr(cu.paths, "_atomic_write", boom)
    for _ in range(3):
        assert cu.migrate_memory_threshold(tmp_path) is None
    assert env.read_bytes() == before
    assert not list(tmp_path.glob("backups/memory-92-*"))
    assert not (tmp_path / cu.MIGRATIONS_FILE).exists()
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    monkeypatch.setattr(cu.paths, "_atomic_write", real_write)
    assert cu.migrate_memory_threshold(tmp_path)          # the next launch migrates
    assert env.read_bytes() == f"{KEY}=92\n".encode()


def test_a_busy_lock_means_not_done_and_nothing_changed(tmp_path, monkeypatch):
    before = f"{KEY}=75\n".encode()
    env = _bytes_env(tmp_path, before)

    @contextmanager
    def busy(path, timeout=None):
        raise TimeoutError("busy")
        yield  # pragma: no cover

    monkeypatch.setattr(cu.paths, "machine_file_lock", busy)
    assert cu.migrate_memory_threshold(tmp_path) is None
    assert env.read_bytes() == before and not (tmp_path / cu.MIGRATIONS_FILE).exists()


# --- a queued publish job really waits; Check plan carries the memory reason ----------------------------------

def test_a_queued_job_waits_at_the_gate_under_low_memory_and_goes_when_it_frees(tmp_path):
    free = [1000]
    rig = Rig(tmp_path, limit=2, memory=lambda: ps.MemoryReading(16000, free[0]))
    rig.warm_up()
    other = "660e8400-e29b-41d4-a716-446655440000"
    rig.warm_up(other, account="Acc2")
    holder = rig.checkout(other, account="Acc2")   # ACL-2369: the first action is never held; the next one is
    out: list = []
    err: list = []

    def run():
        try:
            with warm.job_wait(lambda: False, lambda view: None):
                out.append(rig.checkout())
        except BaseException as exc:  # noqa: BLE001
            err.append(exc)

    t = threading.Thread(target=run)
    t.start()
    time.sleep(0.5)
    assert out == [] and err == [] and t.is_alive(), "a job must stay queued, not fail, under low memory"
    free[0] = 12000
    t.join(5)
    assert err == [] and len(out) == 1
    out[0].release()
    holder.release()


def test_the_gate_refusal_logs_its_numbers(tmp_path, caplog):
    import logging

    rig = Rig(tmp_path, limit=2, memory=lambda: ps.MemoryReading(16000, 1000))
    rig.warm_up()
    other = "660e8400-e29b-41d4-a716-446655440000"
    rig.warm_up(other, account="Acc2")
    holder = rig.checkout(other, account="Acc2")   # ACL-2369: the first action is never held; the next one is
    with caplog.at_level(logging.INFO, logger=warm.log.name):
        with pytest.raises(warm.WarmError):
            rig.checkout()
    line = next(r.getMessage() for r in caplog.records if "memory holds the action" in r.getMessage())
    assert "GB free" in line and "% used" in line and "limit" in line


def test_check_plan_on_a_held_gate_returns_the_memory_text_and_code(tmp_path, monkeypatch):
    """The real Check plan path with a real gate refusing for memory (no session fake)."""
    from types import SimpleNamespace

    from src.analysis_agent import account_spend as sp
    from src.analysis_agent import account_spend_api as api
    from src.web import publish_api
    from tests.test_acl1761_api_create import _draft
    from tests.test_acl1761_plan_check import fake_profile
    from tests.test_acl1812_warm_sessions import ACT, PID

    rig = Rig(tmp_path, limit=2, memory=lambda: ps.MemoryReading(16000, 1000))
    rig.warm_up()
    other = "660e8400-e29b-41d4-a716-446655440000"
    rig.warm_up(other, account="Acc2")
    holder = rig.checkout(other, account="Acc2")   # ACL-2369: the first action is never held; the next one is
    ent = SimpleNamespace(profile_id=PID, folder_id="", multilogin_instance="A", ad_account_id=ACT)
    monkeypatch.setattr(sp, "_entry_for", lambda account: ent)
    monkeypatch.setattr(sp, "_set_wait_note", lambda account, note: None)
    monkeypatch.setattr(sp, "_profile_key", lambda account, *a, **k: fake_profile(account))
    monkeypatch.setattr(warm, "_REGISTRY", rig.reg)
    monkeypatch.setattr(api, "_warm_registry", lambda: rig.reg)
    monkeypatch.setattr("src.fb_api.connection.prepare_existing", lambda *a, **k: True)
    monkeypatch.setattr("src.analysis_agent.account_refresh_machine.verify_binding", lambda *a, **k: None)
    api._COOLDOWN.clear()
    d = _draft(tmp_path, ads=1)
    monkeypatch.setattr(publish_api, "build_drafts", lambda slots, **kw: [d])
    recorded: list = []
    monkeypatch.setattr(publish_api, "_record", lambda step, ok, campaign="", diag=None, duration_s=None:
                        recorded.append((step, ok, diag or {})))
    out = publish_api.check_plan([{"name": "x"}], account_label="Test", country="MX", force=True)
    assert out["error"] == warm.memory_hold_text("600 MB")
    assert out["connection_code"] in ("below_reserve", "memory_low")
    (step,) = [r for r in recorded if r[0] == "api_plan_check"]
    assert step[1] is False and step[2]["code"] == out["connection_code"]
    assert step[2]["reason"] == warm.memory_hold_text("600 MB")
