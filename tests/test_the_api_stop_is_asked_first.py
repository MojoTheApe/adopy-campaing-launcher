# -*- coding: utf-8 -*-
"""Открытый профиль останавливается через MLX API, а не убийством Mimic (acl-1203).

Первый живой прогон после ACL-1200 (FacebookDQ501, 16.09 12:08):

    12:08:35  Stopping it via MLX API and restarting with automation enabled…
    12:08:36  MLX stop_profile failed (HTTP 404: 404 page not found);
              killing Mimic for this profile only
    12:08:44  Connected via MLX automation CDP … port 58906

Прогон выжил — на запасном пути. Все три адреса, которые пробовал
`stop_profile`, отвечали «404 page not found»: маршрута нет. Настоящий адрес
из коллекции Postman самого Multilogin — `GET /api/v1/profile/stop/p/{id}`;
на этом лаунчере (12.13.0) он отвечает структурно даже для несуществующего
профиля: 500 «profile already stopped».

Проверяется: правильный адрес спрашивается первым; 2xx — конец; «already
stopped» — тоже конец, а не ошибка; 404 записывается с адресом и уходит к
следующему; kill в browser.py вызывается только когда API не смог.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.meta_campaign_agent.multilogin_config import MultiloginInstanceConfig  # noqa: E402
from src.meta_campaign_agent.multilogin_launcher import (  # noqa: E402
    MultiloginLauncher,
    MultiloginLauncherError,
)

PROFILE = "550e8400-e29b-41d4-a716-446655440000"
FOLDER = "6ba7b810-9dad-11d1-80b4-00c04fd430c8"
STOP = f"/api/v1/profile/stop/p/{PROFILE}"


class _Reply:
    def __init__(self, status_code: int, payload=None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or (str(payload) if payload is not None else "")

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def _routing_miss() -> _Reply:
    return _Reply(404, None, "404 page not found")


def _already_stopped() -> _Reply:
    return _Reply(500, {"status": {"error_code": "INTERNAL_SERVER_ERROR",
                                   "http_code": 500, "message": "profile already stopped"}})


@pytest.fixture
def launcher(monkeypatch):
    """Лаунчер с подменённой сетью: отвечает по таблице и записывает вопросы."""
    import src.meta_campaign_agent.multilogin_launcher as mod

    asked: list[str] = []
    table: dict[str, _Reply] = {}

    def fake_get(url, headers=None, params=None, timeout=None, **kw):
        path = url.split("45001", 1)[-1]
        asked.append(path)
        return table.get(path, _routing_miss())

    monkeypatch.setattr(mod.requests, "get", fake_get)
    cfg = MultiloginInstanceConfig(instance_id="A", automation_token="t",
                                   launcher_base_v2="https://launcher.mlx.yt:45001/api/v2")
    return {"launcher": MultiloginLauncher(cfg), "asked": asked, "table": table}


# ── то, что было сломано ──────────────────────────────────────────────────

def test_the_documented_address_is_asked_first(launcher):
    launcher["table"][STOP] = _Reply(200, {"status": {"http_code": 200}})
    launcher["launcher"].stop_profile(PROFILE, folder_id=FOLDER)
    assert launcher["asked"][0] == STOP


def test_a_stop_that_works_asks_nothing_else(launcher):
    launcher["table"][STOP] = _Reply(200, {"status": {"http_code": 200}})
    launcher["launcher"].stop_profile(PROFILE, folder_id=FOLDER)
    assert launcher["asked"] == [STOP]


def test_the_address_has_no_folder_in_it(launcher):
    """В отличие от start: папка в пути stop не нужна и её там нет."""
    launcher["table"][STOP] = _Reply(200, {"status": {"http_code": 200}})
    launcher["launcher"].stop_profile(PROFILE, folder_id=FOLDER)
    assert FOLDER not in launcher["asked"][0]


def test_already_stopped_is_done_not_an_error(launcher):
    """Хотели, чтобы профиль не работал, — он и не работает."""
    launcher["table"][STOP] = _already_stopped()
    launcher["launcher"].stop_profile(PROFILE, folder_id=FOLDER)
    assert launcher["asked"] == [STOP]


# ── когда и этого адреса нет ──────────────────────────────────────────────

def test_a_routing_miss_falls_through_and_finally_raises(launcher):
    with pytest.raises(MultiloginLauncherError) as err:
        launcher["launcher"].stop_profile(PROFILE, folder_id=FOLDER)
    assert launcher["asked"][0] == STOP
    assert len(launcher["asked"]) > 1, "старые адреса остались хвостом"
    assert "404" in str(err.value)


def test_a_routing_miss_is_logged_with_its_address(launcher, caplog):
    """Следующий переезд API читается из лога, а не открывается заново."""
    with caplog.at_level(logging.INFO, logger="src.meta_campaign_agent.multilogin_launcher"):
        with pytest.raises(MultiloginLauncherError):
            launcher["launcher"].stop_profile(PROFILE, folder_id=FOLDER)
    lines = [r.getMessage() for r in caplog.records if "not served" in r.getMessage()]
    assert lines and f"v1/profile/stop/p/{PROFILE}" in lines[0]


def test_a_real_refusal_stops_the_search(launcher):
    """Не-404 — это ответ, а не отсутствие маршрута; дальше не перебираем."""
    launcher["table"][STOP] = _Reply(401, {"status": {"http_code": 401, "message": "Wrong JWT token"}})
    with pytest.raises(MultiloginLauncherError) as err:
        launcher["launcher"].stop_profile(PROFILE, folder_id=FOLDER)
    assert launcher["asked"] == [STOP]
    assert "Wrong JWT token" in str(err.value)


# ── что делает вызывающий: kill только когда API не смог ──────────────────

class _Launcher:
    def __init__(self, stop_ok: bool):
        self.stop_ok = stop_ok
        self.stops = 0
        self.starts = 0

    def start_profile(self, folder_id, profile_id, *, headless=False):
        self.starts += 1
        if self.starts == 1:
            raise MultiloginLauncherError("Start profile failed HTTP 401: can't lock profile")
        from src.meta_campaign_agent.multilogin_launcher import StartedProfile

        return StartedProfile(profile_id=profile_id, folder_id=folder_id, port="58906")

    def stop_profile(self, profile_id, folder_id=None):
        self.stops += 1
        if not self.stop_ok:
            raise MultiloginLauncherError("Stop profile failed HTTP 404: 404 page not found")


class _Entry:
    profile_id = PROFILE
    folder_id = FOLDER
    multilogin_instance = "A"


@pytest.fixture
def attach_provider(monkeypatch):
    import src.fb_ads_rpa.browser as browser_mod

    kills: list[str] = []
    monkeypatch.setattr(browser_mod, "_kill_chrome_for_user_data_dir",
                        lambda udd, pid, mlx_only=True: kills.append(pid))
    monkeypatch.setattr(browser_mod.time, "sleep", lambda s: None)
    import src.meta_campaign_agent.multilogin_config as cfg_mod

    monkeypatch.setattr(cfg_mod, "get_multilogin_instance",
                        lambda inst: MultiloginInstanceConfig("A", "t", "https://launcher.mlx.yt:45001/api/v2"))
    holder: dict = {}

    def make_launcher(cfg):
        return holder["launcher"]

    import src.meta_campaign_agent.multilogin_launcher as launcher_mod

    monkeypatch.setattr(launcher_mod, "MultiloginLauncher", make_launcher)
    # ACL-1320: останавливать и поднимать заново робот вправе только профиль,
    # который сам поднимал раньше; открытый руками — `profile_open_by_hand`
    # (tests/test_a_hand_opened_profile_is_never_stopped.py).
    monkeypatch.setattr(browser_mod, "_ROBOT_PROFILES", {PROFILE: frozenset()})
    return {"provider": browser_mod.MultiloginAttachOnlyProvider(), "kills": kills, "holder": holder}


def test_when_the_api_stops_the_profile_nothing_is_killed(attach_provider):
    fake = _Launcher(stop_ok=True)
    attach_provider["holder"]["launcher"] = fake
    started = attach_provider["provider"]._start_via_mlx_api(_Entry(), "DQ501", PROFILE, r"C:\mlx\p")  # linux-ok: a Windows path as opaque data, never touched on disk
    assert started.port == "58906"
    assert fake.stops == 1
    assert attach_provider["kills"] == []


def test_when_the_api_cannot_stop_the_kill_still_carries_the_run(attach_provider):
    """Запасной путь остаётся: именно он вывез прогон 16.09."""
    fake = _Launcher(stop_ok=False)
    attach_provider["holder"]["launcher"] = fake
    started = attach_provider["provider"]._start_via_mlx_api(_Entry(), "DQ501", PROFILE, r"C:\mlx\p")  # linux-ok: a Windows path as opaque data, never touched on disk
    assert started.port == "58906"
    assert attach_provider["kills"] == [PROFILE]


def test_the_log_tells_the_stop_from_the_kill(attach_provider, caplog):
    fake = _Launcher(stop_ok=True)
    attach_provider["holder"]["launcher"] = fake
    with caplog.at_level(logging.INFO, logger="src.fb_ads_rpa.browser"):
        attach_provider["provider"]._start_via_mlx_api(_Entry(), "DQ501", PROFILE, r"C:\mlx\p")  # linux-ok: a Windows path as opaque data, never touched on disk
    assert any("stopped through the MLX API" in r.getMessage() for r in caplog.records)
    assert not any("killing Mimic" in r.getMessage() for r in caplog.records)


def test_a_read_never_kills_when_the_api_stop_fails(attach_provider):
    fake = _Launcher(stop_ok=False)
    attach_provider["holder"]["launcher"] = fake
    with pytest.raises(RuntimeError, match="Nothing was closed"):
        attach_provider["provider"]._start_via_mlx_api(
            _Entry(), "Example", PROFILE, r"C:\mlx\p", allow_kill=False)  # linux-ok: a Windows path as opaque data, never touched on disk
    assert fake.stops == 1 and fake.starts == 1
    assert attach_provider["kills"] == []


def test_a_read_can_restart_gracefully_without_kill_permission(attach_provider):
    fake = _Launcher(stop_ok=True)
    attach_provider["holder"]["launcher"] = fake
    started = attach_provider["provider"]._start_via_mlx_api(
        _Entry(), "Example", PROFILE, r"C:\mlx\p", allow_kill=False)  # linux-ok: a Windows path as opaque data, never touched on disk
    assert started.port == "58906"
    assert fake.stops == 1 and fake.starts == 2
    assert attach_provider["kills"] == []


# ── ACL-2040: один сбой основного адреса не оставляет окно открытым ───────

@pytest.fixture
def flaky(monkeypatch):
    """Основной адрес сначала рвётся (как 16.10 23:10 на Doc120), потом отвечает."""
    import src.meta_campaign_agent.multilogin_launcher as mod

    asked: list[str] = []
    plan: dict = {"fail_primary": 1}

    def fake_get(url, headers=None, params=None, timeout=None, **kw):
        path = url.split("45001", 1)[-1]
        asked.append(path)
        if path == STOP:
            if plan["fail_primary"] > 0:
                plan["fail_primary"] -= 1
                raise ConnectionError("Connection aborted: remote end closed connection")
            return _Reply(200, {"status": {"http_code": 200}})
        return _routing_miss()

    monkeypatch.setattr(mod.requests, "get", fake_get)
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    cfg = MultiloginInstanceConfig(instance_id="A", automation_token="t",
                                   launcher_base_v2="https://launcher.mlx.yt:45001/api/v2")
    return {"launcher": MultiloginLauncher(cfg), "asked": asked, "plan": plan}


def test_one_dropped_request_is_asked_again_and_the_profile_closes(flaky):
    flaky["launcher"].stop_profile(PROFILE, folder_id=FOLDER)
    assert flaky["asked"] == [STOP, STOP], "старые адреса не нужны, если основной ответил"


def test_a_main_address_that_keeps_failing_is_named_in_the_error(flaky):
    flaky["plan"]["fail_primary"] = 99
    with pytest.raises(MultiloginLauncherError) as err:
        flaky["launcher"].stop_profile(PROFILE, folder_id=FOLDER)
    assert flaky["asked"][:3] == [STOP, STOP, STOP]
    text = str(err.value)
    assert "main address" in text and "Connection aborted" in text


def test_a_server_error_on_the_main_address_is_retried(flaky, monkeypatch):
    import src.meta_campaign_agent.multilogin_launcher as mod

    replies = [_Reply(502, None, "Bad Gateway"), _Reply(200, {"status": {"http_code": 200}})]
    monkeypatch.setattr(mod.requests, "get",
                        lambda url, **kw: (flaky["asked"].append(url.split("45001", 1)[-1]), replies.pop(0))[1])
    flaky["launcher"].stop_profile(PROFILE, folder_id=FOLDER)
    assert flaky["asked"] == [STOP, STOP]


def test_a_timeout_on_the_main_address_is_not_asked_again(flaky, monkeypatch):
    import src.meta_campaign_agent.multilogin_launcher as mod

    def fake_get(url, **kw):
        path = url.split("45001", 1)[-1]
        flaky["asked"].append(path)
        if path == STOP:
            raise mod.requests.Timeout("Read timed out")
        return _routing_miss()

    monkeypatch.setattr(mod.requests, "get", fake_get)
    with pytest.raises(MultiloginLauncherError) as err:
        flaky["launcher"].stop_profile(PROFILE, folder_id=FOLDER)
    assert flaky["asked"].count(STOP) == 1
    assert "Read timed out" in str(err.value)
