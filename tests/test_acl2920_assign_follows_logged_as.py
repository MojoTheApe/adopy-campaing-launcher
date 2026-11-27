"""ACL-2920: раздел Accounts следует за байером из «Logged as» (отдельного выбора юзера нет)."""
from __future__ import annotations

import asyncio
import json
import urllib.parse
from pathlib import Path
from unittest.mock import patch

import pytest
from starlette.requests import Request

from src.web import user_assign as ua

D1, D2 = ("1" * 16, "2" * 16)   # номера кабинетов собираем в тесте: сканер секретов не любит длинные цифры в коде
OWNER_KEY = "OWNERKEY-for-tests"


class FakeHub:
    """Хаб в памяти: users по имени, назначения по почте, и журнал вызовов."""

    def __init__(self):
        self.users = {"dan@adopy.test": "Dan Dicker", "gil@adopy.test": "Gil Zangi"}
        self.assigned = {"dan@adopy.test": [
            {"account_name": "Alpha 1", "ad_account_id": "act_" + D1, "removed_at": None, "assigned_by": "owner"},
            {"account_name": "Old One", "ad_account_id": "", "removed_at": "2026-11-20T10:00:00Z", "assigned_by": "owner"},
        ]}
        self.calls = []

    def __call__(self, method, url, body, headers):
        parsed = urllib.parse.urlparse(url)
        q = urllib.parse.parse_qs(parsed.query)
        payload = json.loads(body) if body else None
        self.calls.append((method, parsed.path, q, payload, headers.get("Authorization")))
        if method == "GET" and parsed.path == "/api/users":
            name = (q.get("name") or [""])[0].casefold()
            users = [{"email": e, "name": n, "role": "buyer", "disabled": 0, "active_accounts": 1}
                     for e, n in self.users.items() if not name or n.casefold() == name]
            return 200, {"users": users}
        if method == "GET" and parsed.path == "/api/assignments":
            email = q["user_email"][0]
            if email not in self.users:
                return 404, {"error": "unknown user"}
            return 200, {"user_email": email, "name": self.users[email], "accounts": self.assigned.get(email, [])}
        if method == "POST" and parsed.path == "/api/assignments":
            email = payload["user_email"]
            created = email not in self.users
            if created:
                self.users[email] = payload.get("user_name") or ""
            elif payload.get("user_name") and not self.users[email]:
                self.users[email] = payload["user_name"]
            rows = self.assigned.setdefault(email, [])
            for r in payload["rows"]:
                rows[:] = [x for x in rows if x["account_name"] != r["account_name"]]
                rows.append({**r, "removed_at": None, "assigned_by": "owner"})
            return 200, {"ok": True, "assigned": len(payload["rows"]), "created": created}
        if method == "POST" and parsed.path == "/api/assignments/remove":
            for r in self.assigned.get(payload["user_email"], []):
                if r["account_name"] in payload["account_names"] and not r["removed_at"]:
                    r["removed_at"] = "2026-11-25T10:00:00Z"
            return 200, {"ok": True, "removed": len(payload["account_names"])}
        return 404, {}


@pytest.fixture
def hub(monkeypatch, tmp_path):
    monkeypatch.setenv("ACL_HUB_OWNER_KEY", OWNER_KEY)
    monkeypatch.setenv("ACL_HUB_URL", "https://hub.example")
    from config import paths

    local = tmp_path / "local"
    local.mkdir()
    monkeypatch.setattr(paths, "get_local_config_dir", lambda: str(local))
    from src.web import account_links   # хранилище связей импортирует get_local_config_dir по имени: подменяем и его

    monkeypatch.setattr(account_links, "get_local_config_dir", lambda: str(local))
    csv_path = tmp_path / "ad_account_map.csv"
    csv_path.write_text(
        "account_name,ad_account_id,page_id,multilogin_instance,folder_id,profile_id\n"
        "Alpha 1,act_" + D1 + ",77,A,f-1,p-1\nBeta 2,act_" + D2 + ",,A,,p-2\n", encoding="utf-8")
    fake = FakeHub()
    real = ua.HubClient
    monkeypatch.setattr(ua, "HubClient", lambda *a, **k: real("https://hub.example", OWNER_KEY, fake))
    with patch("src.web.app.get_ad_account_map_path", return_value=str(csv_path)), \
            patch("src.analysis_agent.gsheet_accounts.fetch_account_rows", return_value=[]):
        yield fake


