"""ACL-2924: почта байера в блоке «Logged as» — одноимённые юзеры, замена запомненной, строка результата."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_acl2920_assign_follows_logged_as import (  # noqa: F401  (фикстура hub и помощники)
    _accounts, _post, _remove, _send, hub,
)

ROOT = Path(__file__).resolve().parent.parent


def _set_email(buyer, email):
    from src.web import app as app_mod

    return _post(app_mod.api_user_assign_buyer_email, {"buyer": buyer, "user_email": email})


# ── одноимённые юзеры ────────────────────────────────────────────────────

def test_same_name_users_ask_for_the_email_and_the_typed_one_is_used_and_remembered(hub):
    hub.users["twin@adopy.test"] = "Dan Dicker"
    status, data = _send("Dan Dicker", "Beta 2")
    assert data["ok"] is False and data["need_email"] is True and "Several hub users" in data["error"]
    assert not [c for c in hub.calls if c[0] == "POST"]               # ничего не отправлено
    status, data = _send("Dan Dicker", "Beta 2", email="dan@adopy.test")
    assert data["ok"] and data["user_email"] == "dan@adopy.test"      # набранная почта использована
    (post,) = [c for c in hub.calls if c[0] == "POST"]
    assert post[3]["user_email"] == "dan@adopy.test"
    status, data = _accounts("Dan Dicker")                            # и запомнена: дальше без вопросов
    assert data["ok"] and data["user_email"] == "dan@adopy.test"
    assert _send("Dan Dicker", "Alpha 1")[1]["ok"]


def test_same_name_remove_asks_for_the_email_too(hub):
    hub.users["twin@adopy.test"] = "Dan Dicker"
    status, data = _remove("Dan Dicker", ["Alpha 1"])
    assert data["ok"] is False and data["need_email"] is True
    assert not [c for c in hub.calls if c[1] == "/api/assignments/remove"]


# ── замена запомненной почты на месте ────────────────────────────────────

def test_a_remembered_email_can_be_replaced_and_the_new_one_is_used_next_time(hub):
    assert _set_email("Dan Dicker", "dan.typo@adopy.test")[1] == {
        "ok": True, "buyer": "Dan Dicker", "user_email": "dan.typo@adopy.test"}
    status, data = _accounts("Dan Dicker")                            # опечатка закрепилась: юзера в хабе нет
    assert data["ok"] and data["user_email"] == "dan.typo@adopy.test" and data["accounts"] == [] and data["new_user"] is True
    assert _set_email("Dan Dicker", "DAN@adopy.test")[1]["ok"]        # исправили
    status, data = _accounts("Dan Dicker")
    assert data["user_email"] == "dan@adopy.test" and [a["account_name"] for a in data["accounts"]] == ["Alpha 1", "Old One"]
    assert _send("Dan Dicker", "Beta 2")[1]["user_email"] == "dan@adopy.test"
    assert not [c for c in hub.calls if c[1] == "/api/assignments" and c[0] == "POST" and c[3]["user_email"] == "dan.typo@adopy.test"]


def test_replacing_the_email_does_not_touch_other_buyers(hub):
    _set_email("Dan Dicker", "dan.other@adopy.test")
    status, data = _accounts("Gil Zangi")
    assert data["user_email"] == "gil@adopy.test"


@pytest.mark.parametrize("bad", ["", "   ", "no-at-sign", "two words@x.co"])
def test_buyer_email_endpoint_refuses_a_bad_email(hub, bad):
    status, data = _set_email("Dan Dicker", bad)
    assert status == 400 and data["ok"] is False
    status, data = _accounts("Dan Dicker")                            # прежнее не испорчено
    assert data["user_email"] == "dan@adopy.test"


def test_buyer_email_endpoint_needs_a_buyer_and_never_calls_the_hub(hub):
    assert _set_email("", "x@adopy.test")[0] == 400
    assert _set_email("Dan Dicker", "x@adopy.test")[1]["ok"]
    assert hub.calls == []                                            # правка почты — только на этой машине


def test_buyer_email_endpoint_is_closed_without_the_owner_key(monkeypatch):
    from src.web import app as app_mod

    monkeypatch.delenv("ACL_HUB_OWNER_KEY", raising=False)
    status, data = _post(app_mod.api_user_assign_buyer_email, {"buyer": "B", "user_email": "a@b.co"})
    assert status == 403 and data["ok"] is False


def test_hub_error_carries_its_status():
    from src.web import user_assign as ua

    assert ua.HubError("x", 404).status == 404 and ua.HubError("x").status == 0


# ── страница: строка результата, правка на месте, одноимённые (JS без браузера) ──

@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_block_script_behaviour_in_a_minimal_dom():
    out = subprocess.run(["node", str(ROOT / "tests" / "ua_block_check.js"),
                          str(ROOT / "src" / "web" / "static" / "user_assign.js")],
                         capture_output=True, text=True, timeout=60, encoding="utf-8")
    report = json.loads(out.stdout.strip().splitlines()[-1])
    assert report["ok"], report["bad"]
    assert len(report["checks"]) >= 14


def test_template_shows_the_email_in_place_without_a_new_button():
    html = (ROOT / "src" / "web" / "templates" / "_user_assign.html").read_text(encoding="utf-8")
    assert 'id="ua-email"' in html and " hidden" not in html.split('id="ua-email"')[1].split(">")[0]
    assert html.count("<button") == 2                                  # только Check и Assign, новых кнопок нет
