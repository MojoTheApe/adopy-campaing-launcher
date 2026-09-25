# -*- coding: utf-8 -*-
"""Team access: токен Multilogin выпускает тимлид, остальные вставляют готовый (ACL-1311).

Логин в Multilogin у команды общий — один на Inhouse, один на KLD. Вход по
паролю выпускает новый токен автоматизации; 24.09 оба токена на машине
владельца отвечали 401 при сроке ещё на две недели. Если каждый байер входит
сам, он выбивает остальных. Поэтому вход — только на машине тимлида
(`ACL_MLX_TEAM_ADMIN=1`), у байеров — вставка готового токена.

Проверяем настоящие маршруты через ASGI, с подменённым Multilogin.
"""
from __future__ import annotations

import asyncio
import base64
import json
import time

import httpx
import pytest

from src.web.app import app


class _Response:
    def __init__(self, status_code: int, payload=None) -> None:
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = json.dumps(self._payload)
        self.headers = {"content-type": "application/json"}

    def json(self):
        return self._payload


def _jwt(exp: float) -> str:
    def part(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")
    return f"{part({'alg': 'none'})}.{part({'exp': exp, 'email': 'team@example.test'})}.sig"


def _call(method: str, path: str, *, body=None, headers=None) -> httpx.Response:
    async def run():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            return await client.request(
                method, path,
                content=json.dumps(body) if body is not None else None,
                headers={"Content-Type": "application/json", **(headers or {})},
            )
    return asyncio.run(run())


@pytest.fixture
def multilogin(monkeypatch):
    """Multilogin, записывающий обращения. По умолчанию токен живой."""
    import requests

    calls: list[str] = []
    answer = {"folders": 200}

    def fake_get(url, **kwargs):
        calls.append(url)
        return _Response(answer["folders"], {"data": {"folders": []}})

    def fake_post(url, **kwargs):
        calls.append(url)
        return _Response(200, {"data": {"token": "bearer"}})

    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.delenv("ACL_MLX_TEAM_ADMIN", raising=False)
    return calls, answer


def _as_team_lead(monkeypatch, yes: bool = True):
    if yes:
        monkeypatch.setenv("ACL_MLX_TEAM_ADMIN", "1")
    else:
        monkeypatch.delenv("ACL_MLX_TEAM_ADMIN", raising=False)


# ── Кто тимлид ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("value, admin", [("1", True), ("true", True), ("", False), ("0", False)])
def test_the_status_says_whose_machine_this_is(multilogin, monkeypatch, value, admin):
    """Роль нужна странице даже без токена: от неё зависит, что предложить."""
    monkeypatch.setenv("ACL_MLX_TEAM_ADMIN", value)
    monkeypatch.delenv("MULTILOGIN_AUTOMATION_TOKEN_A", raising=False)
    answer = _call("GET", "/multilogin-token-status?instance=A").json()
    assert answer["team_admin"] is admin
    assert answer["has_token"] is False


# ── Заменён или истёк ─────────────────────────────────────────────────────

def test_a_token_refused_before_its_expiry_was_replaced(multilogin, monkeypatch):
    """Срок впереди, а Multilogin отказывает — значит, токен заменили новым
    входом или сменили пароль. Сказать «истёк» — значит отправить человека
    ждать то, чего не случится."""
    calls, answer = multilogin
    answer["folders"] = 401
    monkeypatch.setenv("MULTILOGIN_AUTOMATION_TOKEN_A", _jwt(time.time() + 14 * 86400))
    state = _call("GET", "/multilogin-token-status?instance=A").json()
    assert state["alive"] is False
    assert state["reason"] == "revoked"
    assert "team lead" in state["next_step"], "байеру не сказано, где взять новый токен"


def test_a_token_past_its_expiry_is_expired_without_asking_multilogin(multilogin, monkeypatch):
    calls, _ = multilogin
    monkeypatch.setenv("MULTILOGIN_AUTOMATION_TOKEN_A", _jwt(time.time() - 3600))
    state = _call("GET", "/multilogin-token-status?instance=A").json()
    assert state["reason"] == "expired"
    assert calls == [], "за ответом о просроченном токене сходили в Multilogin"


def test_the_team_lead_is_told_to_sign_in_again_and_a_buyer_to_ask(multilogin, monkeypatch):
    _, answer = multilogin
    answer["folders"] = 401
    monkeypatch.setenv("MULTILOGIN_AUTOMATION_TOKEN_A", _jwt(time.time() + 86400))
    _as_team_lead(monkeypatch, False)
    buyer = _call("GET", "/multilogin-token-status?instance=A").json()["next_step"]
    _as_team_lead(monkeypatch, True)
    lead = _call("GET", "/multilogin-token-status?instance=A").json()["next_step"]
    assert "team lead" in buyer and "Sign in" not in buyer
    assert "Sign in again" in lead


# ── Копирование токена ────────────────────────────────────────────────────

def test_the_team_lead_can_read_the_current_token(multilogin, monkeypatch):
    _as_team_lead(monkeypatch)
    monkeypatch.setenv("MULTILOGIN_AUTOMATION_TOKEN_B", "team-token-kld")
    r = _call("POST", "/multilogin-team-token", body={"instance": "B"})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "instance": "B", "token": "team-token-kld"}
    assert r.headers.get("cache-control") == "no-store", "токен может осесть в кэше"