def _post(handler, body):
    raw = json.dumps(body).encode()
    scope = {"type": "http", "method": "POST", "path": "/x", "headers": [(b"content-type", b"application/json")]}

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}

    async def run():
        resp = await handler(Request(scope, receive=receive))
        return resp.status_code, json.loads(resp.body)

    return asyncio.run(run())


def _accounts(buyer):
    from src.web import app as app_mod

    async def run():
        resp = await app_mod.api_user_assign_accounts(buyer)
        return resp.status_code, json.loads(resp.body)

    return asyncio.run(run())


def _send(buyer, text, email=""):
    from src.web import app as app_mod

    return _post(app_mod.api_user_assign_send, {"buyer": buyer, "text": text, "user_email": email})


def _remove(buyer, names, email=""):
    from src.web import app as app_mod

    return _post(app_mod.api_user_assign_remove, {"buyer": buyer, "account_names": names, "user_email": email})


# ── список аккаунтов выбранного байера ───────────────────────────────────

def test_listing_follows_the_selected_buyer_and_shows_removed_rows(hub):
    status, data = _accounts("Dan Dicker")
    assert status == 200 and data["ok"] and data["user_email"] == "dan@adopy.test"
    assert [(a["account_name"], a["removed"]) for a in data["accounts"]] == [("Alpha 1", False), ("Old One", True)]
    status, data = _accounts("gil zangi")                         # другой байер: имя без учёта регистра
    assert data["ok"] and data["user_email"] == "gil@adopy.test" and data["accounts"] == []


def test_listing_view_has_no_secrets_or_extra_fields(hub):
    _, data = _accounts("Dan Dicker")
    assert set(data["accounts"][0]) == {"account_name", "ad_account_id", "removed", "assigned_by"}
    assert OWNER_KEY not in json.dumps(data)


def test_buyer_without_a_hub_user_asks_for_the_email(hub):
    status, data = _accounts("Yinon Avraham")
    assert data["ok"] is False and data["need_email"] is True and data["accounts"] == []
    assert _accounts("")[0] == 400


def test_two_hub_users_with_one_name_are_not_guessed(hub):
    hub.users["twin@adopy.test"] = "Dan Dicker"
    status, data = _accounts("Dan Dicker")
    # ACL-2924: не угадываем, а просим почту (поле на странице показывается), а не оставляем владельца ни с чем
    assert data["ok"] is False and "Several hub users" in data["error"] and data["need_email"] is True


# ── назначение и почта один раз ──────────────────────────────────────────

def test_assign_to_the_selected_buyer_found_by_name_needs_no_email(hub):
    status, data = _send("Dan Dicker", "Beta 2")
    assert data["ok"] and data["assigned"] == ["Beta 2"] and data["user_email"] == "dan@adopy.test"
    (post,) = [c for c in hub.calls if c[0] == "POST"]
    assert post[3]["user_email"] == "dan@adopy.test" and post[3]["user_name"] == "Dan Dicker"
    assert [a["account_name"] for a in hub.assigned["dan@adopy.test"] if not a["removed_at"]] == ["Alpha 1", "Beta 2"]


