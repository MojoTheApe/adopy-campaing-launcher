# -*- coding: utf-8 -*-
"""Профили запускаются обычным входом, который продлевает себя сам (ACL-1335).

27.09 Multilogin перестал выдавать токены автоматизации (`/workspace/automation_token`
→ 403 всем, даже владельцу), а токен, выданный на другой машине, профили не
запускает: «can't lock profile». Новый байер не мог подключиться вовсе, а
сохранённые токены тимлида истекают около 03.10 — после чего робот не открыл
бы ни одного профиля нигде.

Проверяется на поддельном Multilogin (в сеть не ходит ничего):

* подключение: вход → список пространств с ролями → выбор → переключение →
  в машинный `.env` уходят почта, refresh-токен и пространство, но не пароль
  и не его хеш;
* поставщик токена слота: продление по часам (подменённым), под замком (одно
  продление на много потоков), поворот refresh-токена сохраняется;
* 401 на запуске — одно продление и один повтор, не больше;
* отвергнутое продление — понятная ошибка «press Reconnect» и тишина в сети;
* живой токен автоматизации работает как раньше, без входа и без продлений;
* секреты не уезжают в телеметрию и Sentry.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import threading
import time
from pathlib import Path

import httpx
import pytest

from src.meta_campaign_agent import mlx_credentials as cred
from src.meta_campaign_agent.multilogin_config import MultiloginInstanceConfig, get_multilogin_instance
from src.meta_campaign_agent.multilogin_launcher import MultiloginLauncher, MultiloginLauncherError

ROOT = Path(__file__).resolve().parents[1]
T0 = 1_900_000_000.0
LAUNCHER = "https://launcher.mlx.yt:45001/api/v2"
PROFILE = "p-1"
FOLDER = "f-1"
PASSWORD = "synthetic-pass-9"
PASSWORD_MD5 = "5d013a0819b39f7e41fedf7708527c0a"


def jwt(n: int, exp: float) -> str:
    """Поддельный JWT: читается как настоящий, ничего не подписывает."""
    def part(obj) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")
    return f"{part({'alg': 'none'})}.{part({'exp': exp, 'n': n})}.c2lnbmF0dXJl"


class Reply:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = json.dumps(self._payload)
        self.headers = {"content-type": "application/json"}
        self.content = self.text.encode()

    def json(self):
        return self._payload


class FakeMultilogin:
    """Multilogin на столе: вход, пространства, продление с поворотом, лаунчер."""

    def __init__(self, clock, *, workspaces=None, rotate=True, lifetime=1800):
        self.clock = clock
        self.lifetime = lifetime
        self.rotate = rotate
        self.workspaces = workspaces if workspaces is not None else [
            {"workspace_id": "ws-in", "name": "Inhouse", "role": "launcher"}]
        self.n = 0
        self.valid_bearers: set[str] = set()
        self.refresh_valid = "r-0"
        self.refresh_rejected = False
        self.calls: list[tuple[str, str, dict]] = []
        self.start_replies: list[Reply] = []
        self.refresh_pause = 0.0
        self.lock = threading.Lock()

    def _bearer(self) -> str:
        self.n += 1
        token = jwt(self.n, self.clock() + self.lifetime)
        self.valid_bearers.add(token)
        return token

    def count(self, path: str) -> int:
        return sum(1 for _m, url, _b in self.calls if path in url)

    def post(self, url, headers=None, json=None, timeout=None, **kw):
        with self.lock:
            self.calls.append(("POST", url, {"json": json, "headers": headers or {}}))
        if url.endswith("/user/signin"):
            if json.get("password") != PASSWORD_MD5:
                return Reply(401, {"status": {"message": "wrong credentials"}})
            return Reply(200, {"data": {"token": self._bearer(), "refresh_token": self.refresh_valid}})
        if url.endswith("/user/refresh_token"):
            if self.refresh_pause:
                time.sleep(self.refresh_pause)
            with self.lock:
                if self.refresh_rejected or json.get("refresh_token") != self.refresh_valid:
                    return Reply(401, {"status": {"message": "refresh token is invalid"}})
                data = {"token": self._bearer()}
                if self.rotate:
                    self.refresh_valid = f"r-{self.n}"
                    data["refresh_token"] = self.refresh_valid
                return Reply(200, {"data": data})
        if url.endswith("/profile/search"):
            if not self._authorised(headers):
                return Reply(401, {"status": {"message": "unauthorized"}})
            return Reply(200, {"data": {"profiles": [{"profile_id": PROFILE, "name": "Doc19",
                                                      "folder_id": FOLDER}], "total_count": 1}})
        return Reply(404, {})

    def _authorised(self, headers) -> bool:
        auth = (headers or {}).get("Authorization", "")
        return auth.startswith("Bearer ") and auth[7:] in self.valid_bearers

    def get(self, url, headers=None, timeout=None, params=None, **kw):
        with self.lock:
            self.calls.append(("GET", url, {"headers": headers or {}}))
        if url.endswith("/user/workspaces"):
            return Reply(200, {"data": self.workspaces})
        if "/start" in url:
            if self.start_replies:
                return self.start_replies.pop(0)
            if not self._authorised(headers):
                return Reply(401, {"status": {"message": "Unauthorized"}})
            return Reply(200, {"data": {"port": "40123"}})
        if url.endswith("/workspace/folders"):
            if not self._authorised(headers):
                return Reply(401, {})
            return Reply(200, {"data": {"folders": [{"folder_id": FOLDER, "name": "Main"}]}})
        if url.endswith("/profile/statuses"):
            return Reply(200, {"data": {"states": {PROFILE: {"profile_id": PROFILE, "name": "Doc19",
                                                             "folder_id": FOLDER, "status": "running"}}}})
        if "automation_token" in url:
            return Reply(403, {"status": {"message": "Request is not allowed"}})
        return Reply(404, {})


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(cred, "_now", c)
    return c


@pytest.fixture
def mlx(monkeypatch, clock):
    """Поддельный Multilogin на месте `requests` — во всех модулях сразу."""
    import requests

    fake = FakeMultilogin(clock)
    monkeypatch.setattr(requests, "post", fake.post)
    monkeypatch.setattr(requests, "get", fake.get)
    monkeypatch.setattr(cred, "_http", lambda: fake)
    for slot in ("A", "B"):
        monkeypatch.delenv(f"MULTILOGIN_AUTOMATION_TOKEN_{slot}", raising=False)
        monkeypatch.delenv(f"MULTILOGIN_AUTOMATION_TOKEN_{slot}_PREVIOUS", raising=False)
    return fake


def _save_session(monkeypatch, slot="A", refresh="r-0", workspace="ws-in", name="Inhouse", role="launcher"):
    monkeypatch.setenv(f"MULTILOGIN_EMAIL_{slot}", "buyer@example.test")
    monkeypatch.setenv(f"MULTILOGIN_REFRESH_TOKEN_{slot}", refresh)
    monkeypatch.setenv(f"MULTILOGIN_WORKSPACE_ID_{slot}", workspace)
    monkeypatch.setenv(f"MULTILOGIN_WORKSPACE_NAME_{slot}", name)
    monkeypatch.setenv(f"MULTILOGIN_WORKSPACE_ROLE_{slot}", role)


def _machine_env() -> str:
    from config.paths import get_machine_env_path

    path = Path(get_machine_env_path())
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _launcher(slot="A", token="") -> MultiloginLauncher:
    return MultiloginLauncher(MultiloginInstanceConfig(slot, token, LAUNCHER))


# ── поставщик токена ──────────────────────────────────────────────────────

def test_a_saved_sign_in_gives_a_bearer_and_keeps_it_until_near_expiry(mlx, clock, monkeypatch):
    _save_session(monkeypatch)
    first = cred.for_slot("A").bearer()
    assert first in mlx.valid_bearers
    assert mlx.count("/user/refresh_token") == 1

    # Запас — пять минут, числом, а не константой модуля: иначе тест
    # согласился бы с любым её значением, и с нулём тоже.
    clock.t += 1800 - 5 * 60 - 30                  # ещё не пора
    assert cred.for_slot("A").bearer() == first
    assert mlx.count("/user/refresh_token") == 1

    clock.t += 60                                  # до конца меньше пяти минут
    second = cred.for_slot("A").bearer()
    assert second != first and second in mlx.valid_bearers
    assert mlx.count("/user/refresh_token") == 2


def test_the_bearer_renews_itself_through_a_long_run(mlx, clock, monkeypatch):
    """Прогон дольше жизни токена: каждый запуск идёт с живым токеном."""
    _save_session(monkeypatch)
    launcher = _launcher()
    for _ in range(6):
        started = launcher.start_profile(FOLDER, PROFILE)
        assert started.port == "40123"
        clock.t += 1000                              # 6 × 1000 с > 1800 с жизни токена
    starts = [c for c in mlx.calls if "/start" in c[1]]
    assert all(c[2]["headers"]["Authorization"][7:] in mlx.valid_bearers for c in starts)
    assert mlx.count("/user/refresh_token") >= 3


def test_a_rotated_refresh_token_is_saved_to_the_machine_env(mlx, monkeypatch):
    _save_session(monkeypatch)
    cred.for_slot("A").bearer()
    assert mlx.refresh_valid != "r-0"
    assert os.environ["MULTILOGIN_REFRESH_TOKEN_A"] == mlx.refresh_valid
    assert f'MULTILOGIN_REFRESH_TOKEN_A="{mlx.refresh_valid}"' in _machine_env()


def test_a_rotated_refresh_token_survives_a_failed_save(mlx, monkeypatch):
    """Диск не записался — старый refresh-токен после поворота уже мёртв, и
    процесс обязан продолжать новым, а не упасть на следующем продлении."""
    def broken(values):
        raise OSError("disk is full")

    monkeypatch.setattr(cred, "_persist", broken)
    _save_session(monkeypatch)
    cred.for_slot("A").bearer()
    assert os.environ["MULTILOGIN_REFRESH_TOKEN_A"] == mlx.refresh_valid
    cred.for_slot("A").renew_after_401(cred.for_slot("A")._bearer)
    assert mlx.count("/user/refresh_token") == 2
    assert cred.for_slot("A")._dead_refresh == ""


def test_without_rotation_the_old_refresh_token_stays(mlx, monkeypatch):
    mlx.rotate = False
    _save_session(monkeypatch)
    cred.for_slot("A").bearer()
    assert os.environ["MULTILOGIN_REFRESH_TOKEN_A"] == "r-0"
    assert "MULTILOGIN_REFRESH_TOKEN_A" not in _machine_env()


def test_many_threads_renew_once(mlx, clock, monkeypatch):
    """Поворот refresh-токена делает двойное продление смертельным: второе
    пришло бы со старым токеном и получило отказ. Поэтому — замок слота."""
    _save_session(monkeypatch)
    mlx.refresh_pause = 0.05
    got: list[str] = []
    errors: list[BaseException] = []

    def work():
        try:
            got.append(cred.for_slot("A").bearer())
        except BaseException as exc:  # pragma: no cover - это и есть провал
            errors.append(exc)

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(set(got)) == 1
    assert mlx.count("/user/refresh_token") == 1


def test_each_slot_has_its_own_sign_in(mlx, monkeypatch):
    _save_session(monkeypatch, "A", workspace="ws-in", name="Inhouse")
    _save_session(monkeypatch, "B", refresh="r-0", workspace="ws-kld", name="KLD")
    cred.for_slot("A").bearer()
    mlx.refresh_valid = "r-0"   # у B свой вход; поддельный Multilogin прощает общий номер
    cred.for_slot("B").bearer()
    asked = [c[2]["json"]["workspace_id"] for c in mlx.calls if c[1].endswith("/user/refresh_token")]
    assert asked == ["ws-in", "ws-kld"]


# ── 401 на запуске: одно продление, один повтор ───────────────────────────

def test_a_401_on_start_renews_once_and_retries_once(mlx, clock, monkeypatch):
    _save_session(monkeypatch)
    launcher = _launcher()
    launcher.start_profile(FOLDER, PROFILE)
    # Multilogin погасил токен досрочно (не по часам).
    mlx.valid_bearers.clear()
    started = launcher.start_profile(FOLDER, PROFILE)
    assert started.port == "40123"
    assert mlx.count("/start") == 3        # удачный, отказ, повтор
    assert mlx.count("/user/refresh_token") == 2


def test_a_second_401_is_reported_not_looped(mlx, monkeypatch):
    _save_session(monkeypatch)
    mlx.start_replies = [Reply(401, {"status": {"message": "Unauthorized"}})] * 5
    with pytest.raises(MultiloginLauncherError, match="401"):
        _launcher().start_profile(FOLDER, PROFILE)
    assert mlx.count("/start") == 2
    assert mlx.count("/user/refresh_token") == 2   # первое получение + одно продление


def test_a_refused_renewal_says_reconnect_and_stops_asking(mlx, monkeypatch):
    _save_session(monkeypatch)
    mlx.refresh_rejected = True
    launcher = _launcher()
    with pytest.raises(MultiloginLauncherError, match="Multilogin sign-in expired — press Reconnect"):
        launcher.start_profile(FOLDER, PROFILE)
    with pytest.raises(MultiloginLauncherError, match="press Reconnect"):
        launcher.start_profile(FOLDER, PROFILE)
    assert mlx.count("/user/refresh_token") == 1, "отказ известен — в сеть больше не ходим"
    assert mlx.count("/start") == 0


def test_a_role_that_cannot_launch_is_named(mlx, monkeypatch):
    _save_session(monkeypatch, role="member")
    mlx.start_replies = [Reply(403, {"status": {"message": "Forbidden"}})]
    with pytest.raises(MultiloginLauncherError) as info:
        _launcher().start_profile(FOLDER, PROFILE)
    text = str(info.value)
    assert "role in Inhouse (member)" in text and "Launcher role" in text


# ── обратная совместимость: токен автоматизации ───────────────────────────

def test_an_alive_automation_token_works_exactly_as_before(mlx, monkeypatch):
    token = jwt(900, T0 + 10 * 86400)
    mlx.valid_bearers.add(token)
    monkeypatch.setenv("MULTILOGIN_AUTOMATION_TOKEN_A", token)
    launcher = MultiloginLauncher(get_multilogin_instance("A"))
    launcher.start_profile(FOLDER, PROFILE)
    (start,) = [c for c in mlx.calls if "/start" in c[1]]
    assert start[2]["headers"]["Authorization"] == f"Bearer {token}"
    assert mlx.count("/user/") == 0, "без входа — ни одного запроса к входу"


def test_an_expired_automation_token_without_sign_in_is_still_sent(mlx, monkeypatch):
    """Машина без входа ведёт себя ровно как до ACL-1335: решает Multilogin."""
    token = jwt(901, T0 - 60)
    monkeypatch.setenv("MULTILOGIN_AUTOMATION_TOKEN_A", token)
    with pytest.raises(MultiloginLauncherError, match="401"):
        MultiloginLauncher(get_multilogin_instance("A")).start_profile(FOLDER, PROFILE)
    assert mlx.count("/start") == 1
    assert mlx.count("/user/") == 0


def test_an_alive_automation_token_is_preferred_over_the_sign_in(mlx, monkeypatch):
    token = jwt(902, T0 + 86400)
    mlx.valid_bearers.add(token)
    monkeypatch.setenv("MULTILOGIN_AUTOMATION_TOKEN_A", token)
    _save_session(monkeypatch)
    assert cred.for_slot("A").bearer() == token
    assert cred.for_slot("A").method() == "automation"
    assert mlx.count("/user/refresh_token") == 0


def test_an_expired_automation_token_gives_way_to_the_sign_in(mlx, monkeypatch):
    monkeypatch.setenv("MULTILOGIN_AUTOMATION_TOKEN_A", jwt(903, T0 - 60))
    _save_session(monkeypatch)
    assert cred.for_slot("A").method() == "signin"
    assert cred.for_slot("A").bearer() in mlx.valid_bearers


def test_a_refused_automation_token_falls_back_to_the_sign_in(mlx, monkeypatch):
    """Токен «живой» по своему сроку, но Multilogin его не принимает."""
    monkeypatch.setenv("MULTILOGIN_AUTOMATION_TOKEN_A", jwt(904, T0 + 86400))  # не в valid_bearers
    _save_session(monkeypatch)
    started = MultiloginLauncher(get_multilogin_instance("A")).start_profile(FOLDER, PROFILE)
    assert started.port == "40123"
    starts = [c for c in mlx.calls if "/start" in c[1]]
    assert len(starts) == 2
    assert starts[1][2]["headers"]["Authorization"][7:] in mlx.valid_bearers


def test_the_slot_needs_a_token_or_a_sign_in(mlx, monkeypatch):
    with pytest.raises(ValueError, match="Connect Multilogin"):
        get_multilogin_instance("A")
    _save_session(monkeypatch)
    cfg = get_multilogin_instance("A")
    assert cfg.automation_token == ""


def test_open_profiles_are_read_with_the_sign_in(mlx, monkeypatch):
    from src.fb_ads_rpa import mlx_profiles

    _save_session(monkeypatch)
    got = mlx_profiles.running_profiles_from_launcher()
    assert [p["profile_id"] for p in got] == [PROFILE]
    (asked,) = [c for c in mlx.calls if c[1].endswith("/profile/statuses")]
    assert asked[2]["headers"]["Authorization"][7:] in mlx.valid_bearers


def test_the_api_provider_is_chosen_for_a_signed_in_machine(monkeypatch):
    from src.fb_ads_rpa import config as rpa_config

    monkeypatch.setenv("FB_MLX_ATTACH_ONLY", "0")
    monkeypatch.delenv("FB_BROWSER_PROVIDER", raising=False)
    monkeypatch.delenv("MULTILOGIN_AUTOMATION_TOKEN_A", raising=False)
    assert rpa_config.get_browser_provider() == "multilogin_direct"
    _save_session(monkeypatch)
    assert rpa_config.get_browser_provider() == "multilogin"


def test_the_doctor_accepts_a_signed_in_machine(monkeypatch):
    from scripts import acl_doctor as doctor

    monkeypatch.delenv("MULTILOGIN_AUTOMATION_TOKEN_A", raising=False)
    monkeypatch.setenv("FB_BROWSER_PROVIDER", "multilogin_attach")
    _save_session(monkeypatch)
    seen: list = []
    monkeypatch.setattr(doctor, "report", lambda *a, **k: seen.append(a))
    doctor.check_multilogin_token()
    assert seen and seen[0][0] == doctor.OK


# ── подключение в дешборде ────────────────────────────────────────────────

def _post(path: str, body: dict) -> dict:
    from src.web.app import app

    async def run():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            r = await client.post(path, content=json.dumps(body),
                                  headers={"Content-Type": "application/json"})
            return r.json()
    return asyncio.run(run())


def _get(path: str) -> dict:
    from src.web.app import app

    async def run():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            return (await client.get(path)).json()
    return asyncio.run(run())


def _connect(**extra) -> dict:
    return _post("/multilogin-signin", {"email": "buyer@example.test", "password": PASSWORD,
                                        "instance": "A", **extra})


def test_connect_with_one_workspace_saves_the_sign_in_not_the_password(mlx):
    body = _connect()
    assert body["ok"] is True and body["token_saved"] is True
    assert body["method"] == "signin" and body["workspace"] == "Inhouse"
    assert [p["profile_id"] for p in body["profiles"]] == [PROFILE]

    saved = _machine_env()
    assert 'MULTILOGIN_EMAIL_A="buyer@example.test"' in saved
    assert f'MULTILOGIN_REFRESH_TOKEN_A="{mlx.refresh_valid}"' in saved
    assert 'MULTILOGIN_WORKSPACE_ID_A="ws-in"' in saved
    assert 'MULTILOGIN_WORKSPACE_NAME_A="Inhouse"' in saved
    assert PASSWORD not in saved and PASSWORD_MD5 not in saved
    assert "MULTILOGIN_AUTOMATION_TOKEN_A_PREVIOUS" not in saved
    assert mlx.count("automation_token") == 0, "токен автоматизации больше не просим"
    # Переключение прошло тем же продлением, которым вход потом живёт.
    assert [c[2]["json"]["workspace_id"] for c in mlx.calls if c[1].endswith("/refresh_token")] == ["ws-in"]
    # Токен, полученный при подключении, уже в памяти — запуск без нового продления.
    _launcher().start_profile(FOLDER, PROFILE)
    assert mlx.count("/user/refresh_token") == 1


def test_several_workspaces_are_offered_with_roles_then_the_pick_is_saved(mlx):
    mlx.workspaces = [
        {"id": "ws-kld", "name": "KLD", "role": "Launcher"},
        {"workspace": {"workspace_id": "ws-in", "name": "Inhouse"}, "user_role": {"name": "owner"}},
        {"workspace_id": "ws-view", "workspace_name": "Audit", "role": "viewer"},
    ]
    first = _connect()
    assert first["ok"] is True and first["needs_workspace"] is True
    assert first["token_saved"] is False
    assert first["workspaces"] == [
        {"workspace_id": "ws-kld", "name": "KLD", "role": "launcher", "can_launch": True},
        {"workspace_id": "ws-in", "name": "Inhouse", "role": "owner", "can_launch": True},
        {"workspace_id": "ws-view", "name": "Audit", "role": "viewer", "can_launch": False},
    ]
    assert _machine_env().find("MULTILOGIN_REFRESH_TOKEN_A") == -1, "до выбора ничего не сохраняется"
    assert "refresh" not in json.dumps(first).lower(), "refresh-токен странице не отдаётся"

    # Роль без права запуска — отказ словами, и выбор можно повторить.
    refused = _post("/multilogin-signin-workspace",
                    {"pending": first["pending"], "workspace_id": "ws-view", "instance": "A"})
    assert refused["ok"] is False
    assert "Your role in Audit is viewer: it cannot start profiles" in refused["error"]
    assert "MULTILOGIN_WORKSPACE_ID_A" not in _machine_env()

    done = _post("/multilogin-signin-workspace",
                 {"pending": first["pending"], "workspace_id": "ws-in", "instance": "A"})
    assert done["ok"] is True and done["workspace"] == "Inhouse" and done["role"] == "owner"
    assert 'MULTILOGIN_WORKSPACE_ID_A="ws-in"' in _machine_env()

    again = _post("/multilogin-signin-workspace",
                  {"pending": first["pending"], "workspace_id": "ws-in", "instance": "A"})
    assert again["ok"] is False and "press Connect again" in again["error"]


def test_connect_moves_the_old_automation_token_aside(mlx, monkeypatch):
    """Чужой токен шёл бы первым и упирался в «can't lock profile»."""
    old = jwt(905, T0 + 86400)
    monkeypatch.setenv("MULTILOGIN_AUTOMATION_TOKEN_A", old)
    assert _connect()["ok"] is True
    assert os.environ["MULTILOGIN_AUTOMATION_TOKEN_A"] == ""
    assert os.environ["MULTILOGIN_AUTOMATION_TOKEN_A_PREVIOUS"] == old
    assert cred.for_slot("A").method() == "signin"


