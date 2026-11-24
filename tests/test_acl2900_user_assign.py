"""ACL-2900: назначение аккаунтов пользователю через хаб."""
from __future__ import annotations

import asyncio
import json
import urllib.error
from pathlib import Path
from unittest.mock import patch

import pytest
from starlette.requests import Request

from src.meta_campaign_agent.ad_account_map import AccountMapEntry
from src.web import user_assign as ua
from src.web.app import normalise_ad_account_id

# Номера кабинетов собираются при запуске: длинные цифры в исходнике сканер секретов
# (scripts/acl_scan_secrets.py) принимает за настоящие кабинеты.
D1, D2, D3, D4, D5, D9 = ("1" * 16, "2" * 16, "3" * 16, "4" * 16, "5" * 16, "9" * 16)
ACT_A = "act_" + D1
ACT_B = "act_" + D2


def _entry(act: str = "", **kw) -> AccountMapEntry:
    return AccountMapEntry(ad_account_id=act, page_id=kw.get("page_id"), multilogin_instance="A",
                           folder_id=kw.get("folder_id"), profile_id=kw.get("profile_id"),
                           proxy_host="1.2.3.4", proxy_user="u", proxy_pass="secret",
                           tracking_link="https://t.example/x", country=kw.get("country"),
                           page_name=kw.get("page_name"))


def _map() -> dict:
    return {
        "Alpha 1": _entry(ACT_A, profile_id="p-1", folder_id="f-1", country="CL", page_id="77"),
        "Beta 2": _entry(ACT_B, profile_id="p-2"),
        "Gamma Twin": _entry("act_" + D3),
        "Gamma Twin 2": _entry("act_" + D4),
        "Clash X": _entry("act_" + D5),
        "Clash Y": _entry("act_" + D5),
    }


def _resolve(text: str, labels=()):
    return ua.resolve_lines(ua.parse_lines(text), _map(), labels, normalise=normalise_ad_account_id)


# ── разбор строк ─────────────────────────────────────────────────────────

def test_parse_lines_skips_blank_and_trims():
    assert ua.parse_lines("  a \n\n b\r\n   \nc") == ["a", "b", "c"]


@pytest.mark.parametrize("line,expected", [
    (D1, ACT_A),
    ("act_" + D1, ACT_A),
    ("https://adsmanager.facebook.com/adsmanager/manage/campaigns?act=" + D1 + "&nav_entry_point=x", ACT_A),
    ("https://business.facebook.com/adsmanager/manage/accounts?business_id=9&act=" + D1, ACT_A),
    ("https://www.facebook.com/ads/manager/account_settings/account_info/?act=" + D1, ACT_A),
])
def test_ids_links_resolve_by_ad_account_id(line, expected):
    (res,) = _resolve(line)
    assert res["state"] == "found" and res["account_name"] == "Alpha 1"
    assert res["ad_account_id"] == expected


def test_name_is_not_taken_for_an_id_even_with_digits():
    assert not ua.looks_like_id("Facebook Elf 12345678 CL")
    assert ua.looks_like_id("act_123456789") and ua.looks_like_id("123456789")


def test_name_match_is_case_insensitive_and_uses_shared_list():
    res = _resolve("alpha 1\nSheet Only", labels=["Sheet Only"])
    assert [r["state"] for r in res] == ["found", "found"]
    assert res[1]["row"]["account_name"] == "Sheet Only" and res[1]["row"]["ad_account_id"] == ""


# ── состояния ────────────────────────────────────────────────────────────

def test_not_found():
    (res,) = _resolve("Nobody Here\n"[:-1])
    assert res["state"] == "not_found" and res["row"] is None
    (res,) = _resolve("act_" + D9)
    assert res["state"] == "not_found"


def test_ambiguous_lists_candidates():
    (res,) = _resolve("Gamma")
    assert res["state"] == "ambiguous"
    assert res["candidates"] == ["Gamma Twin", "Gamma Twin 2"]


def test_partial_name_match_is_marked():
    (res,) = _resolve("Alpha")
    assert res["state"] == "found" and res["account_name"] == "Alpha 1" and res["match"] == "partial"
    (exact,) = _resolve("alpha 1")
    assert exact["state"] == "found" and "match" not in exact