def test_new_buyer_is_created_by_entering_the_email_once_and_remembered(hub):
    status, data = _send("Yinon Avraham", "Alpha 1")
    assert data["ok"] is False and data["need_email"] is True      # первый раз: просим почту
    assert not [c for c in hub.calls if c[0] == "POST"]            # ничего не отправлено
    status, data = _send("Yinon Avraham", "Alpha 1", email="Yinon@Adopy.test")
    assert data["ok"] and data["user_email"] == "yinon@adopy.test"
    assert hub.users["yinon@adopy.test"] == "Yinon Avraham"        # хаб запомнил имя вместе с почтой
    status, data = _send("Yinon Avraham", "Beta 2")                # во второй раз почту не просим
    assert data["ok"] and data["user_email"] == "yinon@adopy.test"


def test_email_is_remembered_on_the_machine_even_if_the_hub_name_differs(hub):
    hub.users["daniel@adopy.test"] = "Daniel T"                    # в хабе имя из Google другое
    assert _send("Daniel Tyukavkin", "Alpha 1")[1]["need_email"] is True
    assert _send("Daniel Tyukavkin", "Alpha 1", email="daniel@adopy.test")[1]["ok"]
    assert hub.users["daniel@adopy.test"] == "Daniel T"            # имя Google не тронуто
    status, data = _accounts("Daniel Tyukavkin")                   # дальше находим по памяти машины
    assert data["ok"] and data["user_email"] == "daniel@adopy.test"


def test_a_bad_email_is_refused_and_nothing_is_sent(hub):
    status, data = _send("Yinon Avraham", "Alpha 1", email="not an email")
    assert status == 400 and data["need_email"] is True
    assert not [c for c in hub.calls if c[0] == "POST"]


def test_unmatched_lines_are_not_sent_for_the_selected_buyer(hub):
    status, data = _send("Dan Dicker", "nope-nothing-here")
    assert data["ok"] is False and "Nothing to assign" in data["error"]
    assert not [c for c in hub.calls if c[0] == "POST"]


# ── Remove для любой строки списка ───────────────────────────────────────

def test_remove_works_for_any_listed_row_not_only_ones_added_in_this_window(hub):
    # Alpha 1 назначили раньше (в фикстуре), в этом окне ничего не назначали
    status, data = _remove("Dan Dicker", ["Alpha 1"])
    assert data == {"ok": True, "removed": ["Alpha 1"]}
    _, listing = _accounts("Dan Dicker")
    assert all(a["removed"] for a in listing["accounts"])
    (post,) = [c for c in hub.calls if c[1] == "/api/assignments/remove"]
    assert post[3] == {"user_email": "dan@adopy.test", "account_names": ["Alpha 1"]}


def test_remove_needs_a_buyer_and_a_name(hub):
    from src.web import app as app_mod

    assert _post(app_mod.api_user_assign_remove, {"account_names": ["Alpha 1"]})[0] == 400
    assert _post(app_mod.api_user_assign_remove, {"buyer": "Dan Dicker", "account_names": []})[0] == 400


# ── ключ владельца остаётся на сервере ───────────────────────────────────

def test_owner_key_only_goes_to_the_hub_never_to_page_answers(hub):
    answers = [_accounts("Dan Dicker")[1], _send("Dan Dicker", "Beta 2")[1], _remove("Dan Dicker", ["Alpha 1"])[1],
               _accounts("Nobody")[1]]
    assert OWNER_KEY not in json.dumps(answers)
    assert all(c[4] == f"Bearer {OWNER_KEY}" for c in hub.calls)


def test_block_template_has_no_second_user_picker_and_js_reads_logged_as():
    base = Path(__file__).resolve().parent.parent / "src" / "web"
    html = (base / "templates" / "_user_assign.html").read_text(encoding="utf-8")
    js = (base / "static" / "user_assign.js").read_text(encoding="utf-8")
    assert 'id="ua-user"' not in html and "type a new email" not in html
    assert "buyer-select" in js and "/api/user-assign/accounts" in js
    assert "ACL_HUB_OWNER_KEY" not in js and "ACL_HUB_OWNER_KEY" not in html
