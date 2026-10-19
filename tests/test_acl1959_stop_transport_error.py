# -*- coding: utf-8 -*-
"""Сбой связи на адресе остановки не превращается в ложный 404 (ACL-1959).

TEST9095, 16.10 18:10: закрытие после чтения 9403A писало
`MLX stop failed (Stop profile failed HTTP 404: 404 page not found)`, окно
оставалось открытым. Настоящий адрес `v1/profile/stop/p/{id}` лаунчер 12.15.0
обслуживает (без токена — 401, не 404); первый вызов упал исключением через
35-60 с, а `stop_profile` ушёл в хвост старых адресов, и их 404 затёр
настоящую ошибку.

Проверяется: один повтор настоящего адреса; «already stopped» на повторе —
готово; два сбоя — ошибка с адресом и типом исключения, хвост не трогаем;
404 на настоящем адресе по-прежнему ведёт в хвост.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import requests

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


def _dropped():
    return requests.ConnectionError("Connection aborted: RemoteDisconnected")


def _timed_out():
    return requests.ReadTimeout("Read timed out. (read timeout=120)")


@pytest.fixture
def launcher(monkeypatch):
    """Лаунчер с подменённой сетью: по каждому адресу — очередь ответов."""
    import src.meta_campaign_agent.multilogin_launcher as mod

    asked: list[str] = []
    script: dict[str, list] = {}

    def fake_get(url, headers=None, params=None, timeout=None, **kw):
        path = url.split("45001", 1)[-1]
        asked.append(path)
        queue = script.get(path) or [_Reply(404, None, "404 page not found")]
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(mod.requests, "get", fake_get)
    pauses: list[float] = []
    monkeypatch.setattr(mod.time, "sleep", pauses.append)
    cfg = MultiloginInstanceConfig(instance_id="A", automation_token="t",
                                   launcher_base_v2="https://launcher.mlx.yt:45001/api/v2")
    return {"launcher": MultiloginLauncher(cfg), "asked": asked, "script": script, "pauses": pauses}


@pytest.mark.parametrize("failure", [_dropped, _timed_out], ids=["connection", "timeout"])
def test_a_dropped_stop_is_asked_once_more_and_succeeds(launcher, failure):
    launcher["script"][STOP] = [failure(), _Reply(200, {"status": {"http_code": 200}})]
    launcher["launcher"].stop_profile(PROFILE, folder_id=FOLDER)
    assert launcher["asked"] == [STOP, STOP]
    assert len(launcher["pauses"]) == 1 and launcher["pauses"][0] > 0, "повтор — после паузы"


def test_already_stopped_on_the_retry_is_done(launcher):
    """Первый вызов мог успеть остановить профиль, хотя ответ не дошёл."""
    launcher["script"][STOP] = [_dropped(), _Reply(500, {"status": {"message": "profile already stopped"}})]
    launcher["launcher"].stop_profile(PROFILE, folder_id=FOLDER)
    assert launcher["asked"] == [STOP, STOP]


def test_two_dropped_stops_report_the_real_error_not_a_404(launcher):
    launcher["script"][STOP] = [_dropped(), _dropped()]
    with pytest.raises(MultiloginLauncherError) as err:
        launcher["launcher"].stop_profile(PROFILE, folder_id=FOLDER)
    text = str(err.value)
    assert "404" not in text
    assert "ConnectionError" in text and "RemoteDisconnected" in text
    assert f"v1/profile/stop/p/{PROFILE}" in text
    assert launcher["asked"] == [STOP, STOP], "старые адреса после сбоя связи не спрашиваем"


def test_a_routing_miss_on_the_real_address_still_walks_the_tail(launcher):
    with pytest.raises(MultiloginLauncherError) as err:
        launcher["launcher"].stop_profile(PROFILE, folder_id=FOLDER)
    assert launcher["asked"][0] == STOP
    assert STOP not in launcher["asked"][1:], "404 — не сбой связи, повтора нет"
    assert len(launcher["asked"]) == 4
    assert "404" in str(err.value)
