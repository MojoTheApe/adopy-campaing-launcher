# -*- coding: utf-8 -*-
"""A profile stopped by hand stays locked in Multilogin X; the robot releases the lock once (ACL-2300).

Live, 2026-10-29 (Doc122): "Start profile failed HTTP 500: can't lock profile" three times
although no browser of the profile ran here. The owner had stopped it by hand and MLX Desktop
showed "Locked". Now: "can't lock" + no local process -> one release through the launcher, one
more start. Running locally, release failed, or any other 500 -> no loop.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import browser as br  # noqa: E402
from src.fb_ads_rpa import profile_trouble as pt  # noqa: E402
from src.meta_campaign_agent.multilogin_launcher import (  # noqa: E402
    MultiloginLauncher as _RealLauncher,
    MultiloginLauncherError,
    StartedProfile,
)

PROFILE = "550e8400-e29b-41d4-a716-446655440000"
CANT_LOCK = "Start profile failed HTTP 500: can't lock profile"


class _Launcher:
    def __init__(self, start_errors, unlock_error=None, mlx_state="stopped"):
        self.mlx_state = mlx_state
        self.start_errors = list(start_errors)
        self.unlock_error = unlock_error
        self.starts = 0
        self.unlocks = 0
        self.stops = 0

    def start_profile(self, folder_id, profile_id, *, headless=False):
        self.starts += 1
        if self.start_errors:
            raise MultiloginLauncherError(self.start_errors.pop(0))
        return StartedProfile(profile_id=profile_id, folder_id=folder_id, port="58906")

    def profile_run_state(self, profile_id):
        return self.mlx_state

    def release_stale_lock(self, profile_id, folder_id=None):
        self.unlocks += 1
        if self.unlock_error:
            raise MultiloginLauncherError(self.unlock_error)

    def stop_profile(self, profile_id, folder_id=None):
        self.stops += 1


@pytest.fixture
def world(monkeypatch):
    import src.meta_campaign_agent.multilogin_config as cfg_mod
    import src.meta_campaign_agent.multilogin_launcher as launcher_mod

    w = SimpleNamespace(fake=None, running=False, telemetry=[], told=[])
    monkeypatch.setattr(cfg_mod, "get_multilogin_instance", lambda inst: SimpleNamespace(instance_id=inst))
    monkeypatch.setattr(launcher_mod, "MultiloginLauncher", lambda cfg: w.fake)
    monkeypatch.setattr(br, "_ROBOT_PROFILES", {})
    monkeypatch.setattr(br.time, "sleep", lambda s: None)
    monkeypatch.setattr(br.MultiloginDirectCDPProvider, "profile_is_running",
                        lambda self, udd, pid="": w.running)
    monkeypatch.setattr(br, "_record_lock_release",
                        lambda pid, **kw: w.telemetry.append(kw))
    monkeypatch.setattr(br, "_tell_job", lambda text: w.told.append(text))
    monkeypatch.setattr(br, "_kill_chrome_for_user_data_dir", lambda *a, **k: pytest.fail("killed"))
    return w


def _start():
    entry = SimpleNamespace(profile_id=PROFILE, folder_id="f-1", multilogin_instance="A")
    return br.MultiloginAttachOnlyProvider()._start_via_mlx_api(entry, "Facebook_Doc122", PROFILE, "C:/mlx/p")


def test_locked_and_not_running_is_released_then_started(world):
    world.fake = _Launcher([CANT_LOCK])
    started = _start()
    assert started.port == "58906"
    assert (world.fake.unlocks, world.fake.starts, world.fake.stops) == (1, 2, 0)
    assert world.telemetry == [{"ok": True, "note": "released"}]
    assert any("released" in t for t in world.told)


def test_locked_and_running_locally_is_not_released(world):
    world.running = True
    world.fake = _Launcher([CANT_LOCK])
    with pytest.raises(pt.ProfileStop) as stop:
        _start()
    assert stop.value.code == pt.OPEN_BY_HAND
    assert world.fake.unlocks == 0 and world.telemetry == []


def test_a_failed_release_stops_with_a_clear_message_and_no_loop(world):
    world.fake = _Launcher([CANT_LOCK] * 3, unlock_error="HTTP 500: boom")
    with pytest.raises(RuntimeError) as err:
        _start()
    assert "MLX Desktop" in str(err.value) and "Locked" in str(err.value)
    assert (world.fake.unlocks, world.fake.starts) == (1, 1)
    assert world.telemetry[0]["ok"] is False


def test_still_locked_after_one_release_stops_without_a_second_release(world):
    world.fake = _Launcher([CANT_LOCK] * 3)
    with pytest.raises(RuntimeError) as err:
        _start()
    assert "MLX Desktop" in str(err.value)
    assert (world.fake.unlocks, world.fake.starts) == (1, 2)


def test_other_http_500_errors_are_unchanged(world):
    world.fake = _Launcher(["Start profile failed HTTP 500: proxy check failed"] * 3)
    with pytest.raises(RuntimeError) as err:
        _start()
    assert "proxy check failed" in str(err.value)
    assert world.fake.unlocks == 0 and world.fake.starts == 1


def test_multilogin_status_unknown_or_running_means_no_release(world):
    for mlx_state in (None, "running"):
        world.fake = _Launcher([CANT_LOCK], mlx_state=mlx_state)
        with pytest.raises(pt.ProfileStop):
            _start()
        assert world.fake.unlocks == 0


# --- the real launcher method against the documented /profile/statuses shape -------------------

def _real_launcher(monkeypatch, reply):
    import src.meta_campaign_agent.multilogin_launcher as lm

    calls = []

    def get(url, headers=None, timeout=None, **kw):
        calls.append(url)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(lm.requests, "get", get)
    cfg = SimpleNamespace(launcher_base_v1="http://l/api/v1", launcher_base_v2="http://l/api/v2",
                          automation_token="t")
    launcher = _RealLauncher(cfg)
    launcher._calls = calls
    return launcher


class _Http:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._p = payload
        self.text = ""

    def json(self):
        if self._p is None:
            raise ValueError("no json")
        return self._p


def _states(status, key=PROFILE):
    return _Http(200, {"data": {"states": {key: {"profile_id": key, "status": status}}}})


@pytest.mark.parametrize("status", ["running", "starting", "stopping", "browser_running", "browser_stopping"])
def test_running_like_statuses_mean_running(monkeypatch, status):
    ln = _real_launcher(monkeypatch, _states(status))
    assert ln.profile_run_state(PROFILE) == "running"
    assert ln._calls == ["http://l/api/v1/profile/statuses"]


def test_stopped_entry_and_absent_profile_mean_stopped(monkeypatch):
    assert _real_launcher(monkeypatch, _states("stopped")).profile_run_state(PROFILE) == "stopped"
    assert _real_launcher(monkeypatch, _states("running", key="other-profile")).profile_run_state(PROFILE) == "stopped"
    assert _real_launcher(monkeypatch, _Http(200, {"data": {"states": {}}})).profile_run_state(PROFILE) == "stopped"


@pytest.mark.parametrize("reply", [_states("locked"), _states(""), _states(None), _states(5),
                                   _Http(401, {"status": {}}), _Http(404), _Http(200, {"data": {"states": "x"}}),
                                   _Http(200, {"data": {}}), _Http(200, {}), _Http(200, ["bad"]),
                                   ConnectionError("down")])
def test_anything_else_is_unknown(monkeypatch, reply):
    assert _real_launcher(monkeypatch, reply).profile_run_state(PROFILE) is None


def test_real_launcher_end_to_end_releases_on_a_stopped_entry(world, monkeypatch):
    ln = _real_launcher(monkeypatch, _states("stopped"))
    ln.start_profile = _Launcher([CANT_LOCK]).start_profile
    released = []
    ln.release_stale_lock = lambda pid, folder_id=None: released.append(pid)
    world.fake = ln
    assert _start().port == "58906"
    assert released == [PROFILE]


@pytest.mark.parametrize("status", ["browser_stopping", "locked"])
def test_real_launcher_end_to_end_no_release_when_running_or_unknown(world, monkeypatch, status):
    ln = _real_launcher(monkeypatch, _states(status))
    ln.start_profile = _Launcher([CANT_LOCK]).start_profile
    released = []
    ln.release_stale_lock = lambda pid, folder_id=None: released.append(pid)
    world.fake = ln
    with pytest.raises(pt.ProfileStop):
        _start()
    assert released == []


def test_held_on_another_machine_says_click_locked(world):
    world.fake = _Launcher(["Start profile failed HTTP 500: can't lock profile, in use by another device"])
    with pytest.raises(RuntimeError) as err:
        _start()
    assert "Locked" in str(err.value) and world.fake.unlocks == 0


