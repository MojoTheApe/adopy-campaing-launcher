# -*- coding: utf-8 -*-
"""ACL-1441: «Connect Multilogin» выдаёт токен в выбранном рабочем пространстве.

Вживую 2026-10-01: логин ndays6443 по умолчанию попадает в своё пустое
Free-пространство, где автоматизации нет, и Multilogin отвечает
«403 Request is not allowed». Профили KLD живут в пространстве 1ejadmedia, где
у того же логина роль `user` и токен выдаётся — если явно переключиться.

Поля API сверены с документацией Multilogin X (Postman 28533318/2s946h9Cv9):
`GET /user/workspaces` → data.workspaces[] {workspace_id, name, role};
`POST /user/refresh_token` {email, refresh_token, workspace_id} → data.token,
data.refresh_token; `GET /workspace/restrictions` → data.automation_available,
data.plan_name. Настоящий Multilogin здесь не вызывается — только заглушка.
"""
from __future__ import annotations

import asyncio
import hashlib
import json

import httpx
import pytest

from src.web import multilogin_auth
from src.web.multilogin_auth import PLAN_HAS_NO_AUTOMATION, ROLE_CANNOT_ISSUE


class Response:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text or json.dumps(self._payload)
        self.headers = {"content-type": "application/json"}

    def json(self):
        return self._payload


OWN_FREE = {"workspace_id": "ws-own", "name": "ndays6443@gmail.com", "role": "owner"}
KLD = {"workspace_id": "ws-kld", "name": "1ejadmedia", "role": "user"}
INHOUSE = {"workspace_id": "ws-inh", "name": "admin@lotostech", "role": "manager"}


class Mlx:
    """Multilogin X, каким его описывает документация, с записью всех вызовов.

    Сессионный токен говорит, в каком пространстве находится запрос:
    `bearer-default` — пространство по умолчанию (первое в списке),
    `bearer-<id>` — после переключения через /user/refresh_token.
    """

    def __init__(self, workspaces, *, automation=None, can_issue=None,
                 refresh_fails=False, list_status=200):
        self.workspaces = workspaces
        self.automation = automation or {}
        self.can_issue = can_issue or {}
        self.refresh_fails = refresh_fails
        self.list_status = list_status
        self.calls: list[tuple] = []
        self._refresh = "r0"
        self._n = 0

    def _ws(self, headers):
        bearer = (headers or {}).get("Authorization", "").removeprefix("Bearer ")
        if bearer == "bearer-default":
            return self.workspaces[0]["workspace_id"]
        return bearer.removeprefix("bearer-")

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append(("POST", url, headers, json))
        if url.endswith("/user/signin"):
            return Response(200, {"data": {"token": "bearer-default", "refresh_token": "r0"}})
        if url.endswith("/user/refresh_token"):
            if self.refresh_fails:
                return Response(400, {"status": {"message": "invalid refresh token"}})
            assert json["refresh_token"] == self._refresh, "старый refresh_token после ротации"
            self._n += 1
            self._refresh = f"r{self._n}"
            return Response(200, {"data": {"token": f"bearer-{json['workspace_id']}",
                                           "refresh_token": self._refresh}})
        return Response(404)

    def get(self, url, headers=None, timeout=None):
        self.calls.append(("GET", url, headers, None))
        ws = self._ws(headers)
        if url.endswith("/user/workspaces"):
            if self.list_status != 200:
                return Response(self.list_status, {"status": {"message": "nope"}})
            return Response(200, {"data": {"total_count": len(self.workspaces),
                                           "workspaces": self.workspaces}})
        if url.endswith("/workspace/restrictions"):
            return Response(200, {"data": {"automation_available": self.automation.get(ws, True),
                                           "plan_name": "Free" if not self.automation.get(ws, True) else "Team"}})
        if "/workspace/automation_token" in url:
            if not self.automation.get(ws, True):
                return Response(403, {"status": {"message": "Request is not allowed"}})
            if not self.can_issue.get(ws, True):
                return Response(403, {"status": {"message": "Request is not allowed"}})
            return Response(200, {"data": {"token": f"auto-{ws}"}})
        return Response(404)

    def urls(self, part):
        return [c for c in self.calls if part in c[1]]


