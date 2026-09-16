# -*- coding: utf-8 -*-
"""Имя и папка открытого профиля приходят от самого Multilogin (acl-1198).

Чтобы подключить аккаунт, байер открывал профиль в Multilogin Desktop — иначе
дашборду неоткуда было взять имя: оно читалось из ЗАГОЛОВКА ОКНА браузера, а id
восстанавливался по укороченному имени папки Windows.

16.09 на живой машине выяснилось, что и это не работает: `_name_from_title`
отбрасывает любое имя с пробелом, а здешние профили называются
`FacebookDQ501 - Duan Guyen`. То есть в списке были голые UUID, и подсказка
`suggested_profile` не срабатывала никогда — ровно то, ради чего имя и читали.

Локальный лаунчер отвечает на всё это сразу:

    GET https://launcher.mlx.yt:45001/api/v1/profile/statuses
    2a5d0c87-… | 'FacebookDQ501 - Duan Guyen' | folder=b8301b4d-… | browser_running

Проверяется здесь: имя с пробелами доходит целиком, folder_id приезжает вместе
с ним, пустой слот одного инстанса не мешает спросить через другой, а любой
отказ лаунчера тихо возвращает нас к старому чтению машины — список профилей
это удобство, и упасть он права не имеет.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import mlx_profiles  # noqa: E402


DQ501 = "2a5d0c87-04e6-43fb-bdd1-f9bcb1a3be6b"
DOC19 = "cabcf42f-3699-4d78-9886-231fd44b3498"
FOLDER = "b8301b4d-0558-48d6-9861-8cf12c49ef7c"


def _state(profile_id: str, name: str, status: str = "browser_running") -> dict:
    return {
        "profile_id": profile_id,
        "name": name,
        "folder_id": FOLDER,
        "status": status,
        "is_quick": False,
    }


class _Reply:
    def __init__(self, payload, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


@pytest.fixture
def launcher(monkeypatch):
    """Отвечает вместо лаунчера и записывает, о чём его спросили."""
    calls: list[dict] = []
    reply: dict = {"value": _Reply({"data": {"states": {}}})}

    def fake_get(url, headers=None, timeout=None, **kw):
        calls.append({"url": url, "headers": headers or {}, "timeout": timeout})
        value = reply["value"]
        if isinstance(value, Exception):
            raise value
        return value

    import requests

    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setenv("MULTILOGIN_AUTOMATION_TOKEN_A", "")
    monkeypatch.setenv("MULTILOGIN_AUTOMATION_TOKEN_B", "token-b")
    monkeypatch.delenv("MULTILOGIN_LAUNCHER_BASE_B", raising=False)
    return {"calls": calls, "reply": reply}


@pytest.fixture
def no_machine_scan(monkeypatch):
    """Старый путь чтения машины отвечает пусто — чтобы не зависеть от неё."""
    monkeypatch.setattr(mlx_profiles, "all_profile_ids", lambda: [])
    monkeypatch.setattr(mlx_profiles, "open_profile_ids", lambda: [])
    monkeypatch.setattr(mlx_profiles, "_window_titles_by_pid", lambda: {})


# ── то, что было сломано ──────────────────────────────────────────────────

def test_a_name_with_spaces_arrives_whole(launcher, no_machine_scan):
    """Ровно случай DQ501: пробелы в имени больше ничего не отбрасывают."""
    launcher["reply"]["value"] = _Reply(
        {"data": {"states": {DQ501: _state(DQ501, "FacebookDQ501 - Duan Guyen")}}}
    )
    got = mlx_profiles.open_profiles_detailed()
    assert [p["name"] for p in got] == ["FacebookDQ501 - Duan Guyen"]


def test_such_a_name_now_has_a_key(launcher, no_machine_scan):
    """Без ключа подсказка профиля не срабатывала вовсе."""
    launcher["reply"]["value"] = _Reply(
        {"data": {"states": {DQ501: _state(DQ501, "FacebookDQ501 - Duan Guyen")}}}
    )
    (one,) = mlx_profiles.open_profiles_detailed()
    assert mlx_profiles.profile_name_key(one["name"]) == "Q501"


def test_the_profile_and_the_account_agree_on_that_key():
    """Обе стороны сравнения должны давать один ключ, иначе подсказки нет."""
    assert mlx_profiles.profile_name_key("FacebookDQ501 - Duan Guyen") == (
        mlx_profiles.profile_name_key("FacebookDQ501 - Duan Guyen".split(" ")[0])
    )


def test_a_name_without_spaces_keys_exactly_as_before():
    """Прежние имена Multilogin трогать нельзя — на них живут другие байеры."""
    assert mlx_profiles.profile_name_key("KLD_BFL_W27") == "W27"
    assert mlx_profiles.profile_name_key("FacebookMKTOBFLW27") == "W27"
    assert mlx_profiles.profile_name_key("") == ""
    assert mlx_profiles.profile_name_key("Duan Guyen") == ""


# ── что именно приходит ───────────────────────────────────────────────────

def test_the_folder_comes_with_the_profile(launcher, no_machine_scan):
    """folder_id больше не приходится выводить из пути профиля на диске."""
    launcher["reply"]["value"] = _Reply(
        {"data": {"states": {DQ501: _state(DQ501, "FacebookDQ501 - Duan Guyen")}}}
    )
    (one,) = mlx_profiles.open_profiles_detailed()
    assert one["folder_id"] == FOLDER


def test_every_entry_has_the_same_three_fields(launcher, no_machine_scan):
    launcher["reply"]["value"] = _Reply(
        {"data": {"states": {
            DQ501: _state(DQ501, "FacebookDQ501 - Duan Guyen"),
            DOC19: _state(DOC19, "FacebookDoc19 - Eros Andersson"),
        }}}
    )
    got = mlx_profiles.open_profiles_detailed()
    assert len(got) == 2
    assert all(set(p) == {"profile_id", "name", "folder_id"} for p in got)


def test_a_profile_shutting_down_is_not_offered(launcher, no_machine_scan):
    """Предложить его — значит предложить то, что через секунду исчезнет."""
    launcher["reply"]["value"] = _Reply(
        {"data": {"states": {
            DQ501: _state(DQ501, "FacebookDQ501 - Duan Guyen", "profile_stopping"),
            DOC19: _state(DOC19, "FacebookDoc19 - Eros Andersson"),
        }}}
    )
    got = mlx_profiles.open_profiles_detailed()
    assert [p["profile_id"] for p in got] == [DOC19]


def test_the_launcher_is_asked_on_its_v1_address(launcher, no_machine_scan):
    mlx_profiles.running_profiles_from_launcher()
    assert launcher["calls"], "лаунчер не был спрошен вовсе"
    assert launcher["calls"][0]["url"].endswith("/api/v1/profile/statuses")


def test_an_empty_slot_does_not_stop_the_other_instance(launcher, no_machine_scan):
    """Заполнен обычно один слот: второй — учётка KLD, она нужна не всем."""
    launcher["reply"]["value"] = _Reply(
        {"data": {"states": {DQ501: _state(DQ501, "FacebookDQ501 - Duan Guyen")}}}
    )
    got = mlx_profiles.running_profiles_from_launcher()
    assert [p["profile_id"] for p in got] == [DQ501]
    assert [c["headers"].get("Authorization") for c in launcher["calls"]] == [
        "Bearer token-b"
    ]


def test_the_question_is_not_allowed_to_hang(launcher, no_machine_scan):
    """Обзор аккаунтов спрашивает это на каждой загрузке страницы."""
    mlx_profiles.running_profiles_from_launcher()
    assert 0 < launcher["calls"][0]["timeout"] <= 10


# ── когда лаунчера нет ────────────────────────────────────────────────────

def test_a_dead_launcher_falls_back_instead_of_raising(launcher, no_machine_scan):
    import requests

    launcher["reply"]["value"] = requests.ConnectionError("Multilogin is not running")
    assert mlx_profiles.open_profiles_detailed() == []


def test_a_refusal_falls_back_too(launcher, no_machine_scan):
    launcher["reply"]["value"] = _Reply({"status": {"http_code": 401}}, status_code=401)
    assert mlx_profiles.running_profiles_from_launcher() == []


def test_an_unexpected_shape_falls_back_too(launcher, no_machine_scan):
    """Формат ответа не наш, и меняться он может без нас."""
    launcher["reply"]["value"] = _Reply({"data": {"states": ["not", "a", "map"]}})
    assert mlx_profiles.running_profiles_from_launcher() == []


def test_no_token_at_all_means_no_question(monkeypatch, no_machine_scan):
    """Без токенов спрашивать нечем — и падать тоже незачем."""
    calls: list[str] = []

    import requests

    monkeypatch.setattr(requests, "get", lambda url, **kw: calls.append(url))
    monkeypatch.setenv("MULTILOGIN_AUTOMATION_TOKEN_A", "")
    monkeypatch.setenv("MULTILOGIN_AUTOMATION_TOKEN_B", "")
    assert mlx_profiles.running_profiles_from_launcher() == []
    assert calls == []