def test_a_buyers_dashboard_never_hands_out_the_token(multilogin, monkeypatch):
    _as_team_lead(monkeypatch, False)
    monkeypatch.setenv("MULTILOGIN_AUTOMATION_TOKEN_A", "team-token-inhouse")
    r = _call("POST", "/multilogin-team-token", body={"instance": "A"})
    assert r.status_code == 403
    assert "team-token-inhouse" not in r.text


def test_the_token_is_not_readable_by_a_plain_link(multilogin, monkeypatch):
    """GET не проходит проверку происхождения — токен отдаёт только POST."""
    _as_team_lead(monkeypatch)
    monkeypatch.setenv("MULTILOGIN_AUTOMATION_TOKEN_A", "team-token-inhouse")
    r = _call("GET", "/multilogin-team-token?instance=A")
    assert r.status_code == 405
    assert "team-token-inhouse" not in r.text


def test_another_site_cannot_read_the_token(multilogin, monkeypatch):
    _as_team_lead(monkeypatch)
    monkeypatch.setenv("MULTILOGIN_AUTOMATION_TOKEN_A", "team-token-inhouse")
    r = _call("POST", "/multilogin-team-token", body={"instance": "A"},
              headers={"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403
    assert "team-token-inhouse" not in r.text


# ── Вход по паролю ────────────────────────────────────────────────────────

def test_a_buyer_cannot_issue_a_token_even_from_an_old_page(multilogin, monkeypatch):
    """Старая открытая страница или забытая панель в билдере тоже шлют этот
    запрос. Отказ должен случиться раньше, чем Multilogin услышит пароль."""
    calls, _ = multilogin
    _as_team_lead(monkeypatch, False)
    r = _call("POST", "/multilogin-signin",
              body={"email": "team@example.test", "password": "pw", "save_token": True})
    assert r.status_code == 403
    assert "team token" in r.json()["error"]
    assert calls == [], "пароль ушёл в Multilogin с машины байера"


def test_the_team_lead_still_signs_in(multilogin, monkeypatch):
    calls, _ = multilogin
    _as_team_lead(monkeypatch)
    monkeypatch.setattr("src.web.app.update_machine_env", lambda values: "C:/fake/.env")
    _call("POST", "/multilogin-signin",
          body={"email": "team@example.test", "password": "pw", "save_token": True})
    assert any(url.endswith("/user/signin") for url in calls)


def test_a_dead_token_during_search_tells_a_buyer_whom_to_ask(multilogin, monkeypatch):
    import requests

    _as_team_lead(monkeypatch, False)
    monkeypatch.setenv("MULTILOGIN_AUTOMATION_TOKEN_A", "dead")
    monkeypatch.setattr(requests, "post", lambda url, **kw: _Response(401, {"status": {"message": "unauthorized"}}))
    error = _call("POST", "/multilogin-search", body={"search_text": "", "instance": "A"}).json()["error"]
    assert "team lead" in error and "Connect" not in error
