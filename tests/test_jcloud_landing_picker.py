"""ACL-2852: поиск лендингов в JCloudFront и выбор до трёх лендингов на аккаунт."""
from __future__ import annotations

import json

import pytest

from src.jcloud import client as jc
from src.web import account_landings as store

TOKEN = "tok-SECRET-1234567890abcdef"


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("JCLOUD_MCP_URL", "http://127.0.0.1:18765/mcp")
    monkeypatch.setenv("JCLOUD_MCP_TOKEN", TOKEN)
    monkeypatch.setattr(store, "_path", lambda: tmp_path / "account_landings.json")


def _ready(items, **extra):
    return {"ok": True, "state": "ready", "page": 1, "items": items, "total": len(items),
            "has_more": False, "snapshot_id": "snap", "complete": True, "unclassified_count": 0, **extra}


def _landing(lid=21101, title="aipl_en_1_lp"):
    return {"id": lid, "title": title, "url": f"https://test.example.com/{title}", "domain": "test.example.com",
            "path": f"/{title}", "template": "react_aipl_7780", "language": "en", "status": None,
            "page_type": "funnel", "stock_ids": [418]}


# ───────────────────────── что ввёл человек ─────────────────────────

@pytest.mark.parametrize("text,kind,query", [
    ("https://a.example.com/x", "url", "https://a.example.com/x"),
    ("  /aipl_en_1_lp ", "path", "/aipl_en_1_lp"),
    ("aipl_en_1_lp", "name", "aipl_en_1_lp"),
    ("", "name", ""),
])
def test_classify_query(text, kind, query):
    assert jc.classify_query(text) == (kind, query)


def test_variant_is_searched_as_path_first(monkeypatch):
    calls = []
    monkeypatch.setattr(jc, "_call", lambda tool, args: calls.append((tool, args)) or _ready([_landing()]))
    out = jc.search("aipl_en_1_lp")
    assert calls[0][1]["query"] == "/aipl_en_1_lp" and calls[0][1]["kind"] == "path"
    assert out["ok"] and out["searched_as"] == "path" and out["items"][0]["id"] == 21101
    assert len(calls) == 1


def test_variant_without_path_match_falls_back_to_template(monkeypatch):
    seen = []

    def fake(tool, args):
        seen.append(args)
        return _ready([]) if args["kind"] == "path" else _ready([_landing()])

    monkeypatch.setattr(jc, "_call", fake)
    out = jc.search("aipl")
    assert [a["kind"] for a in seen] == ["path", "template"]
    assert seen[1]["template_match"] == "contains"
    assert out["searched_as"] == "template" and out["total"] == 1


def test_url_and_path_do_not_fall_back(monkeypatch):
    seen = []
    monkeypatch.setattr(jc, "_call", lambda tool, args: seen.append(args) or _ready([]))
    assert jc.search("https://a.example.com/x")["total"] == 0
    assert jc.search("/x")["total"] == 0
    assert [a["kind"] for a in seen] == ["url", "path"]


def test_paging_keeps_mode_and_snapshot(monkeypatch):
    seen = []
    monkeypatch.setattr(jc, "_call", lambda tool, args: seen.append(args) or _ready([_landing()]))
    jc.search("aipl", page=2, snapshot_id="snap", mode="template")
    assert seen == [{"query": "aipl", "kind": "template", "template_match": "contains",
                     "page": 2, "page_size": jc.PAGE_SIZE, "snapshot_id": "snap"}]


def test_items_carry_only_card_fields(monkeypatch):
    raw = {**_landing(), "secret_extra": "x"}
    monkeypatch.setattr(jc, "_call", lambda tool, args: _ready([raw]))
    item = jc.search("/aipl_en_1_lp")["items"][0]
    assert set(item) == set(jc.ITEM_FIELDS)


# ───────────────────────── ожидание и ошибки ─────────────────────────

def test_pending_is_returned_with_pause(monkeypatch):
    monkeypatch.setattr(jc, "_call", lambda t, a: {"ok": True, "state": "pending", "stage": "catalog",
                                                   "retry_after_seconds": 3})
    out = jc.search("aipl", mode="template")
    assert out["state"] == "pending" and out["retry_after_seconds"] == 3 and out["searched_as"] == "template"


@pytest.mark.parametrize("code,fragment", [
    ("invalid_query", "does not look like"),
    ("query_too_broad", "Narrow the search"),
    ("snapshot_changed", "Search again"),
])
def test_server_error_codes_get_plain_messages(monkeypatch, code, fragment):
    monkeypatch.setattr(jc, "_call", lambda t, a: {"ok": False, "error": {"code": code, "message": "raw"}})
    out = jc.search("/x")
    assert out["ok"] is False and out["code"] == code and fragment in out["message"]


def test_incomplete_catalog_reports_unclassified(monkeypatch):
    monkeypatch.setattr(jc, "_call", lambda t, a: _ready([_landing()], complete=False, unclassified_count=4))
    out = jc.search("/x")
    assert out["complete"] is False and out["unclassified_count"] == 4


def test_not_configured_makes_no_call(monkeypatch):
    monkeypatch.delenv("JCLOUD_MCP_TOKEN")
    monkeypatch.setattr(jc, "_call", lambda t, a: pytest.fail("must not call"))
    assert jc.search("/x")["code"] == "not_configured"