def test_exact_name_wins_over_longer_names():
    (res,) = _resolve("gamma twin")
    assert res["state"] == "found" and res["account_name"] == "Gamma Twin"


def test_duplicate_when_two_lines_hit_one_account():
    res = _resolve("Alpha 1\n" + ACT_A + "\nBeta 2")
    assert [r["state"] for r in res] == ["found", "duplicate", "found"]
    assert res[1]["account_name"] == "Alpha 1"


def test_clash_when_ad_account_shared_by_another_account():
    res = _resolve("Clash X")
    assert res[0]["state"] == "clash" and res[0]["candidates"] == ["Clash Y"]
    assert res[0]["row"] is None
    # а по номеру кабинета это неоднозначность между двумя именами
    (res,) = _resolve("act_" + D5)
    assert res["state"] == "ambiguous"


def test_row_has_only_allowed_fields():
    (res,) = _resolve("Alpha 1")
    assert list(res["row"]) == list(ua.ROW_FIELDS)
    assert res["row"] == {
        "account_name": "Alpha 1", "ad_account_id": ACT_A, "page_id": "77", "page_name": "",
        "country": "CL", "multilogin_instance": "A", "folder_id": "f-1", "profile_id": "p-1",
    }
    assert not {"proxy_host", "proxy_pass", "proxy_user", "tracking_link"} & set(res["row"])


# ── хаб ──────────────────────────────────────────────────────────────────

def _client(status=200, data=None, boom=None, calls=None):
    def request(method, url, body, headers):
        if calls is not None:
            calls.append((method, url, json.loads(body) if body else None, headers))
        if boom:
            raise boom
        return status, data if data is not None else {}
    return ua.HubClient("https://hub.example/", "KEY", request)


def test_client_sends_bearer_and_payload():
    calls = []
    _client(data={"ok": True}, calls=calls).assign("a@b.co", [{"account_name": "X"}])
    method, url, body, headers = calls[0]
    assert (method, url) == ("POST", "https://hub.example/api/assignments")
    assert body == {"user_email": "a@b.co", "rows": [{"account_name": "X"}]}
    assert headers["Authorization"] == "Bearer KEY"


@pytest.mark.parametrize("status", [401, 403])
def test_client_auth_errors_are_clear(status):
    with pytest.raises(ua.HubError, match=f"HTTP {status}"):
        _client(status=status).users()


def test_client_unreachable_and_server_error():
    with pytest.raises(ua.HubError, match="Could not reach the hub"):
        _client(boom=urllib.error.URLError("down")).users()
    with pytest.raises(ua.HubError, match="HTTP 400: unknown field: x"):
        _client(status=400, data={"error": "unknown field: x"}).assign("a@b.co", [{}])


# ── маршруты ─────────────────────────────────────────────────────────────

def _call(handler, body=None):
    raw = json.dumps(body or {}).encode()
    scope = {"type": "http", "method": "POST", "path": "/x", "headers": [(b"content-type", b"application/json")]}

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}

    async def run():
        resp = await handler(Request(scope, receive=receive)) if body is not None else await handler()
        return resp.status_code, json.loads(resp.body)

    return asyncio.run(run())


@pytest.fixture
def owner(monkeypatch, tmp_path):
    """Ключ владельца задан; карта — временный CSV; общий список подменён."""
    monkeypatch.setenv("ACL_HUB_OWNER_KEY", "KEY")
    monkeypatch.setenv("ACL_HUB_URL", "https://hub.example")
    csv_path = tmp_path / "ad_account_map.csv"
    csv_path.write_text(
        "account_name,ad_account_id,page_id,multilogin_instance,folder_id,profile_id,proxy_host,tracking_link,country\n"
        "Alpha 1,act_" + D1 + ",77,A,f-1,p-1,9.9.9.9,https://t.example,CL\n"
        "Beta 2,act_" + D2 + ",,A,,p-2,,,\n", encoding="utf-8")
    with patch("src.web.app.get_ad_account_map_path", return_value=str(csv_path)), \
            patch("src.analysis_agent.gsheet_accounts.fetch_account_rows",
                  return_value=[{"account_label": "Sheet Only"}]):
        yield


