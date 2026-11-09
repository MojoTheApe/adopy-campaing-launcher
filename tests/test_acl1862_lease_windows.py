"""ACL-1862 (PR1): the same close/open lease checks against a real second process.

Windows only: the file lock is held by a spawned process (CI splits by file).
"""
from __future__ import annotations

import pytest

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
