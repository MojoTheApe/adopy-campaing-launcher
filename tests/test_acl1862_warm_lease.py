"""ACL-1862: замок физического профиля у тёплого реестра — закрытие, запуск и аренда вкладки.

Держатель замка — настоящий второй процесс (общий OS-замок), MLX/вкладки/Graph подставные.
Живых профилей и аккаунтов нет.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from src.fb_ads_rpa import profile_lease as pl
from src.fb_api import warm
from tests.test_acl1812_warm_sessions import PID, Rig, entry

pytestmark = pytest.mark.real_profile_lease

OTHER = "7c9e6679-7425-40de-944b-e07fc1f90ae7"
ROOT = Path(__file__).resolve().parents[1]

HOLDER = textwrap.dedent("""
    import sys, time
    from pathlib import Path
    sys.path.insert(0, sys.argv[1])
    from src.fb_ads_rpa import profile_lease as pl
    root = Path(sys.argv[2])
    pl._lease_root = lambda: root
    lease = pl.profile_lock(sys.argv[3])
    assert lease.acquire(blocking=False)
    print("held", flush=True)
    time.sleep(float(sys.argv[4]))
""")


@pytest.fixture
def other_process(tmp_path, monkeypatch):
    root = tmp_path / "leases"
    root.mkdir()
    monkeypatch.setattr(pl, "_lease_root", lambda: root)
    started = []

    def hold(key, seconds=60):
        proc = subprocess.Popen([sys.executable, "-c", HOLDER, str(ROOT), str(root), key, str(seconds)],
                                stdout=subprocess.PIPE, text=True)
        assert proc.stdout.readline().strip() == "held"
        started.append(proc)
        return proc

    yield hold
    for proc in started:
        proc.kill()
        proc.wait(10)


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


def test_close_is_refused_while_another_process_holds_the_profile(rig, other_process):
    rig.warm_up()
    other_process(PID)
    res = rig.reg.close_one("Acc", entry())
    assert res["ok"] is False and res["code"] == "busy"
    assert rig.stopped == [], "the closer is never called under another owner"
    assert rig.reg.session(PID) is not None
    assert rig.reg.restart_wait_s(PID) == 0, "a refused close starts no pause"


def test_alias_spellings_of_the_profile_converge_to_one_owner(other_process):
    other_process(PID)
    for alias in (PID.upper(), "{" + PID + "}", " " + PID + " "):
        assert pl.physical_profile_key(alias) == PID
        assert not pl.profile_lock(alias).acquire(blocking=False), alias


def test_another_profile_is_not_blocked(rig, other_process):
    rig.warm_up(OTHER)
    other_process(PID)
    res = rig.reg.close_one("Acc", entry(OTHER))
    assert res["ok"] is True and rig.stopped == ["Acc"]


def test_close_goes_through_once_the_other_process_is_gone(rig, other_process):
    rig.warm_up()
    proc = other_process(PID)
    assert rig.reg.close_one("Acc", entry())["code"] == "busy"
    proc.kill()                 # crash: the OS frees the lock, no cleanup needed
    proc.wait(10)
    res = rig.reg.close_one("Acc", entry())
    assert res["ok"] is True and rig.stopped == ["Acc"]
    assert not pl.profile_lock(PID).held_by_current_thread(), "released after the stop"


def test_failed_stop_still_releases_the_lease(rig):
    rig.warm_up()

    def boom(entry_, label):
        raise RuntimeError("MLX 500")

    rig.reg._close_window = boom
    assert rig.reg.close_one("Acc", entry())["code"] == "stop_failed"
    assert not pl.profile_lock(PID).held_by_current_thread()
    assert pl.profile_lock(PID).acquire(blocking=False)
    pl.profile_lock(PID).release()


def test_open_does_not_start_a_profile_another_process_holds(rig, other_process):
    other_process(PID)
    res = rig.reg.open_one("Acc", entry())
    assert res["ok"] is False and res["code"] == "profile_lease"
    assert rig.started == [], "Multilogin start is never sent under another owner"


def test_open_starts_free_profile_and_releases_the_lease(rig):
    res = rig.reg.open_one("Acc", entry())
    assert res["ok"] is True and rig.started
    assert not pl.profile_lock(PID).held_by_current_thread()


def test_open_inside_a_held_lease_reenters_without_deadlock(rig):
    with pl.profile_lock(PID):
        res = rig.reg.open_one("Acc", entry())
        assert res["ok"] is True
        assert pl.profile_lock(PID).held_by_current_thread(), "the outer hold survives"


def test_failed_start_releases_the_lease(rig):
    rig.start_error = RuntimeError("boom")
    assert rig.reg.open_one("Acc", entry())["ok"] is False
    assert not pl.profile_lock(PID).held_by_current_thread()


def test_checkout_without_the_lease_is_refused_and_touches_nothing(rig):
    rig.warm_up()
    with pytest.raises(warm.WarmError) as err:
        rig.checkout()
    assert err.value.code == "profile_lease"
    assert rig.tabs == [] and rig.reg.session(PID).owner == 0


def test_checkout_with_the_lease_works_and_a_foreign_owner_blocks_acquiring_it(rig, other_process):
    rig.warm_up()
    other_process(PID)
    lease_obj = pl.profile_lock(PID)
    assert not lease_obj.acquire(blocking=False), "the caller cannot get the lease, so it cannot check out"
    with pytest.raises(warm.WarmError):
        rig.checkout()


def test_checkout_under_the_callers_lease_succeeds(rig):
    rig.warm_up()
    with pl.profile_lock(PID):
        lease = rig.checkout()
        lease.release()
    assert rig.reg.session(PID).owner == 0