def test_default_free_workspace_plus_team_workspace_issues_the_token_in_the_team_one():
    mlx = Mlx([OWN_FREE, KLD], automation={"ws-own": False, "ws-kld": True})
    out = multilogin_auth.sign_in("ndays6443@gmail.com", "pw", requests_module=mlx)

    assert out.ok and not out.needs_choice
    assert out.automation_token == "auto-ws-kld"
    assert out.workspace_name == "1ejadmedia" and out.workspace_id == "ws-kld"
    assert out.token_error == ""
    # Токен выдан ровно один раз и с сессией KLD, а не пространства по умолчанию.
    issued = mlx.urls("automation_token")
    assert len(issued) == 1 and issued[0][2]["Authorization"] == "Bearer bearer-ws-kld"
    assert out.bearer == "bearer-ws-kld", "профили читаются из того же пространства"


def test_a_single_workspace_needs_no_extra_step():
    mlx = Mlx([KLD])
    out = multilogin_auth.sign_in("buyer@adopy.com", "pw", requests_module=mlx)

    assert out.ok and not out.needs_choice
    assert out.automation_token == "auto-ws-kld"
    assert out.workspace_name == "1ejadmedia"
    assert not mlx.urls("/workspace/restrictions"), "одно пространство — нечего прощупывать"


def test_two_workspaces_with_automation_ask_the_person_and_issue_nothing():
    mlx = Mlx([OWN_FREE, KLD, INHOUSE], automation={"ws-own": False})
    out = multilogin_auth.sign_in("x@adopy.com", "pw", requests_module=mlx)

    assert out.ok and out.needs_choice
    assert out.automation_token == ""
    assert not mlx.urls("automation_token"), "до выбора токен не выпускается"
    listed = {w["workspace_id"]: w for w in out.workspaces}
    assert listed["ws-kld"]["name"] == "1ejadmedia" and listed["ws-kld"]["role"] == "user"
    assert listed["ws-own"]["automation"] is False
    assert listed["ws-inh"]["automation"] is True

    again = multilogin_auth.sign_in("x@adopy.com", "pw", workspace_id="ws-inh",
                                    requests_module=Mlx([OWN_FREE, KLD, INHOUSE]))
    assert again.ok and not again.needs_choice
    assert again.automation_token == "auto-ws-inh" and again.workspace_name == "admin@lotostech"


def test_the_slots_remembered_workspace_is_used_without_asking():
    mlx = Mlx([OWN_FREE, KLD, INHOUSE])
    out = multilogin_auth.sign_in("x@adopy.com", "pw", remembered_workspace="1ejadmedia",
                                  requests_module=mlx)
    assert not out.needs_choice and out.automation_token == "auto-ws-kld"
    assert not mlx.urls("/workspace/restrictions")


def test_a_role_without_rights_is_named():
    mlx = Mlx([KLD], can_issue={"ws-kld": False})
    out = multilogin_auth.sign_in("member@adopy.com", "pw", requests_module=mlx)

    assert out.ok and out.automation_token == ""
    assert out.token_error.startswith(ROLE_CANNOT_ISSUE)
    assert "1ejadmedia" in out.token_error


def test_a_plan_without_automation_is_named():
    mlx = Mlx([OWN_FREE], automation={"ws-own": False})
    out = multilogin_auth.sign_in("ndays6443@gmail.com", "pw", requests_module=mlx)

    assert out.automation_token == ""
    assert out.token_error.startswith(PLAN_HAS_NO_AUTOMATION)


def test_choosing_a_workspace_without_automation_is_refused_without_minting():
    mlx = Mlx([OWN_FREE, KLD], automation={"ws-own": False})
    out = multilogin_auth.sign_in("ndays6443@gmail.com", "pw", workspace_id="ws-own",
                                  requests_module=mlx)
    assert out.token_error.startswith(PLAN_HAS_NO_AUTOMATION)
    assert out.workspaces, "список остаётся, чтобы выбрать другое пространство"


def test_a_failed_workspace_switch_is_named():
    mlx = Mlx([KLD], refresh_fails=True)
    out = multilogin_auth.sign_in("buyer@adopy.com", "pw", requests_module=mlx)

    assert out.ok and out.automation_token == ""
    assert "Could not switch to the workspace '1ejadmedia'" in out.token_error
    assert "invalid refresh token" in out.token_error
    assert not mlx.urls("automation_token")


