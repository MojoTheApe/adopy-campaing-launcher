"""ACL-1862 (PR1): the same close/open lease checks against a real second process.

Windows only: the file lock is held by a spawned process (CI splits by file).
"""
from __future__ import annotations

import pytest

from src.fb_ads_rpa import operations
from src.fb_ads_rpa.operations import profile_lock, serialized_browser
from src.fb_ads_rpa.profile_lease import ProfileLeaseError
from src.fb_api import warm
from tests.test_profile_process_lease import OTHER

from tests.test_acl1812_warm_sessions import PID, Rig, entry
from tests.test_profile_process_lease import process_factory  # noqa: F401 — фикстура

pytestmark = pytest.mark.windows_only(reason="Real file lock held by a spawned process")


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


def _warm_ours(rig):
    rig.warm_up()
    rig.reg.register(PID, account="Acc", port=9333, dashboard_started=True)


def test_other_process_holding_the_profile_blocks_close_and_open(rig, process_factory):  # noqa: F811
    proc, entered, release, _result = process_factory(PID, mode="hold")
    assert entered.wait(15)
    _warm_ours(rig)
    assert rig.reg.close_one("Acc", entry())["code"] == "busy"
    assert rig.stopped == []
    rig.scan_result[PID] = ("none", None)
    assert rig.reg.open_one("Acc", entry())["code"] == "profile_lease"
    assert rig.started == []
    release.set()
    proc.join(10)
    rig.scan_result[PID] = ("port", 9333)
    assert rig.reg.close_one("Acc", entry())["code"] == "closed"


def test_killed_holder_does_not_block_the_next_close(rig, process_factory):  # noqa: F811
    proc, entered, _release, _result = process_factory(PID, mode="hold")
    assert entered.wait(15)
    _warm_ours(rig)
    assert rig.reg.close_one("Acc", entry())["code"] == "busy"
    proc.terminate()
    proc.join(10)
    assert rig.reg.close_one("Acc", entry())["code"] == "closed"


# ───────────────────────── part 2: checkout, reads, leaks (mixed paths) ─────────────────────────

def test_read_in_this_process_excludes_the_other_process_and_releases(process_factory):  # noqa: F811
    @serialized_browser(key=lambda profile: profile, reading=True)
    def read(profile):
        _, _, _, result = process_factory(profile)
        return result.get(timeout=20)

    assert read(PID) is False                 # во время чтения чужой процесс замок не получил
    _, _, _, after = process_factory(PID)
    assert after.get(timeout=20) is True


def test_read_into_a_profile_held_by_the_other_process_is_refused(process_factory, monkeypatch):  # noqa: F811
    proc, entered, release, _ = process_factory(PID, mode="hold")
    assert entered.wait(15)
    monkeypatch.setattr(operations, "READ_WAIT_SECONDS", 0.3)
    called = []

    @serialized_browser(key=lambda profile: profile, reading=True)
    def read(profile):
        called.append(profile)

    with pytest.raises(ProfileLeaseError):
        read(PID)
    assert called == []
    read(OTHER)                               # другой профиль идёт независимо
    assert called == [OTHER]
    release.set()
    proc.join(10)


def test_checkout_needs_the_lease_and_shares_it_with_the_other_process(tmp_path, process_factory, strict_checkout_lease):  # noqa: F811
    rig = Rig(tmp_path)
    _warm_ours(rig)
    with pytest.raises(warm.WarmError) as err:
        rig.checkout()
    assert err.value.code == "profile_lease"
    with profile_lock(PID):
        _, _, _, result = process_factory(PID)
        assert result.get(timeout=20) is False   # checkout и чужой процесс - один замок
        rig.checkout().release()


def test_warm_session_refuses_while_the_other_process_holds_and_never_checks_out(process_factory, monkeypatch):  # noqa: F811
    from src.analysis_agent import account_spend_api as api

    proc, entered, release, _ = process_factory(PID, mode="hold")
    assert entered.wait(15)
    monkeypatch.setattr(operations, "READ_WAIT_SECONDS", 0.3)
    monkeypatch.setattr(api, "_warm_session_held", lambda *a: pytest.fail("checkout must not be reached"))
    with pytest.raises(api.ApiReadError) as err:
        with api._warm_session("Acc", "act_1", "k", PID, False):
            pass
    assert err.value.code == "profile_lease"
    release.set()
    proc.join(10)


def test_leaked_lease_is_cleared_so_the_other_process_can_enter(process_factory):  # noqa: F811
    @serialized_browser(key=lambda profile: profile, reading=True)
    def read(profile):
        profile_lock(profile).acquire()       # утечка: взял и не отпустил

    read(PID)
    _, _, _, result = process_factory(PID)
    assert result.get(timeout=20) is True
