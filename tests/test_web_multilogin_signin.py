"""Signing in to Multilogin is not the same as being allowed to automate it.

Only the workspace owner may mint an automation token. A team member's correct
password signs in fine and then gets nothing — and without that token the RPA
cannot start a single profile.

The panel used to report both outcomes as a green "connected", differing only by
a trailing clause. So the operator saw success, pressed Publish, and got
"MULTILOGIN_AUTOMATION_TOKEN_A is not set in .env" — a message pointing at a
file they had just been told was written. Found on a second campaign manager's
first supervised run, 2026-09-03.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from src.web.app import app

_ROOT = Path(__file__).resolve().parents[1]


class _Response:
    def __init__(self, status_code: int, payload=None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text
        self.headers = {"content-type": "application/json"}

    def json(self):
        return self._payload


def _signin(monkeypatch, *, token_response: _Response, saved: list) -> dict:
    """Drive /multilogin-signin with the Multilogin API stubbed out."""
    import requests

    def fake_post(url, **kwargs):
        if url.endswith("/user/signin"):
            return _Response(200, {"data": {"token": "bearer-abc"}})
        return _Response(200, {"data": {"profiles": []}})

    def fake_get(url, **kwargs):
        if "automation_token" in url:
            return token_response
        return _Response(200, {"data": {}})

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(
        "src.web.app.update_machine_env",
        lambda values: saved.append(values) or "C:/fake/.env",
    )

    async def _run():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            response = await client.post(
                "/multilogin-signin",
                content=json.dumps({"email": "a@b.co", "password": "x", "save_token": True}),
                headers={"Content-Type": "application/json"},
            )
            return response.json()

    return asyncio.run(_run())


def test_signin_without_an_automation_token_does_not_report_a_saved_token(monkeypatch):
    saved: list = []
    body = _signin(
        monkeypatch,
        token_response=_Response(403, {"status": {"message": "only workspace owner"}}),
        saved=saved,
    )

    assert body["token_saved"] is False
    assert not saved, "nothing may be written when there is no token to write"
    assert "403" in body["token_error"]
    assert "only workspace owner" in body["token_error"]


def test_signin_with_a_token_saves_it_and_reports_no_problem(monkeypatch):
    saved: list = []
    body = _signin(
        monkeypatch,
        token_response=_Response(200, {"data": {"token": "automation-xyz"}}),
        saved=saved,
    )

    assert body["token_saved"] is True
    assert body["token_error"] == ""
    assert saved == [{"MULTILOGIN_AUTOMATION_TOKEN_A": "automation-xyz"}]


def test_the_two_multilogin_accounts_are_named_not_lettered():
    """"Instance A" names a slot in a config file, not anything anyone can recognise.

    Picking the wrong one is not a typo, it is a day: on 2026-09-03 the token for
    the Inhouse account was renewed with the KLD account's password, and every
    error along the way named something else — no token, no rights, expired plan.
    """
    from unittest.mock import MagicMock

    from src.web.app import _enrich_country_overview, mlx_instance_labels, templates
    from tests.test_web_drill import _MOCK_REPORT

    assert mlx_instance_labels() == {"A": "Inhouse", "B": "KLD"}

    report = {**_MOCK_REPORT, "countries": [dict(c) for c in _MOCK_REPORT["countries"]]}
    _enrich_country_overview(report)
    # Фикстура приведена к тому, что передаёт приложение: карта аккаунтов и
    # `mlx` в контексте.
    #
    # Выбор между двумя учётками Multilogin живёт теперь в форме подключения
    # (ACL-1130); раньше он был ещё и в панели билдера, и её убрали. Заодно
    # вскрылось, что фикстура давала `mlx_labels`, а `{% set mlx = mlx_labels %}`
    # — локальная переменная шаблона, и включаемые файлы её не видят. Панель
    # билдера жила в самом report.html и потому видела, а карта — нет. То есть
    # проверка смотрела не туда, куда смотрит человек.
    one = {"country": "Mexico", "key": "MX", "rows": [], "virtual": False,
           "live_read": 0, "live_sums": [], "book": {}}
    context = dict(
        request=MagicMock(), report=report, warnings=[], built_at="x", upload_errors={},
        account_preflight={}, account_preflight_json={}, ref_options={},
        ref_options_json={}, account_alias_map_json={}, draft_status={},
        amap={"countries": [one], "available": [], "all_accounts": [], "live": {},
              "dates": [], "date_from": "", "date_to": "", "span": ""},
        mlx=mlx_instance_labels(),
    )
    template = templates.env.get_template("report.html")

    rendered = template.render(**context, mlx_labels=mlx_instance_labels())
    assert "Instance A" not in rendered

    # Сам выбор — там, где человек его делает: в форме подключения аккаунта.
    # Раньше он был ещё и в панели билдера внутри report.html, и проверка
    # смотрела на страницу целиком. Панель убрана (ACL-1130), а карта на этой
    # фикстуре не разворачивается — поэтому спрашиваем тот кусок напрямую, а
    # не подгоняем фикстуру под целую страницу.
    picker = templates.env.get_template("_account_map_country.html").render(
        mc=one, amap=context["amap"], mlx=mlx_instance_labels())
    assert ">Inhouse<" in picker and ">KLD<" in picker
    assert "Instance A" not in picker
    # The slot ids stay A and B: they are written in every machine's
    # ad_account_map.csv and in the environment keys. Renaming those would mean
    # a migration on every install, for a label.
    assert 'value="A"' in picker and 'value="B"' in picker

    # Templates reload per request, Python only on restart. Between updating the
    # files and restarting the server the report must still render.
    stale = templates.env.get_template("_account_map_country.html").render(
        mc=one, amap=context["amap"])
    assert ">Inhouse<" in stale and ">KLD<" in stale, (
        "без `mlx` в контексте селектор остаётся без подписей"
    )


def test_the_panel_does_not_show_success_when_no_token_was_saved():
    """A green tick differing only by a trailing clause is not a warning."""
    template = (_ROOT / "src" / "web" / "templates" / "report.html").read_text(encoding="utf-8")

    start = template.index("function mlSignin(cid)")
    body = template[start:start + 3000]
    branch = body[body.index("if (data.token_saved)"):]

    assert "var(--warn)" in branch
    assert "NO automation token" in branch
    # The operator has to be told where to get one, or the warning is a dead end.
    assert "Generate Token" in branch


def test_a_refused_sign_in_is_not_reported_as_success(monkeypatch):
    """Ревью 24.09: ветку отказа не проверял никто — убери её, и неверный
    пароль отвечает «ok», с пустым списком профилей. Человек решит, что
    вошёл, и пойдёт искать, почему ничего не открывается."""
    import requests

    def fake_post(url, **kwargs):
        if url.endswith("/user/signin"):
            return _Response(401, {"status": {"message": "wrong credentials"}})
        return _Response(200, {"data": {"profiles": []}})

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(requests, "get", lambda url, **kw: _Response(200, {"data": {}}))
    saved: list = []
    monkeypatch.setattr("src.web.app.update_machine_env",
                        lambda values: saved.append(values) or "C:/fake/.env")

    async def _run():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            r = await client.post(
                "/multilogin-signin",
                content=json.dumps({"email": "a@b.co", "password": "wrong", "save_token": True}),
                headers={"Content-Type": "application/json"},
            )
            return r.json()

    body = asyncio.run(_run())
    assert body["ok"] is False
    assert "wrong credentials" in body["error"]
    assert saved == [], "в настройку что-то записали после отказа входа"
