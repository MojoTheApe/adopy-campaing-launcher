# -*- coding: utf-8 -*-
"""MLX shows the profile as running and refuses with "can't lock": one stop, one reopen (ACL-2300).

Owner decision 2026-10-29: when the start answers "can't lock" and /profile/statuses lists the
profile as browser_running, the robot stops it once through the documented launcher stop and
starts it again (the ACL-1388 soft stop and reopen path). An unknown status is not touched.
A second refusal ends with a message and no second stop.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import browser as br  # noqa: E402
from src.fb_ads_rpa import hand_window as hw  # noqa: E402
from src.fb_ads_rpa import profile_trouble as pt  # noqa: E402
from src.meta_campaign_agent.multilogin_launcher import (  # noqa: E402
    MultiloginLauncher as _RealLauncher,
    MultiloginLauncherError,
    StartedProfile,
)

PROFILE = "550e8400-e29b-41d4-a716-446655440000"
CANT_LOCK = "Start profile failed HTTP 500: can't lock profile"


class _Http:
    def __init__(self, status, payload=None):
        self.status_code, self._p, self.text = status, payload, ""

    def json(self):
        if self._p is None:
            raise ValueError("no json")
        return self._p


class _Launcher(_RealLauncher):
    """The real status reader over a fake HTTP answer; start and stop are scripted."""

    def __init__(self, world):
        super().__init__(SimpleNamespace(launcher_base_v1="http://l/api/v1", launcher_base_v2="http://l/api/v2",
                                         automation_token="t"))
        self.world = world

    def start_profile(self, folder_id, profile_id, *, headless=False):
        self.world.ev.append("start")
        if self.world.locked_starts > 0:
            self.world.locked_starts -= 1
            raise MultiloginLauncherError(CANT_LOCK, status=500)
        return StartedProfile(profile_id=profile_id, folder_id=folder_id, port="58906")

    def stop_profile(self, profile_id, folder_id=None):
        self.world.ev.append("stop")


@pytest.fixture
def world(monkeypatch):
    import src.meta_campaign_agent.multilogin_config as cfg_mod
    import src.meta_campaign_agent.multilogin_launcher as lm

    w = SimpleNamespace(ev=[], locked_starts=1, status="browser_running", telemetry=[])
    monkeypatch.setenv(hw.ENV_ENABLED, "1")
    monkeypatch.setattr(br, "_ROBOT_PROFILES", {})
    monkeypatch.setattr(br.time, "sleep", lambda s: None)
    monkeypatch.setattr(br, "_find_mlx_profile_dir", lambda pid: "C:/mlx/p")
    monkeypatch.setattr(br, "_profile_pids", lambda udd, pid: frozenset())
    monkeypatch.setattr(br, "_kill_chrome_for_user_data_dir", lambda *a, **k: pytest.fail("killed"))
    monkeypatch.setattr(br, "_tell_job", lambda t: None)
    monkeypatch.setattr(br, "_record_hand_window", lambda *a, **k: None)
    monkeypatch.setattr(br, "_record_lock_release", lambda pid, **kw: w.telemetry.append(kw))
    monkeypatch.setattr(cfg_mod, "get_multilogin_instance", lambda inst: SimpleNamespace(instance_id=inst))
    monkeypatch.setattr(lm, "MultiloginLauncher", lambda cfg: _Launcher(w))

    def get(url, headers=None, timeout=None, **kw):
        if w.status is None:
            return _Http(404)
        return _Http(200, {"data": {"states": {PROFILE: {"profile_id": PROFILE, "status": w.status}}}})

    monkeypatch.setattr(lm.requests, "get", get)
    prov = br.MultiloginAttachOnlyProvider()
    monkeypatch.setattr(prov._direct, "_try_attach_existing", lambda *a, **kw: None)
    monkeypatch.setattr(prov._direct, "_profile_process_running", lambda *a: False)
    monkeypatch.setattr(prov, "_connect_started",
                        lambda started, pid: w.ev.append("connect") or SimpleNamespace(page="reopened-page"))
    w.prov = prov
    return w


def _open(world):
    entry = SimpleNamespace(profile_id=PROFILE, folder_id="f-1", multilogin_instance="A")
    return world.prov.open(entry, "Facebook_Doc122")


def test_browser_running_and_cant_lock_is_stopped_once_and_reopened(world):
    assert _open(world).page == "reopened-page"
    assert world.ev == ["start", "stop", "start", "connect"]
    assert world.telemetry == [{"ok": True, "note": "browser_running"}]


def test_a_second_refusal_ends_with_a_message_and_no_second_stop(world):
    world.locked_starts = 99
    with pytest.raises(pt.ProfileStop) as stop:
        _open(world)
    assert stop.value.code == pt.RESTART_FAILED
    assert world.ev.count("stop") == 1
    assert world.telemetry == [{"ok": False, "note": "browser_running"}]


@pytest.mark.parametrize("status", ["locked", None])
def test_unknown_status_is_not_touched_and_says_click_locked(world, status):
    world.status = status
    with pytest.raises(pt.ProfileStop) as stop:
        _open(world)
    assert stop.value.code == pt.OPEN_BY_HAND
    assert "Locked" in str(stop.value)
    assert world.ev == ["start"]