def test_a_failed_reconnect_keeps_the_working_connection(mlx, monkeypatch):
    _save_session(monkeypatch, refresh="r-keep")
    body = _post("/multilogin-signin", {"email": "buyer@example.test", "password": "wrong",
                                        "instance": "A"})
    assert body["ok"] is False and "wrong credentials" in body["error"]
    assert os.environ["MULTILOGIN_REFRESH_TOKEN_A"] == "r-keep"
    assert "MULTILOGIN_REFRESH_TOKEN_A" not in _machine_env()


def test_status_says_signed_in_and_never_returns_a_token(mlx, monkeypatch):
    _save_session(monkeypatch)
    state = _get("/multilogin-token-status?instance=A")
    assert state["alive"] is True and state["method"] == "signin"
    assert state["email"] == "buyer@example.test" and state["workspace"] == "Inhouse"
    assert state["renews"] is True
    text = json.dumps(state)
    assert "eyJ" not in text and "r-0" not in text and mlx.refresh_valid not in text


def test_status_of_a_refused_sign_in_asks_for_reconnect(mlx, monkeypatch):
    _save_session(monkeypatch)
    mlx.refresh_rejected = True
    state = _get("/multilogin-token-status?instance=A")
    assert state["alive"] is False and state["reason"] == "signin_expired"
    assert "press Reconnect" in state["error"]


