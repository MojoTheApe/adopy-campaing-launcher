"""ACL-1798: опись кампаний для Home — разбор, дерево по id, агрегаты, BI-join, бэкенд, хранилище.

Ни Meta, ни Multilogin не трогаются: клиент Graph — заглушка, профиль — `World`
из тестов ACL-1760. Проверяется то, ради чего задача: нет выдуманных нулей и
деревьев по имени, родитель не складывается из детей, BI цепляется только по
точному имени, `rpa` ничего не запускает, команды выключены и ничего не пишут.
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.datastructures import QueryParams

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.analysis_agent import account_spend as sp  # noqa: E402
from src.analysis_agent import account_spend_api as api  # noqa: E402
from src.analysis_agent import campaign_inventory as ci  # noqa: E402
from src.fb_api import graph as g  # noqa: E402

TEMPLATE = ROOT / "src" / "web" / "templates" / "_campaign_table.html"
D = "2026-10-08"


# ───────────────────────── стенд ─────────────────────────

@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(ci, "_store_path", lambda: tmp_path / "campaign_inventory.json")
    ci._RUNNING.clear()
    # ACL-1853: замок профиля берётся по физическому UUID из карты, не по ярлыку.
    from tests.profile_lease_fakes import fake_profile

    monkeypatch.setattr(sp, "_entry_for", lambda account: SimpleNamespace(profile_id=fake_profile(account)))
    yield tmp_path


def camp(i, name, status="ACTIVE", eff=None, **kw):
    return {"id": str(i), "name": name, "status": status, "effective_status": eff or status, **kw}


def adset(i, name, campaign, status="ACTIVE", eff=None, **kw):
    return {"id": str(i), "name": name, "status": status, "effective_status": eff or status,
            "campaign_id": str(campaign), **kw}


def ad(i, name, adset_id, campaign, status="ACTIVE", eff=None):
    return {"id": str(i), "name": name, "status": status, "effective_status": eff or status,
            "campaign_id": str(campaign), "adset_id": str(adset_id)}


def snapshot(**over) -> ci.Snapshot:
    """Снимок: две кампании, три ad set, три объявления, статистика только у части."""
    snap = ci.Snapshot(account="Acc", state="ok", date_from=D, date_to=D, currency="USD",
                       finished_at="2026-10-11 12:00:00")
    snap.campaigns, _ = ci.parse_inventory([
        camp(1, "Camp A", daily_budget="5000", bid_strategy="LOWEST_COST_WITHOUT_CAP", objective="OUTCOME_LEADS"),
        camp(2, "Camp B", status="PAUSED"),
    ], "campaign")
    snap.adsets, _ = ci.parse_inventory([
        adset(11, "Set A1", 1), adset(12, "Set A2", 1, status="PAUSED"),
        adset(21, "Set B1", 2, daily_budget="2500"),
    ], "adset")
    snap.ads, _ = ci.parse_inventory([
        ad(111, "Ad 1", 11, 1), ad(112, "Ad 2", 11, 1), ad(211, "Ad 3", 21, 2, eff="CAMPAIGN_PAUSED"),
    ], "ad")
    snap.steps = {s: "ok" for s in ci.STEPS}
    snap.insights = {
        "campaign": {"1": {"spend": 100.0, "reach": 5000, "impressions": 20000, "frequency": 4.0, "clicks": 400}},
        "adset": {"11": {"spend": 60.0, "reach": 4000, "impressions": 12000, "frequency": 3.0, "clicks": 300}},
        "ad": {},
    }
    for k, v in over.items():
        setattr(snap, k, v)
    return snap


# ───────────────────────── разбор описи ─────────────────────────

def test_inventory_parsing_keeps_ids_and_drops_rows_without_one():
    rows = [camp(1, "A  b"), {"name": "no id"}, camp(1, "dup"), "junk", camp(2, "B", status="paused")]
    objs, dropped = ci.parse_inventory(rows, "campaign")
    assert [o["id"] for o in objs] == ["1", "2"]
    assert dropped == 3
    assert objs[0]["name"] == "A b"                      # пробелы схлопнуты
    assert objs[1]["status"] == "PAUSED"                 # регистр нормализован


def test_inventory_parsing_levels_carry_their_parent_ids_only():
    (c,), _ = ci.parse_inventory([camp(1, "C", campaign_id="9", adset_id="8")], "campaign")
    assert c["campaign_id"] == "" and c["adset_id"] == ""
    (a,), _ = ci.parse_inventory([adset(2, "S", 1)], "adset")
    assert a["campaign_id"] == "1" and a["adset_id"] == ""
    (x,), _ = ci.parse_inventory([ad(3, "Ad", 2, 1)], "ad")
    assert (x["campaign_id"], x["adset_id"]) == ("1", "2")


def test_missing_budget_is_none_not_zero():
    (c,), _ = ci.parse_inventory([camp(1, "C")], "campaign")
    assert c["daily_budget"] is None and c["lifetime_budget"] is None


def test_insights_missing_fields_stay_none_never_zero():
    got, bad = ci.parse_insights([{"campaign_id": "1", "spend": "10.5", "impressions": "100"}], "campaign")
    row = got["1"]
    assert row["spend"] == 10.5 and row["impressions"] == 100
    assert row["reach"] is None and row["frequency"] is None and row["clicks"] is None
    assert bad == 0


def test_insights_duplicate_id_is_dropped_not_summed():
    rows = [{"ad_id": "7", "spend": "1"}, {"ad_id": "7", "spend": "2"}, {"ad_id": "8", "spend": "3"}]
    got, bad = ci.parse_insights(rows, "ad")
    assert "7" not in got and got["8"]["spend"] == 3.0 and bad == 1


# ───────────────────────── дерево по id ─────────────────────────

def test_tree_is_built_from_ids_not_names():
    snap = snapshot()
    # Два ad set с ОДНИМ именем под разными кампаниями: по имени их не разделить, по id — да.
    snap.adsets, _ = ci.parse_inventory([adset(11, "Same", 1), adset(21, "Same", 2)], "adset")
    snap.ads, _ = ci.parse_inventory([ad(111, "Ad", 11, 1), ad(211, "Ad", 21, 2)], "ad")
    tree = ci.build_tree(snap)
    assert [a["id"] for a in tree["adsets_of"]["1"]] == ["11"]
    assert [a["id"] for a in tree["adsets_of"]["2"]] == ["21"]
    assert [x["id"] for x in tree["ads_of"]["21"]] == ["211"]


def test_orphans_are_not_attached_by_name():
    snap = snapshot()
    snap.adsets, _ = ci.parse_inventory([adset(11, "Camp A", 999)], "adset")      # имя как у кампании 1, родитель чужой
    tree = ci.build_tree(snap)
    assert tree["adsets_of"]["1"] == [] and [a["id"] for a in tree["orphan_adsets"]] == ["11"]
    ci.finalize(snap)
    assert snap.state == "partial" and any("not returned" in p for p in snap.problems)


def test_children_states_are_distinct():
    snap = snapshot()
    ok = ci.child_rows(snap, "adset", "1")
    assert ok["state"] == "ok" and [r["id"] for r in ok["rows"]] == ["11", "12"]
    # прочитано, детей нет → empty
    snap.adsets, _ = ci.parse_inventory([adset(11, "Set A1", 1)], "adset")
    assert ci.child_rows(snap, "adset", "2")["state"] == "empty"
    # шаг не читался / упал → not_read (НЕ «детей нет»)
    snap.steps["adsets"] = "failed"
    snap.problems = ["adsets were not read: Meta rate limit reached"]
    nr = ci.child_rows(snap, "adset", "1")
    assert nr["state"] == "not_read" and nr["rows"] == [] and "rate limit" in nr["note"]
    # страницы обрезаны → partial: дети есть, но не все
    snap.steps["adsets"] = "truncated"
    part = ci.child_rows(snap, "adset", "1")
    assert part["state"] == "partial" and len(part["rows"]) == 1 and part["note"]
    # неизвестный родитель — не «пусто»
    assert ci.child_rows(snap, "adset", "nope")["state"] == "not_read"


def test_child_count_state_on_campaign_row():
    snap = snapshot()
    rows = {r["id"]: r for r in ci.campaign_rows(snap)}
    assert rows["1"]["child_count"] == 2 and rows["1"]["child_state"] == "ok"
    snap.steps["adsets"] = "failed"
    rows = {r["id"]: r for r in ci.campaign_rows(snap)}
    assert rows["1"]["child_state"] == "not_read"


# ───────────────────────── статус, доставка, бюджет ─────────────────────────

def test_status_is_configured_and_delivery_is_separate_with_parent_constraint():
    snap = snapshot()
    snap.campaigns[0]["status"] = "PAUSED"          # родитель выключен, ad set свой статус не меняет
    tree = ci.build_tree(snap)
    row = ci.row_payload(snap, tree, "adset", snap.adsets[0], None)
    assert row["status"] == "on"                                  # собственный статус
    assert row["delivery"]["constraint"] == "Campaign paused"     # «On · Campaign paused»
    a = ci.row_payload(snap, tree, "ad", snap.ads[2], None)
    assert a["delivery"]["label"] == "Campaign off" and a["delivery"]["tone"] == "off"


def test_unknown_status_is_unknown_not_off():
    snap = snapshot()
    snap.campaigns[0]["status"] = "ARCHIVED"
    assert ci.campaign_rows(snap)[0]["status"] == "unknown"


def test_budget_units_authority_and_unknown_currency():
    snap = snapshot()
    rows = {r["id"]: r for r in ci.campaign_rows(snap)}
    assert rows["1"]["budget"] == {"kind": "daily", "amount": 50.0}
    assert rows["2"]["budget"]["kind"] == "ad_sets"                # у кампании нет бюджета — он на ad set
    tree = ci.build_tree(snap)
    assert ci.row_payload(snap, tree, "adset", snap.adsets[0], None)["budget"]["kind"] == "inherited"
    assert ci.row_payload(snap, tree, "adset", snap.adsets[2], None)["budget"] == {"kind": "daily", "amount": 25.0}
    assert ci.row_payload(snap, tree, "ad", snap.ads[0], None)["budget"]["kind"] == "none"
    snap.currency = "JPY"
    assert ci.campaign_rows(snap)[0]["budget"]["amount"] == 5000.0           # у иены нет копеек
    snap.currency = "XYZ"
    assert ci.campaign_rows(snap)[0]["budget"] == {"kind": "unit_unknown", "amount": None}


# ───────────────────────── агрегаты ─────────────────────────

def test_meta_metrics_are_per_level_and_never_summed_up_the_tree():
    snap = snapshot()
    tree = ci.build_tree(snap)
    camp_row = ci.row_payload(snap, tree, "campaign", snap.campaigns[0], None)
    assert camp_row["meta"]["reach"] == 5000 and camp_row["meta"]["frequency"] == 4.0   # ровно то, что дал Meta
    # У ad set 12 Insights нет; у кампании 2 — тоже. Родитель НЕ берёт сумму детей и не 0.
    row2 = ci.row_payload(snap, tree, "campaign", snap.campaigns[1], None)
    assert row2["meta"] == {"state": "no_row"}
    # reach кампании не равен сумме reach ad set (4000) — оставлено как есть
    assert camp_row["meta"]["reach"] != 4000


def test_ratios_are_recomputed_from_totals_and_zero_denominators_are_none():
    snap = snapshot()
    m = ci.row_payload(snap, ci.build_tree(snap), "campaign", snap.campaigns[0], None)["meta"]
    assert m["ctr"] == pytest.approx(100.0 * 400 / 20000)
    assert m["cpm"] == pytest.approx(1000.0 * 100 / 20000)
    assert m["cpc"] == pytest.approx(100 / 400)
    assert ci.derive_ratios(10.0, 0, 0) == {"ctr": None, "cpm": None, "cpc": None}
    assert ci.derive_ratios(10.0, 0, 5)["ctr"] is None
    assert ci.derive_ratios(10.0, 100, 0)["cpc"] is None
    assert ci.derive_ratios(None, 100, 5)["cpm"] is None


def test_insights_step_states_not_read_vs_no_row():
    snap = snapshot()
    snap.steps["insights_campaign"] = "failed"
    assert ci.campaign_rows(snap)[0]["meta"] == {"state": "not_read"}
    snap.steps["insights_campaign"] = "truncated"
    assert ci.campaign_rows(snap)[1]["meta"] == {"state": "partial_unknown"}


def test_bi_metrics_recomputed_and_zero_ftd_flagged():
    m = ci.bi_metrics(100.0, 20, 4)
    assert (m["cpl"], m["cpa"], m["cr"]) == (5.0, 25.0, 20.0)
    z = ci.bi_metrics(50.0, 10, 0)
    assert z["cpa"] is None and z["zero_ftd"] is True and z["cr"] == 0.0
    none = ci.bi_metrics(0.0, 0, 0)
    assert none["cpl"] is None and none["cpa"] is None and none["cr"] is None and none["zero_ftd"] is False


def test_merge_attribution_sums_disjoint_country_slices():
    a = {"campaigns": {"X": {"spend": 10.0, "leads": 2, "deposits": 1}}, "ads": {"X": {"t": {"spend": 4.0, "leads": 1, "deposits": 0}}}}
    b = {"campaigns": {"X": {"spend": 5.0, "leads": 3, "deposits": 1}, "Y": {"spend": 1.0, "leads": 0, "deposits": 0}},
         "ads": {"X": {"t": {"spend": 1.0, "leads": 1, "deposits": 1}}}}
    m = ci.merge_attribution([a, b])
    assert m["campaigns"]["X"] == {"spend": 15.0, "leads": 5, "deposits": 2}
    assert m["ads"]["X"]["t"] == {"spend": 5.0, "leads": 2, "deposits": 1}
    assert "Y" in m["campaigns"]


# ───────────────────────── BI только по точному совпадению ─────────────────────────

BI = {
    "campaigns": {"Camp A": {"spend": 90.0, "leads": 9, "deposits": 3},
                  "Set B1": {"spend": 20.0, "leads": 4, "deposits": 1},
                  "Camp A (copy)": {"spend": 5.0, "leads": 1, "deposits": 0},      # такого в Meta нет
                  "camp b": {"spend": 7.0, "leads": 1, "deposits": 0}},             # регистр другой — не совпадение
    "ads": {"Camp A": {"Ad 1": {"spend": 30.0, "leads": 3, "deposits": 1}},
            "Set B1": {"Ad 3": {"spend": 8.0, "leads": 2, "deposits": 0}}},
}


def test_bi_join_exact_names_only():
    snap = snapshot()
    snap.adsets[0]["name"] = snap.adsets[1]["name"] = "Camp A"        # все ad set кампании 1 носят её имя
    joined, summ = ci.join_bi(snap, BI)
    assert joined["campaign:1"]["match"] == "name" and joined["campaign:1"]["spend"] == 90.0
    assert joined["campaign:1"]["cpa"] == pytest.approx(30.0)
    assert "campaign:2" not in joined or joined["campaign:2"]["match"] != "name"    # «camp b» != «Camp B»
    assert joined["adset:21"]["match"] == "name" and joined["adset:21"]["ftds"] == 1
    assert "adset:11" not in joined                                                 # два одноимённых ad set — неоднозначно
    assert "Camp A (copy)" in summ["evidence_only"] and "camp b" in summ["evidence_only"]


def test_campaign_with_differently_named_ad_sets_gets_no_name_match():
    joined, summ = ci.join_bi(snapshot(), BI)
    assert "campaign:1" not in joined                  # «Camp A» покрывает не все ad set кампании
    assert "Camp A" in summ["evidence_only"]


def test_bi_join_ads_need_a_matched_ad_set_and_unique_title():
    snap = snapshot()
    joined, _ = ci.join_bi(snap, BI)
    assert joined["ad:211"]["match"] == "name" and joined["ad:211"]["spend"] == 8.0   # под Set B1
    assert "ad:111" not in joined       # родительский ad set «Set A1» не совпал — объявление не цепляем


def test_bi_join_duplicate_names_are_ambiguous_and_get_nothing():
    snap = snapshot()
    snap.adsets, _ = ci.parse_inventory([adset(21, "Set B1", 2), adset(22, "Set B1", 2)], "adset")
    joined, summ = ci.join_bi(snap, BI)
    assert "adset:21" not in joined and "adset:22" not in joined
    assert "Set B1" in summ["evidence_only"]


def test_campaign_bi_from_ad_sets_needs_full_coverage_and_recomputes_ratios():
    snap = snapshot()
    snap.campaigns, _ = ci.parse_inventory([camp(1, "Gen name")], "campaign")
    snap.adsets, _ = ci.parse_inventory([adset(11, "S1", 1), adset(12, "S2", 1)], "adset")
    bi = {"campaigns": {"S1": {"spend": 10.0, "leads": 2, "deposits": 1}, "S2": {"spend": 30.0, "leads": 8, "deposits": 1}}, "ads": {}}
    joined, _ = ci.join_bi(snap, bi)
    c = joined["campaign:1"]
    assert c["match"] == "ad_sets" and c["coverage"] == "2/2"
    assert (c["spend"], c["leads"], c["ftds"]) == (40.0, 10, 2)
    assert (c["cpl"], c["cpa"], c["cr"]) == (4.0, 20.0, 20.0)        # из сумм, не среднее строк
    # неполное покрытие — итога нет
    bi["campaigns"].pop("S2")
    joined, _ = ci.join_bi(snap, bi)
    assert joined["campaign:1"] == {"match": "partial", "coverage": "1/2"}


def test_bi_unavailable_means_no_join_not_zeros():
    joined, summ = ci.join_bi(snapshot(), None)
    assert joined == {} and summ["available"] is False
    rows = ci.campaign_rows(snapshot(), joined)
    assert rows[0]["bi"] == {"match": "none"}


# ───────────────────────── чтение через Graph (заглушка клиента) ─────────────────────────

class FakeClient:
    """Клиент Graph по таблице ответов; считает вызовы и умеет «упасть» стопом."""

    def __init__(self, table, fail=None, truncate=()):
        self.table, self.fail, self.truncate = table, fail or {}, set(truncate)
        self.calls = 0
        self.stopped = None
        self.truncated: list[str] = []
        self.log: list[tuple] = []

    def account(self, act):
        self.calls += 1
        return {"id": act, "account_id": act[4:], "currency": "USD", "account_status": 1}

    def get_all(self, path, params=None, max_pages=5):
        self.calls += 1
        self.log.append(("GET", path, dict(params or {})))
        key = path.rsplit("/", 1)[-1]
        if key in self.fail:
            raise self.fail[key]
        if key in self.truncate:
            self.truncated.append(path)
        return self.table.get(key, [])

    def insights_range(self, act, date_from, date_to, *, level="account", breakdowns="", fields="", max_pages=1):
        self.calls += 1
        self.log.append(("GET", f"/{act}/insights", {"level": level, "fields": fields}))
        key = f"insights_{level}"
        if key in self.fail:
            raise self.fail[key]
        if key in self.truncate:
            self.truncated.append(f"/{act}/insights?level={level}")
        return self.table.get(key, [])


ACT = "act_1234567"


def _table():
    return {
        "campaigns": [camp(1, "Camp A"), camp(2, "Camp B")],
        "adsets": [adset(11, "Set A1", 1), adset(21, "Set B1", 2)],
        "ads": [ad(111, "Ad 1", 11, 1)],
        "insights_campaign": [{"campaign_id": "1", "spend": "10", "reach": "100", "impressions": "1000", "clicks": "10",
                               "frequency": "1.5", "account_currency": "USD"}],
        "insights_adset": [], "insights_ad": [],
    }


def _read(client, **kw):
    snap = ci.Snapshot(account="Acc", state="failed", date_from=D, date_to=D)
    ci.read_into(snap, client, ACT)
    ci.finalize(snap)
    return snap


def test_read_into_complete_read_is_ok_and_uses_get_only():
    client = FakeClient(_table())
    snap = _read(client)
    assert snap.state == "ok" and snap.currency == "USD"
    assert len(snap.campaigns) == 2 and len(snap.adsets) == 2 and len(snap.ads) == 1
    assert snap.insights["campaign"]["1"]["reach"] == 100
    assert {m for m, _p, _q in client.log} == {"GET"}
    fields = {p.rsplit("/", 1)[-1]: q["fields"] for _m, p, q in client.log if "fields" in q and "insights" not in p}
    for needed in ("id", "name", "status", "effective_status", "daily_budget", "lifetime_budget", "bid_strategy"):
        assert needed in fields["campaigns"].split(",")
    assert "campaign_id" in fields["adsets"].split(",") and "adset_id" in fields["ads"].split(",")
    assert client.calls <= 24                                            # потолок вызовов Read spend не превышен


def test_read_into_empty_account_is_empty_not_zero_rows_failed():
    snap = _read(FakeClient({"campaigns": []}))
    assert snap.state == "empty" and snap.campaigns == []
    assert snap.steps["adsets"] == "skipped"


def test_read_into_truncated_lists_make_a_partial_snapshot():
    snap = _read(FakeClient(_table(), truncate={"ads"}))
    assert snap.state == "partial" and snap.steps["ads"] == "truncated"
    assert any("partial" in p and "ads" in p for p in snap.problems)
    assert ci.child_rows(snap, "ad", "11")["state"] == "partial"


def test_read_into_graph_error_on_a_level_is_partial_and_distinct_from_empty():
    client = FakeClient(_table(), fail={"ads": g.GraphError("boom", code=100)})
    snap = _read(client)
    assert snap.state == "partial" and snap.steps["ads"] == "failed"
    assert ci.child_rows(snap, "ad", "11")["state"] == "not_read"
    assert snap.steps["insights_ad"] == "skipped" or snap.steps["insights_ad"] == "ok"


def test_read_into_stop_mid_read_keeps_what_was_read_and_stops_calling():
    client = FakeClient(_table(), fail={"ads": g.RateLimited("slow down", code=17)})
    snap = _read(client)
    assert snap.state == "partial" and len(snap.campaigns) == 2 and len(snap.adsets) == 2
    assert snap.steps["ads"] == "failed"
    # после стопа дальше вызовов нет: insights остались непрочитанными
    assert snap.steps["insights_campaign"] == "skipped" and snap.steps["insights_adset"] == "skipped"
    assert not any("insights" in p for _m, p, _q in client.log)


def test_read_into_campaigns_failure_raises_and_state_is_failed():
    client = FakeClient(_table(), fail={"campaigns": g.GraphError("denied", code=200)})
    snap = ci.Snapshot(account="Acc", state="failed", date_from=D, date_to=D)
    with pytest.raises(Exception):
        ci.read_into(snap, client, ACT)
    ci.finalize(snap)
    assert snap.state == "failed"


def test_read_into_other_account_is_refused():
    class Other(FakeClient):
        def account(self, act):
            return {"id": "act_999", "account_id": "999", "currency": "USD"}

    snap = ci.Snapshot(account="Acc", date_from=D, date_to=D)
    with pytest.raises(api.ApiReadError):
        ci.read_into(snap, Other(_table()), ACT)


# ───────────────────────── через настоящую сессию профиля (тот же стенд, что у ACL-1760) ─────────────────────────

def _inv_routes():
    from tests.test_acl1760_api_read import ok, routes

    return routes(**{
        "/ACT/adsets||": ok([adset(11, "Set A1", 111), adset(22, "Set B1", 222)]),
        "/ACT/campaigns||": ok([camp(111, "Camp A"), camp(222, "Camp B", status="PAUSED")]),
        "/ACT/ads||": ok([ad(1, "Ad 1", 11, 111)]),
        "/ACT/insights|adset|": ok([{"adset_id": "11", "spend": "60", "reach": "40", "impressions": "400",
                                     "clicks": "4", "account_currency": "USD"}]),
    })


def test_read_via_api_uses_the_read_spend_session_and_persists(monkeypatch, tmp_path):
    from tests.test_acl1760_api_read import ACCOUNT, World

    world = World(monkeypatch, tmp_path)
    world.page.routes = _inv_routes()
    monkeypatch.setattr(ci, "_store_path", lambda: tmp_path / "campaign_inventory.json")
    snap = ci.read_via_api(ACCOUNT, D, D)
    assert snap.state in ("ok", "partial"), snap.problems
    assert [c["id"] for c in snap.campaigns] == ["111", "222"]
    assert snap.insights["adset"]["11"]["spend"] == 60.0
    assert len(world.opened_profiles) <= 1                               # ровно одна сессия профиля
    # только GET: у страницы-заглушки нет ни одного вызова записи
    assert all((s.get("method") or "GET").upper() == "GET" for s in world.page.specs)
    # профиль был закрыт, а не поднят нами → мы его не останавливали (и наоборот) — без второго открытия
    ci.put_result(snap)
    again = ci.get_snapshot(ACCOUNT)
    assert again is not None and again.finished_at == snap.finished_at and again.campaigns == snap.campaigns


def test_read_via_api_cooldown_refuses_without_a_single_call(monkeypatch, tmp_path):
    from tests.test_acl1760_api_read import ACCOUNT, World

    world = World(monkeypatch, tmp_path)
    key = sp._profile_key(ACCOUNT)
    api._COOLDOWN[key] = (time.monotonic() + 300, "rate_limited")
    snap = ci.read_via_api(ACCOUNT, D, D)
    assert snap.state == "failed" and "Nothing was called" in snap.error
    assert world.page.specs == [] and world.opened_profiles == []


# ───────────────────────── хранилище и свежесть ─────────────────────────

def test_snapshot_survives_a_restart_with_its_read_time():
    snap = snapshot()
    ci.put_result(snap)
    got = ci.get_snapshot("Acc")
    assert got.finished_at == "2026-10-11 12:00:00" and len(got.campaigns) == 2 and got.insights["campaign"]["1"]["reach"] == 5000


def test_failed_read_keeps_the_previous_snapshot_and_records_the_attempt():
    ci.put_result(snapshot())
    ci.put_result(ci.Snapshot(account="Acc", state="failed", error="Meta rate limit", finished_at="2026-10-11 13:00:00"))
    kept = ci.get_snapshot("Acc")
    assert kept is not None and kept.finished_at == "2026-10-11 12:00:00"
    att = ci.get_attempt("Acc")
    assert att["state"] == "failed" and "rate limit" in att["error"]
    # удачное чтение стирает отметку о неудаче
    ci.put_result(snapshot(finished_at="2026-10-11 14:00:00"))
    assert ci.get_attempt("Acc") is None


def test_a_first_failed_read_leaves_no_snapshot():
    ci.put_result(ci.Snapshot(account="Acc", state="failed", error="x"))
    assert ci.get_snapshot("Acc") is None
    assert ci.summary(None, ci.get_attempt("Acc"))["state"] == "idle"


def test_corrupt_store_is_unread_not_a_crash(store):
    (store / "campaign_inventory.json").write_text("{ nope", encoding="utf-8")
    assert ci.get_snapshot("Acc") is None
    ci.put_result(snapshot())                          # и запись поверх испорченного файла проходит
    assert ci.get_snapshot("Acc") is not None


def test_summary_carries_freshness():
    snap = snapshot(finished_at="2026-10-11 12:00:00")
    now = time.mktime(time.strptime("2026-10-11 12:05:30", "%Y-%m-%d %H:%M:%S"))
    s = ci.summary(snap, None, now=now)
    assert s["age_s"] == 330 and s["counts"] == {"campaigns": 2, "adsets": 3, "ads": 3}
    assert ci.summary(snapshot(finished_at="garbage"), None)["age_s"] is None


# ───────────────────────── бэкенд и задание ─────────────────────────

def test_rpa_backend_refuses_and_starts_nothing(monkeypatch):
    monkeypatch.delenv(api.BACKEND_ENV, raising=False)
    started = []
    with pytest.raises(ci.NeedsApiBackend) as e:
        ci.start_read("Acc", D, D, reader=lambda *a: started.append(a))
    assert str(e.value) == "Campaign inventory needs the API backend"
    assert started == [] and ci.running_now() == []


def test_start_read_runs_the_reader_under_the_profile_lock_and_stores(monkeypatch):
    monkeypatch.setenv(api.BACKEND_ENV, "api")
    seen = []

    def reader(account, a, b):
        seen.append((account, a, b))
        return snapshot()

    job = ci.start_read("Acc", D, D, reader=reader)
    assert job["started"] is True
    end = time.time() + 5
    while ci.running_now() and time.time() < end:
        time.sleep(0.02)
    assert seen == [("Acc", D, D)]
    got = ci.get_snapshot("Acc")
    assert got is not None and got.finished_at and got.started_at          # время чтения проставлено заданием
    assert not ci.running_now()


def test_second_press_on_a_running_account_joins_it(monkeypatch):
    monkeypatch.setenv(api.BACKEND_ENV, "api")
    ci._RUNNING.add("Acc")
    calls = []
    assert ci.start_read("Acc", D, D, reader=lambda *a: calls.append(a)) == {"started": False, "running": True}
    assert calls == []


def test_reader_crash_is_a_failed_attempt_not_a_server_crash(monkeypatch):
    monkeypatch.setenv(api.BACKEND_ENV, "api")

    def boom(*a):
        raise RuntimeError("kaput")

    ci.start_read("Acc", D, D, reader=boom)
    end = time.time() + 5
    while ci.running_now() and time.time() < end:
        time.sleep(0.02)
    assert ci.get_snapshot("Acc") is None
    assert "kaput" in ci.get_attempt("Acc")["error"]


# ───────────────────────── маршруты ─────────────────────────

class _FormReq:
    def __init__(self, fields):
        self._fields = dict(fields)

    async def form(self, **_limits):  # read_form передаёт пределы разбора
        return self._fields


def _get_req(query: str):
    return SimpleNamespace(query_params=QueryParams(query))


def _body(resp):
    return json.loads(resp.body)


def test_read_route_refuses_on_rpa_with_409_and_the_exact_message(monkeypatch):
    from src.web import app as web

    monkeypatch.delenv(api.BACKEND_ENV, raising=False)
    resp = asyncio.run(web.campaign_inventory_read(_FormReq({"account": "Acc", "date_from": D, "date_to": D})))
    assert resp.status_code == 409
    body = _body(resp)
    assert body["ok"] is False and body["code"] == "needs_api_backend"
    assert body["error"] == "Campaign inventory needs the API backend"
    assert ci.running_now() == []


def test_read_route_requires_dates_like_read_spend(monkeypatch):
    from src.web import app as web

    monkeypatch.setenv(api.BACKEND_ENV, "api")
    resp = asyncio.run(web.campaign_inventory_read(_FormReq({"account": "Acc", "span": "all"})))
    assert resp.status_code == 400 and "date range" in _body(resp)["error"]
    resp = asyncio.run(web.campaign_inventory_read(_FormReq({"account": "", "date_from": D})))
    assert resp.status_code == 400


def test_read_route_starts_a_job_on_api(monkeypatch):
    from src.web import app as web

    monkeypatch.setenv(api.BACKEND_ENV, "api")
    monkeypatch.setattr(ci, "default_reader", lambda: (lambda account, a, b: snapshot()))
    resp = asyncio.run(web.campaign_inventory_read(_FormReq({"account": "Acc", "date_from": D, "date_to": D})))
    assert resp.status_code == 200 and _body(resp)["started"] is True
    end = time.time() + 5
    while ci.running_now() and time.time() < end:
        time.sleep(0.02)


def test_status_route_is_local_and_flags_rpa(monkeypatch):
    from src.web import app as web

    monkeypatch.delenv(api.BACKEND_ENV, raising=False)
    body = _body(asyncio.run(web.campaign_inventory_status(_get_req("account=Acc"))))
    assert body["needs_api"] is True and body["message"] == "Campaign inventory needs the API backend"
    monkeypatch.setenv(api.BACKEND_ENV, "api")
    ci.put_result(snapshot())
    body = _body(asyncio.run(web.campaign_inventory_status(_get_req("account=Acc&account=Other"))))
    assert body["needs_api"] is False and body["accounts"]["Acc"]["state"] == "ok"
    assert body["accounts"]["Other"]["state"] == "idle"
    assert "campaigns" not in body["accounts"]["Acc"]            # сводка без объектов


def test_tree_and_children_routes_serve_the_snapshot_with_joined_bi(monkeypatch):
    from src.web import app as web

    ci.put_result(snapshot())
    monkeypatch.setattr(web, "_inventory_bi", lambda account, countries, df, dt, span: (BI, ""))
    body = _body(asyncio.run(web.campaign_inventory_tree(_get_req("account=Acc&country=Kuwait"))))
    assert body["has_snapshot"] is True and [r["id"] for r in body["rows"]] == ["1", "2"]
    assert body["rows"][0]["bi"]["match"] == "none"
    assert body["bi"]["matched"] == 1 and "camp b" in body["bi"]["evidence_only"]
    kids = _body(asyncio.run(web.campaign_inventory_children(_get_req("account=Acc&level=adset&parent=2"))))
    assert kids["state"] == "ok" and [r["id"] for r in kids["rows"]] == ["21"]
    assert kids["rows"][0]["bi"]["match"] == "name"
    bad = asyncio.run(web.campaign_inventory_children(_get_req("account=Acc&level=campaign&parent=2")))
    assert bad.status_code == 400


def test_tree_route_for_an_unread_account_is_an_honest_empty(monkeypatch):
    from src.web import app as web

    body = _body(asyncio.run(web.campaign_inventory_tree(_get_req("account=Never"))))
    assert body["has_snapshot"] is False and body["rows"] == [] and body["summary"]["state"] == "idle"


def test_inventory_bi_without_countries_or_workbook_is_unavailable_not_zero(monkeypatch):
    from src.web import app as web

    data, note = web._inventory_bi("Acc", [], "", "", "")
    assert data is None and "country" in note
    monkeypatch.setattr(web, "get_cached_report", lambda: (_ for _ in ()).throw(RuntimeError("down")))
    data, note = web._inventory_bi("Acc", ["Kuwait"], D, D, "")
    assert data is None and "down" in note


# ───────────────────────── шаблон: команды выключены и ничего не пишут ─────────────────────────

SRC = TEMPLATE.read_text(encoding="utf-8")


def test_component_renders_the_scoped_markup_and_no_home_section():
    """ACL-1800 (решение владельца 12.10): отдельной секции «Campaigns» на Home нет —
    шаблон отдаёт только компонент и шаблон разметки для строки аккаунта."""
    from src.web.app import templates

    html = templates.env.get_template("_campaign_table.html").render()
    assert 'id="hc-root"' not in html and 'id="hc-title"' not in html and 'id="hc-seed"' not in html
    assert '<template id="hc-scoped-tpl">' in html
    assert "Campaign inventory needs the API backend" in html          # блок отказа есть, скрыт до ответа статуса
    assert "What was changed here" in html
    assert 'data-hc="table"' not in html and 'data-hc="frame"' not in html and ' id="hc-table"' not in html   # таблицы рисуются под строками аккаунтов, id нет


def test_command_controls_are_disabled_with_the_reason_and_carry_object_data():
    # Duplicate и Copy to… по-прежнему нарисованы выключенными (ACL-1773); причина — в title и aria-label
    assert "var REASON_COPY = 'ACL-1773';" in SRC and "Coming in ACL-1800" not in SRC           # колонки More больше нет
    assert "function dupCell(r)" in SRC
    assert "one('duplicate', 'Duplicate here'" in SRC and "one('copy', 'Copy to accounts'" in SRC
    assert 'class="hc-cmd hc-cmd-txt" disabled aria-disabled="true"' in SRC
    # Команды ACL-1800 (on/off, budget, bid): состояние решает помощник, без него — выключено с причиной
    assert "ACM.home.controlState" in SRC and "the command helper did not load" in SRC
    assert " name=" not in SRC.split("<script", 1)[0] and "onclick" not in SRC.lower()
    # ACL-1800 цепляется по data-атрибутам объекта
    assert "data-object-id" in SRC and "data-level" in SRC and "data-account" in SRC and "data-action" in SRC
    assert "needs owner approval" not in SRC


def test_template_has_no_write_paths_and_one_post():
    posts = re.findall(r"method:\s*'(\w+)'", SRC)
    assert posts == ["POST"]                                # чтение описи и «Open profile» (по кнопке)
    # Meta читает только /campaign-inventory/read; остальное — локальные маршруты профилей (ACL-1812), открытие — по кнопке
    assert re.findall(r"fetch\('([^']+)'", SRC) == ["/warm/state?", "/warm/open", "/warm/job/"]
    assert not re.search(r"\b(PUT|PATCH|DELETE)\b", SRC)
    for verb in ("setInterval",):
        assert verb not in SRC                                      # ни таймеров, ни опроса по расписанию
    assert " name=\"" not in re.sub(r"<script.*?</script>|<style.*?</style>", "", SRC, flags=re.S)


def test_template_unknown_values_use_a_dash_and_numbers_never_default_to_zero():
    assert "var DASH = '\\u2014'" in SRC or "var DASH = '—'" in SRC
    assert "|| 0" not in SRC and "? 0 :" not in SRC
    for banned in ("Number(null)", "parseInt(null"):
        assert banned not in SRC


def test_home_has_no_standalone_campaigns_section():
    """Компонент подключён на странице один раз и не рисует секцию; таблица — только под строкой аккаунта."""
    report = (ROOT / "src" / "web" / "templates" / "report.html").read_text(encoding="utf-8")
    assert report.count('{% include "_campaign_table.html" %}') == 1
    assert "_home_campaigns" not in report
    assert not (ROOT / "src" / "web" / "templates" / "_home_campaigns.html").exists()
    rows = (ROOT / "src" / "web" / "templates" / "_account_map_country.html").read_text(encoding="utf-8")
    assert "amapReadCampaigns(this)" not in rows and 'class="hc hc-scoped amap-campaigns"' in rows
    assert "hc-root" not in report and "hc-title" not in report


def test_tokens_are_scoped_to_the_section_not_root():
    css = re.search(r"<style>(.*?)</style>", SRC, re.S).group(1)
    assert ":root {" not in css and ":root{" not in css
    assert "\n  .hc {\n    --adopy-bg" in css
    assert ':root[data-theme="dark"] .hc {' in css


def test_campaign_name_match_needs_the_book_row_to_cover_the_whole_campaign():
    # Duplicate в билдере: ad set `X` и `X - Copy` под кампанией `X`. Строка книги `X` — лишь часть.
    snap = snapshot()
    snap.campaigns, _ = ci.parse_inventory([camp(1, "X")], "campaign")
    snap.adsets, _ = ci.parse_inventory([adset(11, "X", 1), adset(12, "X - Copy", 1)], "adset")
    snap.ads = []
    both = {"campaigns": {"X": {"spend": 10.0, "leads": 1, "deposits": 0},
                          "X - Copy": {"spend": 30.0, "leads": 3, "deposits": 1}}, "ads": {}}
    c = ci.join_bi(snap, both)[0]["campaign:1"]
    assert c["match"] == "ad_sets" and c["spend"] == 40.0 and c["coverage"] == "2/2"
    only_x = {"campaigns": {"X": {"spend": 10.0, "leads": 1, "deposits": 0}}, "ads": {}}
    c = ci.join_bi(snap, only_x)[0]["campaign:1"]
    assert c == {"match": "partial", "coverage": "1/2"}          # никогда не $10 «Exact name»


def test_campaign_name_match_still_works_with_no_ad_sets_or_all_same_named():
    snap = snapshot()
    snap.campaigns, _ = ci.parse_inventory([camp(1, "X")], "campaign")
    book = {"campaigns": {"X": {"spend": 10.0, "leads": 1, "deposits": 0}}, "ads": {}}
    snap.adsets, snap.ads = [], []
    assert ci.join_bi(snap, book)[0]["campaign:1"]["match"] == "name"
    snap.adsets, _ = ci.parse_inventory([adset(11, "X", 1)], "adset")
    assert ci.join_bi(snap, book)[0]["campaign:1"]["match"] == "name"


def test_status_summaries_parse_the_file_once(monkeypatch):
    ci.put_result(snapshot())
    calls = []
    real = ci._load_all
    monkeypatch.setattr(ci, "_load_all", lambda: calls.append(1) or real())
    out = ci.summaries(["Acc", "A2", "A3"])
    assert len(calls) == 1 and out["Acc"]["state"] == "ok" and out["A2"]["state"] == "idle"
