"""ACL-1165: a failed intermediate attach attempt is not logged as the result.

Real `_try_attach_existing` runs; only the process/port probes and the CDP
connection are replaced. Behavior (the relaunch refusal and the
FB_RPA_ALLOW_UNPROXIED_RELAUNCH gate) is covered by test_browser_relaunch_gate.py.
"""
from __future__ import annotations

import logging
from unittest.mock import patch

import pytest

from src.fb_ads_rpa import browser as br
from src.fb_ads_rpa.browser import MultiloginDirectCDPProvider

PID = "p-attach-log-test"
NAME = "src.fb_ads_rpa.browser"


class _Entry:
    profile_id = PID


def _provider(tmp_path, *, owned_port, loose_port):
    p = MultiloginDirectCDPProvider()
    p._find_profile_user_data_dir = lambda pid: tmp_path / "profile"  # type: ignore[method-assign]
    p._find_mlx_chrome = lambda udd: str(tmp_path / "chrome.exe")  # type: ignore[method-assign]
    p._profile_process_running = lambda *a, **k: True  # type: ignore[method-assign]
    p._discover_owned_debug_port = lambda udd: owned_port  # type: ignore[method-assign]
    p._discover_debug_port = lambda udd, pid: loose_port  # type: ignore[method-assign]
    return p


def _messages(caplog):
    return [(r.levelno, r.getMessage()) for r in caplog.records if r.name == NAME]


def test_strict_miss_then_lenient_hit_never_says_unavailable(tmp_path, caplog, monkeypatch):
    caplog.set_level(logging.DEBUG, logger=NAME)
    p = _provider(tmp_path, owned_port=None, loose_port=59502)
    with patch.object(br, "_connect_playwright", return_value="conn"):
        assert p.attach_existing_only(PID, str(tmp_path)) is None  # strict
        assert p._try_attach_existing(PID, str(tmp_path)) == "conn"  # lenient

    msgs = _messages(caplog)
    assert not any("unavailable" in m for _, m in msgs)
    strict = [(lv, m) for lv, m in msgs if "strict attempt found no own CDP port" in m]
    assert len(strict) == 1 and strict[0][0] < logging.WARNING
    assert "not final" in strict[0][1]
    assert any("Reusing already-running Mimic profile" in m and "59502" in m for _, m in msgs)
    assert max(lv for lv, _ in msgs) < logging.WARNING


def test_all_paths_failing_ends_in_one_warning_that_says_unavailable(tmp_path, caplog, monkeypatch):
    monkeypatch.delenv("FB_RPA_ALLOW_UNPROXIED_RELAUNCH", raising=False)
    caplog.set_level(logging.DEBUG, logger=NAME)
    p = _provider(tmp_path, owned_port=None, loose_port=None)
    with patch.object(p, "_kill_profile_browser") as kill:
        assert p.attach_existing_only(PID, str(tmp_path)) is None  # strict
        with pytest.raises(RuntimeError, match="refusing to relaunch"):  # lenient, then gate
            p.open(_Entry(), "KLD_BFL_X84")
    kill.assert_not_called()

    msgs = _messages(caplog)
    unavailable = [(lv, m) for lv, m in msgs if "attachment unavailable" in m]
    assert len(unavailable) == 1 and unavailable[0][0] == logging.WARNING
    assert "refusing to relaunch" in unavailable[0][1]
    # the two intermediate steps are quieter than the verdict and carry no verdict wording
    steps = [(lv, m) for lv, m in msgs if "attempt found no" in m]
    assert len(steps) == 2 and all(lv < logging.WARNING for lv, _ in steps)
    assert not any("unavailable" in m for _, m in steps)


def test_a_closed_profile_logs_no_attach_failure_at_all(tmp_path, caplog):
    caplog.set_level(logging.DEBUG, logger=NAME)
    p = _provider(tmp_path, owned_port=None, loose_port=None)
    p._profile_process_running = lambda *a, **k: False  # type: ignore[method-assign]
    assert p._try_attach_existing(PID, str(tmp_path), strict_profile=True) is None
    assert _messages(caplog) == []