def test_status_of_an_automation_token_is_labelled(mlx, monkeypatch):
    token = jwt(906, T0 + 86400)
    mlx.valid_bearers.add(token)
    monkeypatch.setenv("MULTILOGIN_AUTOMATION_TOKEN_A", token)
    state = _get("/multilogin-token-status?instance=A")
    assert state["alive"] is True and state["method"] == "automation"


def test_search_renews_the_sign_in_on_a_401(mlx, monkeypatch):
    _save_session(monkeypatch)
    cred.for_slot("A").bearer()
    mlx.valid_bearers.clear()
    body = _post("/multilogin-search", {"search_text": "", "instance": "A"})
    assert body["ok"] is True and [p["profile_id"] for p in body["profiles"]] == [PROFILE]
    assert mlx.count("/user/refresh_token") == 2


def test_search_on_a_refused_sign_in_says_reconnect(mlx, monkeypatch):
    _save_session(monkeypatch)
    mlx.refresh_rejected = True
    body = _post("/multilogin-search", {"search_text": "", "instance": "A"})
    assert body["ok"] is False and body["error"] == cred.SIGNIN_EXPIRED


# ── секреты ───────────────────────────────────────────────────────────────

def test_tokens_are_scrubbed_from_telemetry_and_sentry():
    from src import acl_sentry
    from src.fb_ads_rpa import run_telemetry as rt

    bearer = jwt(907, T0)
    text = (f'Start failed with {bearer} and {{"refresh_token": "opaque-refresh-123"}} '
            f'and ?refresh_token=opaque-refresh-456')
    clean = rt.scrub(text)
    for secret in (bearer, "opaque-refresh-123", "opaque-refresh-456"):
        assert secret not in clean
    diag = rt.scrub_diag({"refresh_token": "opaque", "MULTILOGIN_REFRESH_TOKEN_A": "opaque2",
                          "bearer_token": "x", "step": "start"})
    assert diag == {"refresh_token": "<hidden>", "MULTILOGIN_REFRESH_TOKEN_A": "<hidden>",
                    "bearer_token": "<hidden>", "step": "start"}
    event = acl_sentry._before_send({"exception": {"values": [{"value": f"boom {bearer}"}]},
                                     "extra": {"refresh_token": "opaque-refresh-789"}}, {})
    assert bearer not in json.dumps(event) and "opaque-refresh-789" not in json.dumps(event)


def test_the_secret_scanner_knows_the_refresh_token():
    import re

    from scripts import acl_scan_secrets as scan

    rule = next(p for name, p, _why in scan._RULES if name == "Multilogin token")
    # Строки собираются на ходу: записанные целиком, они бы сами попались
    # сканеру — ровно этим правилом.
    body = "ey" + "JhbGciOiJIUzI1NiJ9.x.y"
    assert re.search(rule, "MULTILOGIN_REFRESH_TOKEN_A=" + f'"{body}"')
    assert re.search(rule, "MULTILOGIN_AUTOMATION_TOKEN_B=" + body)


def test_renewal_never_logs_a_token(mlx, monkeypatch, caplog):
    import logging

    caplog.set_level(logging.DEBUG)
    _save_session(monkeypatch)
    bearer = cred.for_slot("A").bearer()
    mlx.refresh_rejected = True
    cred.for_slot("A")._bearer = ""
    with pytest.raises(cred.MlxSignInExpired):
        cred.for_slot("A").bearer()
    logged = caplog.text
    assert bearer not in logged and "r-0" not in logged and mlx.refresh_valid not in logged