def test_empty_query_is_invalid():
    assert jc.search("   ")["code"] == "invalid_query"


def test_unreachable_transport(monkeypatch):
    import httpx

    def boom(t, a):
        raise ExceptionGroup("tg", [httpx.ConnectError("refused")])

    monkeypatch.setattr(jc, "_call", boom)
    assert jc.search("/x")["code"] == "unreachable"


def test_rejected_token_reports_unauthorized(monkeypatch):
    import httpx

    def boom(t, a):
        req = httpx.Request("POST", "http://x")
        raise ExceptionGroup("tg", [httpx.HTTPStatusError("401", request=req, response=httpx.Response(401, request=req))])

    monkeypatch.setattr(jc, "_call", boom)
    assert jc.search("/x")["code"] == "unauthorized"


def test_token_never_appears_in_any_result(monkeypatch):
    def boom(t, a):
        raise RuntimeError(f"failed with {TOKEN}")

    monkeypatch.setattr(jc, "_call", boom)
    assert TOKEN not in json.dumps(jc.search("/x"))
    assert TOKEN not in jc._redact(f"x {TOKEN} y")


# ───────────────────────── лендинги аккаунта ─────────────────────────

@pytest.mark.parametrize("n,shares", [(0, []), (1, [100]), (2, [50, 50]), (3, [33, 33, 34])])
def test_even_split(n, shares):
    assert store.even_split(n) == shares


def test_adding_recalculates_evenly_and_caps_at_three():
    assert store.add_landing("Acc", _landing(1))["landings"][0]["percent"] == 100
    two = store.add_landing("Acc", _landing(2, "b"))["landings"]
    assert [l["percent"] for l in two] == [50, 50]
    three = store.add_landing("Acc", _landing(3, "c"))["landings"]
    assert [l["percent"] for l in three] == [33, 33, 34]
    out = store.add_landing("Acc", _landing(4, "d"))
    assert out["ok"] is False and len(out["landings"]) == 3


def test_removing_recalculates_evenly():
    for i in (1, 2, 3):
        store.add_landing("Acc", _landing(i, f"t{i}"))
    left = store.remove_landing("Acc", 2)["landings"]
    assert [l["id"] for l in left] == [1, 3] and [l["percent"] for l in left] == [50, 50]


def test_duplicate_landing_is_refused():
    store.add_landing("Acc", _landing(1))
    out = store.add_landing("Acc", _landing(1))
    assert out["ok"] is False and len(out["landings"]) == 1


def test_percent_can_be_edited_and_is_validated():
    store.add_landing("Acc", _landing(1))
    store.add_landing("Acc", _landing(2, "b"))
    assert store.set_percent("Acc", 1, 70)["landings"][0]["percent"] == 70
    assert store.set_percent("Acc", 1, 101)["ok"] is False
    assert store.set_percent("Acc", 1, "abc")["ok"] is False
    assert store.get_landings("Acc")[0]["percent"] == 70


def test_choice_is_per_account_and_survives_reload():
    store.add_landing("A", _landing(1))
    store.add_landing("B", _landing(2, "b"))
    assert [l["id"] for l in store.get_landings("A")] == [1]
    assert [l["id"] for l in store.get_landings("B")] == [2]


def test_landing_without_id_is_refused():
    assert store.add_landing("A", {"title": "x"})["ok"] is False
    assert store.add_landing("A", "nope")["ok"] is False


# ───────────────────────── эндпоинты ─────────────────────────

def _client():
    from fastapi.testclient import TestClient
    from src.web import app as webapp
    return TestClient(webapp.app, base_url="http://127.0.0.1:9090")


HDR = {"Origin": "http://127.0.0.1:9090"}


def test_search_endpoint_returns_results_without_token(monkeypatch):
    monkeypatch.setattr(jc, "_call", lambda t, a: _ready([_landing()]))
    r = _client().post("/api/jcloud/landings/search", headers=HDR, data={"query": "aipl_en_1_lp"})
    assert r.status_code == 200 and r.json()["items"][0]["title"] == "aipl_en_1_lp"
    assert TOKEN not in r.text


def test_landings_endpoint_add_percent_remove():
    c = _client()
    assert c.post("/api/account-landings", headers=HDR, data={}).status_code == 400
    r = c.post("/api/account-landings", headers=HDR,
               data={"account_label": "Acc", "op": "add", "landing": json.dumps(_landing())})
    assert r.json()["landings"][0]["percent"] == 100
    r = c.post("/api/account-landings", headers=HDR,
               data={"account_label": "Acc", "op": "percent", "id": "21101", "percent": "60"})
    assert r.json()["landings"][0]["percent"] == 60
    assert c.post("/api/account-landings", headers=HDR, data={"account_label": "Acc"}).json()["landings"]
    r = c.post("/api/account-landings", headers=HDR, data={"account_label": "Acc", "op": "remove", "id": "21101"})
    assert r.json()["landings"] == []


def test_account_card_has_the_landings_row_and_script():
    from src.web import app as webapp
    from pathlib import Path
    tpl = (Path(webapp.__file__).parent / "templates" / "report.html").read_text(encoding="utf-8")
    assert "data-landings-account" in tpl and "/static/account_landings.js" in tpl
    assert (Path(webapp.__file__).parent / "static" / "account_landings.js").is_file()