def test_no_workspace_list_falls_back_to_the_default_workspace():
    mlx = Mlx([KLD], list_status=403)
    out = multilogin_auth.sign_in("buyer@adopy.com", "pw", requests_module=mlx)
    assert out.automation_token == "auto-ws-kld" and out.workspace_name == ""


def test_every_call_carries_a_normal_user_agent():
    """Cloudflare перед api.multilogin.com отвечает 1010 на python-requests."""
    mlx = Mlx([OWN_FREE, KLD], automation={"ws-own": False})
    multilogin_auth.sign_in("x@adopy.com", "pw", requests_module=mlx)
    for method, url, headers, _ in mlx.calls:
        ua = (headers or {}).get("User-Agent", "")
        assert ua.startswith("Mozilla/5.0"), (method, url)


def test_switch_sends_email_refresh_token_and_workspace_id():
    mlx = Mlx([KLD])
    multilogin_auth.sign_in("buyer@adopy.com", "pw", requests_module=mlx)
    (switch,) = mlx.urls("/user/refresh_token")
    assert switch[3] == {"email": "buyer@adopy.com", "refresh_token": "r0", "workspace_id": "ws-kld"}


# ── Дешборд: эндпоинт не хранит пароль и помнит пространство слота ─────────

def _endpoint(monkeypatch, mlx, body, saved):
    import requests

    from src.web.app import app

    monkeypatch.setattr(requests, "post", mlx.post)
    monkeypatch.setattr(requests, "get", mlx.get)
    monkeypatch.setattr("src.web.app.update_machine_env",
                        lambda values: saved.append(dict(values)) or "C:/fake/.env")
    monkeypatch.setattr("src.web.app._mlx_search_all", lambda *a, **k: ([], ""))
    for key in ("MULTILOGIN_WORKSPACE_B", "MULTILOGIN_WORKSPACE_ID_B"):
        monkeypatch.delenv(key, raising=False)

    async def _run():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            r = await client.post("/multilogin-signin", content=json.dumps(body),
                                  headers={"Content-Type": "application/json"})
            return r.json()

    return asyncio.run(_run())


def test_the_endpoint_asks_to_choose_and_saves_nothing(monkeypatch):
    saved: list = []
    mlx = Mlx([OWN_FREE, KLD, INHOUSE], automation={"ws-own": False})
    body = _endpoint(monkeypatch, mlx, {"email": "x@adopy.com", "password": "s3cret-pw",
                                        "instance": "B"}, saved)
    assert body["ok"] is True and body["choose_workspace"] is True
    assert body["token_saved"] is False
    assert [w["name"] for w in body["workspaces"]] == ["ndays6443@gmail.com", "1ejadmedia", "admin@lotostech"]
    assert not saved


def test_the_endpoint_saves_token_and_workspace_never_the_password(monkeypatch):
    saved: list = []
    mlx = Mlx([OWN_FREE, KLD], automation={"ws-own": False})
    body = _endpoint(monkeypatch, mlx, {"email": "ndays6443@gmail.com", "password": "s3cret-pw",
                                        "instance": "B"}, saved)
    assert body["ok"] and body["token_saved"] is True and body["workspace"] == "1ejadmedia"
    assert saved == [{"MULTILOGIN_AUTOMATION_TOKEN_B": "auto-ws-kld",
                      "MULTILOGIN_WORKSPACE_B": "1ejadmedia",
                      "MULTILOGIN_WORKSPACE_ID_B": "ws-kld"}]
    md5 = hashlib.md5(b"s3cret-pw").hexdigest()
    dumped = json.dumps(saved) + json.dumps(body)
    assert "s3cret-pw" not in dumped and md5 not in dumped


def test_the_signin_result_holds_no_password():
    mlx = Mlx([KLD])
    out = multilogin_auth.sign_in("buyer@adopy.com", "s3cret-pw", requests_module=mlx)
    md5 = hashlib.md5(b"s3cret-pw").hexdigest()
    assert "s3cret-pw" not in repr(out) and md5 not in repr(out)
    # refresh_token живёт только внутри одного вызова — наружу не отдаётся.
    assert not any("refresh" in name for name in out.__dataclass_fields__)