def test_controls_hidden_and_routes_refuse_without_key(monkeypatch):
    from src.web import app as app_mod

    monkeypatch.delenv("ACL_HUB_OWNER_KEY", raising=False)
    assert app_mod._user_assign_enabled() is False
    for handler, body in [(app_mod.api_user_assign_preview, {"text": "x"}),
                          (app_mod.api_user_assign_send, {"user_email": "a@b.co", "text": "x"}),
                          (app_mod.api_user_assign_remove, {"user_email": "a@b.co", "account_names": ["x"]})]:
        status, data = _call(handler, body)
        assert status == 403 and data["ok"] is False and "not available" in data["error"]
    status, data = _call(app_mod.api_user_assign_users)
    assert status == 403


def test_page_flag_follows_key(monkeypatch):
    from src.web import app as app_mod

    monkeypatch.setenv("ACL_HUB_OWNER_KEY", "KEY")
    assert app_mod._user_assign_enabled() is True
    # Шаблон карты подключает блок только по этому флагу.
    src = (Path(app_mod.__file__).parent / "templates" / "_account_map.html").read_text(encoding="utf-8")
    assert '{% if user_assign_enabled is defined and user_assign_enabled() %}{% include "_user_assign.html" %}{% endif %}' in src


def test_preview_route(owner):
    from src.web import app as app_mod

    status, data = _call(app_mod.api_user_assign_preview,
                         {"text": "Alpha 1\nSheet Only\nnope\nhttps://x.com/?act=" + D2})
    assert status == 200
    assert [r["state"] for r in data["results"]] == ["found", "found", "not_found", "found"]


def test_send_includes_only_found_rows(owner, monkeypatch):
    from src.web import app as app_mod

    calls = []
    real = ua.HubClient

    def make(*a, **k):
        return real("https://hub.example", "KEY", lambda m, u, b, h: (calls.append((u, json.loads(b))) or (200, {"ok": True})))

    monkeypatch.setattr(ua, "HubClient", make)
    status, data = _call(app_mod.api_user_assign_send,
                         {"user_email": "New@Buyer.co", "text": "Alpha 1\nnope\nalpha 1\nSheet Only"})
    assert status == 200 and data["ok"] is True
    (url, body), = calls
    assert url.endswith("/api/assignments") and body["user_email"] == "new@buyer.co"
    assert [r["account_name"] for r in body["rows"]] == ["Alpha 1", "Sheet Only"]
    for row in body["rows"]:
        assert set(row) == set(ua.ROW_FIELDS)
    states = [r["state"] for r in data["results"]]
    assert states == ["found", "not_found", "duplicate", "found"]   # не отправленные остаются видны
    assert data["assigned"] == ["Alpha 1", "Sheet Only"]


def test_send_surfaces_hub_error_and_validates(owner, monkeypatch):
    from src.web import app as app_mod

    real = ua.HubClient
    monkeypatch.setattr(ua, "HubClient", lambda *a, **k: real("https://hub.example", "KEY", lambda *x: (403, {})))
    status, data = _call(app_mod.api_user_assign_send, {"user_email": "a@b.co", "text": "Alpha 1"})
    assert data["ok"] is False and "HTTP 403" in data["error"] and data["results"][0]["state"] == "found"
    status, data = _call(app_mod.api_user_assign_send, {"user_email": "nope", "text": "Alpha 1"})
    assert status == 400
    status, data = _call(app_mod.api_user_assign_send, {"user_email": "a@b.co", "text": "zzz"})
    assert data["ok"] is False and "Nothing to assign" in data["error"]


def test_remove_route(owner, monkeypatch):
    from src.web import app as app_mod

    calls = []
    real = ua.HubClient
    monkeypatch.setattr(ua, "HubClient", lambda *a, **k: real(
        "https://hub.example", "KEY", lambda m, u, b, h: (calls.append((u, json.loads(b))) or (200, {"ok": True}))))
    status, data = _call(app_mod.api_user_assign_remove, {"user_email": "a@b.co", "account_names": ["Alpha 1"]})
    assert data == {"ok": True, "removed": ["Alpha 1"]}
    assert calls == [("https://hub.example/api/assignments/remove",
                      {"user_email": "a@b.co", "account_names": ["Alpha 1"]})]
    status, data = _call(app_mod.api_user_assign_remove, {"user_email": "a@b.co", "account_names": []})
    assert status == 400
