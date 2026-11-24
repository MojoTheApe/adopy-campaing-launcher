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


def _group(lid, title, count, cfg):
    return {"configuration": {"id": cfg, "profile": "page-settings-v1"}, "count": count,
            "representative": _landing(lid, title)}


def test_variant_is_sent_as_typed_with_kind_variant_and_grouping(monkeypatch):
    calls = []
    monkeypatch.setattr(jc, "_call", lambda tool, args: calls.append((tool, args)) or _ready([_group(1, "a", 2, "c1")]))
    out = jc.search("1_ar_fm")
    assert calls == [("search_landing_catalog", {"query": "1_ar_fm", "kind": "variant", "group_by": "configuration",
                                                 "page": 1, "page_size": jc.PAGE_SIZE})]
    assert out["ok"] and out["searched_as"] == "variant"


def test_path_search_is_grouped_too(monkeypatch):
    seen = []
    monkeypatch.setattr(jc, "_call", lambda tool, args: seen.append(args) or _ready([]))
    jc.search("/x")
    assert seen[0]["kind"] == "path" and seen[0]["group_by"] == "configuration"


def test_one_card_per_configuration_with_instance_count_and_totals(monkeypatch):
    raw = _ready([_group(1, "a", 2, "c1"), _group(3, "b", 1, "c2")], total=2, matching_landings=3)
    monkeypatch.setattr(jc, "_call", lambda tool, args: raw)
    out = jc.search("1_ar_fm")
    assert [(i["id"], i["count"], i["configuration_id"]) for i in out["items"]] == [(1, 2, "c1"), (3, 1, "c2")]
    assert out["total"] == 2 and out["matching_landings"] == 3
    assert out["items"][0]["url"] == "https://test.example.com/a"  # представитель, а не вся конфигурация


def test_url_resolves_the_landing_then_asks_template_variants_across_domains(monkeypatch):
    seen = []

    def fake(tool, args):
        seen.append(args)
        return _ready([_landing(57150)]) if args.get("kind") == "url" else _ready([_group(57150, "a", 114, "c1")])

    monkeypatch.setattr(jc, "_call", fake)
    out = jc.search("https://a.example.com/x", stock_id=48)
    assert seen[0]["kind"] == "url" and "group_by" not in seen[0] and "stock_ids" not in seen[0]
    assert seen[1] == {"source_landing_id": 57150, "group_by": "configuration", "page": 1,
                       "page_size": jc.PAGE_SIZE, "stock_ids": [48]}
    assert out["items"][0]["count"] == 114


def test_url_that_is_not_in_the_catalog_finds_nothing_without_a_second_call(monkeypatch):
    seen = []
    monkeypatch.setattr(jc, "_call", lambda tool, args: seen.append(args) or _ready([]))
    out = jc.search("https://a.example.com/x")
    assert out["total"] == 0 and out["items"] == [] and len(seen) == 1


def test_url_hit_without_an_id_finds_nothing_instead_of_failing(monkeypatch):
    seen = []
    monkeypatch.setattr(jc, "_call", lambda tool, args: seen.append(args) or _ready([{"url": "https://a.example.com/x"}]))
    out = jc.search("https://a.example.com/x")
    assert out["total"] == 0 and out["items"] == [] and len(seen) == 1


def test_url_resolving_that_is_pending_is_returned_as_pending(monkeypatch):
    monkeypatch.setattr(jc, "_call", lambda t, a: {"ok": True, "state": "pending", "retry_after_seconds": 3})
    assert jc.search("https://a.example.com/x")["state"] == "pending"


def test_paging_keeps_snapshot_and_filters(monkeypatch):
    seen = []
    monkeypatch.setattr(jc, "_call", lambda tool, args: seen.append(args) or _ready([_group(1, "a", 1, "c")]))
    jc.search("1_ar_fm", page=2, snapshot_id="snap", stock_id=418)
    assert seen == [{"query": "1_ar_fm", "kind": "variant", "group_by": "configuration", "page": 2,
                     "page_size": jc.PAGE_SIZE, "snapshot_id": "snap", "stock_ids": [418]}]


def test_items_carry_only_card_fields(monkeypatch):
    raw = _group(1, "a", 2, "c1")
    raw["representative"]["secret_extra"] = "x"
    monkeypatch.setattr(jc, "_call", lambda tool, args: _ready([raw]))
    item = jc.search("/a")["items"][0]
    assert set(item) == set(jc.GROUP_FIELDS)


# ───────────────────────── фильтр по ассету ─────────────────────────

def test_asset_only_search_sends_stock_ids_without_a_query(monkeypatch):
    seen = []
    monkeypatch.setattr(jc, "_call", lambda tool, args: seen.append((tool, args)) or _ready([_landing()]))
    out = jc.search("", stock_id=418)
    assert seen[0][0] == "search_landing_catalog" and seen[0][1]["stock_ids"] == [418]
    assert "query" not in seen[0][1] and out["searched_as"] == "asset" and out["ok"]
    assert seen[0][1]["group_by"] == "configuration"


def test_asset_filter_combines_with_text(monkeypatch):
    seen = []
    monkeypatch.setattr(jc, "_call", lambda tool, args: seen.append(args) or _ready([_landing()]))
    jc.search("aipl_en_1_lp", stock_id=418)
    assert seen[0]["query"] == "aipl_en_1_lp" and seen[0]["kind"] == "variant" and seen[0]["stock_ids"] == [418]


def test_no_text_and_no_asset_is_still_invalid():
    assert jc.search("  ")["code"] == "invalid_query"


def test_find_stocks_returns_id_and_name_only(monkeypatch):
    monkeypatch.setattr(jc, "_call", lambda tool, args: {"ok": True, "matches": 2, "stocks": [
        {"id": 418, "name": "A.I.", "x": 1}, {"id": 666, "name": "AA Acady"}]})
    out = jc.find_stocks("a.")
    assert out["stocks"] == [{"id": 418, "name": "A.I."}, {"id": 666, "name": "AA Acady"}]


def test_find_stocks_ignores_one_letter_and_does_not_call(monkeypatch):
    monkeypatch.setattr(jc, "_call", lambda t, a: pytest.fail("must not call"))
    assert jc.find_stocks("a")["stocks"] == []


def test_stock_endpoints(monkeypatch):
    seen = []
    monkeypatch.setattr(jc, "_call", lambda t, a: seen.append((t, a)) or (
        {"ok": True, "matches": 1, "stocks": [{"id": 418, "name": "A.I."}]} if t == "find_stocks" else _ready([_landing()])))
    c = _client()
    assert c.post("/api/jcloud/stocks/search", headers=HDR, data={"query": "a.i"}).json()["stocks"][0]["id"] == 418
    r = c.post("/api/jcloud/landings/search", headers=HDR, data={"query": "", "stock_id": "418"})
    assert r.json()["ok"] and seen[-1][1]["stock_ids"] == [418]


# ───────────────────────── ожидание и ошибки ─────────────────────────

def test_pending_is_returned_with_pause(monkeypatch):
    monkeypatch.setattr(jc, "_call", lambda t, a: {"ok": True, "state": "pending", "stage": "catalog",
                                                   "retry_after_seconds": 3})
    out = jc.search("aipl")
    assert out["state"] == "pending" and out["retry_after_seconds"] == 3 and out["searched_as"] == "variant"


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
