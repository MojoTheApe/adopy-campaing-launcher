"""ACL-1800: команды над кампаниями — статус, бюджет, ставка; человек подтверждает.

Ни Meta, ни Multilogin здесь не трогаются: Graph — это `FakeMeta` в памяти
(транспорт `GraphClient`), страница помощника — поддельная, а JS-линия
политики гоняется в node. Проверяется: узкая политика записи на обеих линиях
(только POST на id существующего объекта, только status/daily_budget/
bid_amount, ни DELETE, ни создания), превью → подтверждение → запись → сверка,
устаревшее превью, неизвестный результат без повторов, идемпотентность и то,
что включение дочернего объекта никогда не включает родителей.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_api import commands as cmd  # noqa: E402
from src.fb_api import graph as g  # noqa: E402
from src.fb_api import token_capture as tc  # noqa: E402
from src.fb_api import write_policy as wp  # noqa: E402

ACT = "act_1234567890"          # заглушки из acl_scan_secrets._PLACEHOLDERS
ACT_DIGITS = "1234567890"
TOKEN = "EAAB" + "Zx9" * 20
ACCOUNT = "Acc"

CAMP_A, CAMP_CBO = "1000001", "1000002"
SET_BID, SET_AUTO, SET_CBO = "2000001", "2000002", "2000003"
AD_1 = "3000001"


# ───────────────────────── политика записи (Python) ─────────────────────────

@pytest.mark.parametrize("params,field", [
    ({"status": "ACTIVE"}, "status"), ({"status": "PAUSED"}, "status"),
    ({"daily_budget": 2550}, "daily_budget"), ({"daily_budget": "2550"}, "daily_budget"),
    ({"bid_amount": 150}, "bid_amount"), ({"bid_amount": "99"}, "bid_amount"),
])
def test_object_policy_accepts_the_three_fields(params, field):
    assert wp.check_object_update("POST", f"/{CAMP_A}", params) == field


@pytest.mark.parametrize("method", ["GET", "DELETE", "PUT", "post", ""])
def test_object_policy_refuses_other_methods(method):
    with pytest.raises(wp.WriteRefused):
        wp.check_object_update(method, f"/{CAMP_A}", {"status": "PAUSED"})


@pytest.mark.parametrize("path", [
    f"/{ACT}/campaigns", f"/{ACT}/adsets", f"/{ACT}", f"/{CAMP_A}/adsets", f"/{CAMP_A}/",
    "/me/adaccounts", "/", "", "/abc", "/1234", f"/{CAMP_A}?x=1", f"//{CAMP_A}", "/01234567",
])
def test_object_policy_refuses_edges_creation_and_odd_paths(path):
    with pytest.raises(wp.WriteRefused):
        wp.check_object_update("POST", path, {"status": "PAUSED"})


@pytest.mark.parametrize("params", [
    {"name": "x"}, {"effective_status": "ACTIVE"}, {"configured_status": "ACTIVE"},
    {"lifetime_budget": 100}, {"spend_cap": 100}, {"bid_strategy": "COST_CAP"},
    {"access_token": "x"}, {"method": "DELETE"}, {"_method": "DELETE"}, {"batch": []},
    {"execution_options": ["validate_only"]}, {"targeting": {}}, {"adlabels": []},
    {}, {"status": "ACTIVE", "daily_budget": 100}, {"status": "ACTIVE", "name": "x"},
])
def test_object_policy_refuses_other_or_several_fields(params):
    with pytest.raises(wp.WriteRefused):
        wp.check_object_update("POST", f"/{CAMP_A}", params)


@pytest.mark.parametrize("status", ["ARCHIVED", "DELETED", "active", "Paused", "", None, 1, True,
                                    ["ACTIVE"], "ACTIVE "])
def test_object_policy_refuses_other_statuses(status):
    with pytest.raises(wp.WriteRefused):
        wp.check_object_update("POST", f"/{CAMP_A}", {"status": status})


@pytest.mark.parametrize("amount", [0, -1, "0", "-5", 1.5, "1.5", "1e3", "007", " 5", "5 ", "+5", True,
                                    None, "", 10 ** 15, "9" * 16, "٣", [5], {"a": 1}])
@pytest.mark.parametrize("field", ["daily_budget", "bid_amount"])
def test_object_policy_refuses_bad_amounts(field, amount):
    with pytest.raises(wp.WriteRefused):
        wp.check_object_update("POST", f"/{CAMP_A}", {field: amount})


def test_object_policy_refuses_files():
    with pytest.raises(wp.WriteRefused):
        wp.check_object_update("POST", f"/{CAMP_A}", {"status": "PAUSED"}, {"filename": b"x"})


def test_creation_policy_still_refuses_object_paths_and_active():
    """Узкая политика правок не расширила политику создания."""
    with pytest.raises(wp.WriteRefused):
        wp.check(ACT, "POST", f"/{CAMP_A}", {"status": "PAUSED"})
    with pytest.raises(wp.WriteRefused):
        wp.check(ACT, "POST", f"/{ACT}/campaigns",
                 {"name": "C", "objective": "OUTCOME_LEADS", "status": "ACTIVE"})
    assert "object_update" not in wp.policy_for_page(ACT)


def test_command_policy_for_page_has_no_creation_edges():
    pol = wp.policy_for_commands(ACT)
    assert pol["edges"] == {} and pol["files"] == {}
    assert pol["object_update"] == {"fields": ["bid_amount", "daily_budget", "status"],
                                    "statuses": ["ACTIVE", "PAUSED"]}


# ───────────────────────── Graph в памяти ─────────────────────────

class FakeMeta:
    """Транспорт `GraphClient`: GET по id и кабинету, POST только через политику правок."""

    def __init__(self, currency="USD"):
        self.currency = currency
        self.account_status = 1
        self.objs: dict[str, dict] = {}
        self.posts: list[tuple[str, dict]] = []
        self.gets: list[str] = []
        #: id → "applied" (применено, ответ потерян), "lost" (не применено, ответ потерян),
        #: "refuse" (Meta отказала), "drop" (ответила OK, сохранила иное)
        self.fail_next: dict[str, str] = {}
        self.then_mutate: dict[str, tuple[str, object]] = {}   # id → (поле, значение) после GET
        self._seed()

    def _seed(self):
        def o(oid, kind, name, **kw):
            self.objs[oid] = {"id": oid, "_kind": kind, "name": name, "account_id": ACT_DIGITS,
                              "status": "PAUSED", "effective_status": "PAUSED", **kw}

        o(CAMP_A, "campaign", "Camp A", bid_strategy="LOWEST_COST_WITHOUT_CAP")
        o(CAMP_CBO, "campaign", "Camp CBO", status="ACTIVE", effective_status="ACTIVE",
          daily_budget="5000", bid_strategy="LOWEST_COST_WITHOUT_CAP")
        o(SET_BID, "adset", "Set bid cap", campaign_id=CAMP_A, daily_budget="3000",
          bid_strategy="LOWEST_COST_WITH_BID_CAP", bid_amount="150")
        o(SET_AUTO, "adset", "Set auto", campaign_id=CAMP_A, status="ACTIVE", effective_status="CAMPAIGN_PAUSED",
          daily_budget="2000", bid_strategy="LOWEST_COST_WITHOUT_CAP")
        o(SET_CBO, "adset", "Set under CBO", campaign_id=CAMP_CBO, status="ACTIVE",
          effective_status="ACTIVE")
        o(AD_1, "ad", "Ad one", adset_id=SET_BID, campaign_id=CAMP_A)

    # -- транспорт GraphClient
    def state(self):
        return {"kind": "ads_manager", "has_token": True}

    def reacquire(self, timeout=60.0):
        return self.state()

    def request(self, method, path, params=None):
        assert method == "GET"
        self.gets.append(path)
        if path == f"/{ACT}":
            return {"status": 200, "body": {"account_id": ACT_DIGITS, "currency": self.currency,
                                            "account_status": self.account_status}}
        oid = path.strip("/")
        obj = self.objs.get(oid)
        if obj is None:
            return {"status": 400, "body": {"error": {"code": 100, "message": "Object does not exist"}}}
        row = self._row(obj, params.get("fields", ""))
        if oid in self.then_mutate:
            field, value = self.then_mutate.pop(oid)
            obj[field] = value
        return {"status": 200, "body": row}

    def _row(self, obj, fields):
        row = {k: v for k, v in obj.items() if not k.startswith("_")}
        kind = obj["_kind"]
        if kind == "campaign":
            for k in ("bid_amount",):
                row.pop(k, None)
        if kind in ("adset", "ad"):
            camp = self.objs.get(obj.get("campaign_id", ""))
            if camp:
                row["campaign"] = {k: camp.get(k) for k in
                                   ("id", "name", "status", "effective_status", "daily_budget", "bid_strategy")
                                   if camp.get(k) is not None}
        if kind == "ad":
            ads = self.objs.get(obj.get("adset_id", ""))
            if ads:
                row["adset"] = {k: ads.get(k) for k in ("id", "name", "status", "effective_status")}
        return row

    def write(self, path, params, files=None, filename=""):
        wp.check_object_update("POST", path, params, files)   # та же политика, что у страницы
        self.posts.append((path, dict(params)))
        oid = path.strip("/")
        mode = self.fail_next.pop(oid, None)
        ((field, value),) = params.items()
        if mode == "lost":
            return {"error": "call_unknown"}
        if mode == "refuse":
            return {"status": 400, "body": {"error": {"code": 100, "message": "Invalid parameter"}}}
        stored = str(value) if field != "status" else value
        if mode == "drop":
            stored = "1" if field != "status" else "PAUSED"
        self.objs[oid][field] = stored
        if mode == "applied":
            return {"error": "call_unknown"}
        return {"status": 200, "body": {"success": True}}


def make_client(meta, *, commands):
    return g.GraphClient(meta, allow_writes=commands, min_interval_s=0.0, max_calls=80,
                         sleep=lambda s: None)


class World:
    def __init__(self, tmp_path, meta=None):
        self.meta = meta or FakeMeta()
        self.store = cmd.OperationStore(tmp_path / "history.jsonl")
        self.sessions: list[tuple[str, bool]] = []
        self.clients: list[g.GraphClient] = []
        self.sleeps: list[float] = []

    @contextmanager
    def open_session(self, account, commands):
        self.sessions.append((account, commands))
        client = make_client(self.meta, commands=commands)
        self.clients.append(client)
        yield SimpleNamespace(client=client, act=ACT)

    def preview(self, command, value, targets, actor="Dana"):
        return cmd.preview(self.store, self.open_session, account=ACCOUNT, actor=actor,
                           command=command, value=value, targets=targets)

    def execute(self, op, key="key-0000001"):
        return cmd.execute(self.store, self.open_session, revision_id=op["revision_id"],
                           idempotency_key=key, spawn=lambda fn: fn(), sleep=self.sleeps.append)

    def final(self, op):
        return cmd.status(self.store, op["op_id"])


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def t(level, oid):
    return {"level": level, "id": oid}


# ───────────────────────── включить / выключить на каждом уровне ─────────────────────────

@pytest.mark.parametrize("level,oid,before,after", [
    ("campaign", CAMP_A, "PAUSED", "ACTIVE"),
    ("campaign", CAMP_CBO, "ACTIVE", "PAUSED"),
    ("adset", SET_BID, "PAUSED", "ACTIVE"),
    ("adset", SET_CBO, "ACTIVE", "PAUSED"),
    ("ad", AD_1, "PAUSED", "ACTIVE"),
])
def test_switch_on_off_at_each_level(world, level, oid, before, after):
    pv = world.preview("set_status", after, [t(level, oid)])
    assert pv["state"] == "Preview" and pv["revision_id"].startswith("rev_")
    (row,) = pv["affected"]
    assert (row["id"], row["field"], row["before"], row["after"]) == (oid, "status", before, after)
    assert pv["currency"] == "USD" and pv["skipped"] == []
    assert world.meta.posts == []                               # превью ничего не пишет
    assert world.sessions == [(ACCOUNT, False)]                 # и сессия — без права записи
    started = world.execute(pv)
    assert started["state"] == "Running"
    done = world.final(pv)
    assert done["state"] == "Succeeded", done
    assert world.meta.posts == [(f"/{oid}", {"status": after})]
    assert world.meta.objs[oid]["status"] == after
    assert done["affected"][0]["result"] == "saved" and done["affected"][0]["saved"] == after


def test_already_in_state_is_skipped_with_reason(world):
    pv = world.preview("set_status", "PAUSED", [t("campaign", CAMP_A), t("campaign", CAMP_CBO)])
    assert [a["id"] for a in pv["affected"]] == [CAMP_CBO]
    assert pv["skipped"][0]["reason"] == "Already Off"


def test_archived_object_is_skipped(world):
    world.meta.objs[CAMP_A]["status"] = "ARCHIVED"
    pv = world.preview("set_status", "ACTIVE", [t("campaign", CAMP_A)])
    assert pv["affected"] == [] and "archived" in pv["skipped"][0]["reason"]
    with pytest.raises(cmd.CommandRefused):
        world.execute(pv)


# ───────────────────────── родители никогда не включаются сами ─────────────────────────

def test_enabling_a_child_never_enables_its_parents(world):
    before = json.dumps(world.meta.objs, sort_keys=True)
    pv = world.preview("set_status", "ACTIVE", [t("ad", AD_1)])
    (row,) = pv["affected"]
    texts = [c["text"] for c in row["constraints"]]
    assert texts == ["On · Ad set paused", "On · Campaign paused"]
    world.execute(pv)
    assert [p for p, _ in world.meta.posts] == [f"/{AD_1}"]      # только сам объект
    assert world.meta.objs[SET_BID]["status"] == "PAUSED" and world.meta.objs[CAMP_A]["status"] == "PAUSED"
    assert world.final(pv)["state"] == "Succeeded"
    assert before != json.dumps(world.meta.objs, sort_keys=True)  # менялся именно объявление


def test_enabling_an_adset_shows_campaign_paused_and_leaves_campaign(world):
    pv = world.preview("set_status", "ACTIVE", [t("adset", SET_BID)])
    assert [c["text"] for c in pv["affected"][0]["constraints"]] == ["On · Campaign paused"]
    world.execute(pv)
    assert world.meta.objs[CAMP_A]["status"] == "PAUSED"
    assert all(p != f"/{CAMP_A}" for p, _ in world.meta.posts)


def test_switching_off_has_no_parent_constraints(world):
    pv = world.preview("set_status", "PAUSED", [t("adset", SET_CBO)])
    assert pv["affected"][0]["constraints"] == []


# ───────────────────────── бюджет ─────────────────────────

def test_budget_change_on_the_owner_of_the_budget(world):
    pv = world.preview("set_budget", "25.50", [t("adset", SET_BID), t("campaign", CAMP_CBO)])
    rows = {a["id"]: a for a in pv["affected"]}
    assert rows[SET_BID]["before"] == 3000 and rows[SET_BID]["after"] == 2550
    assert rows[SET_BID]["before_display"] == "30.00 USD" and rows[SET_BID]["after_display"] == "25.50 USD"
    world.execute(pv)
    done = world.final(pv)
    assert done["state"] == "Succeeded"
    assert sorted(world.meta.posts) == sorted([(f"/{SET_BID}", {"daily_budget": "2550"}),
                                               (f"/{CAMP_CBO}", {"daily_budget": "2550"})])
    assert world.meta.objs[SET_BID]["daily_budget"] == "2550"


def test_budget_skips_objects_that_do_not_own_one(world):
    pv = world.preview("set_budget", "10", [t("ad", AD_1), t("adset", SET_CBO), t("campaign", CAMP_A),
                                            t("adset", SET_BID)])
    assert [a["id"] for a in pv["affected"]] == [SET_BID]
    reasons = {s["id"]: s["reason"] for s in pv["skipped"]}
    assert reasons[AD_1] == "Ads have no budget"
    assert reasons[SET_CBO] == "Budget is set on the campaign"
    assert reasons[CAMP_A] == "No daily budget on this object"


def test_budget_lifetime_is_not_edited(world):
    world.meta.objs[SET_BID]["lifetime_budget"] = "90000"
    pv = world.preview("set_budget", "10", [t("adset", SET_BID)])
    assert pv["affected"] == [] and "Lifetime" in pv["skipped"][0]["reason"]


@pytest.mark.parametrize("value", ["0", "0.00", "-5", "abc", "", "1.234", "1e3", "10,5", "  "])
def test_budget_bad_amounts_are_refused(world, value):
    with pytest.raises(cmd.CommandRefused):
        world.preview("set_budget", value, [t("adset", SET_BID)])


def test_zero_decimal_currency_uses_whole_units(tmp_path):
    w = World(tmp_path, FakeMeta(currency="JPY"))
    pv = w.preview("set_budget", "1500", [t("adset", SET_BID)])
    assert pv["affected"][0]["after"] == 1500 and pv["affected"][0]["after_display"] == "1,500 JPY"
    with pytest.raises(cmd.CommandRefused):
        w.preview("set_budget", "15.5", [t("adset", SET_BID)])
    w.execute(pv)
    assert w.meta.posts == [(f"/{SET_BID}", {"daily_budget": "1500"})]


# ───────────────────────── ставка ─────────────────────────

def test_bid_change_on_a_supported_strategy(world):
    pv = world.preview("set_bid", "2.40", [t("adset", SET_BID)])
    (row,) = pv["affected"]
    assert row["before"] == 150 and row["after"] == 240 and row["strategy"] == "Bid cap"
    world.execute(pv)
    assert world.final(pv)["state"] == "Succeeded"
    assert world.meta.posts == [(f"/{SET_BID}", {"bid_amount": "240"})]


def test_cost_cap_is_supported_too(world):
    world.meta.objs[SET_BID]["bid_strategy"] = "COST_CAP"
    pv = world.preview("set_bid", "3", [t("adset", SET_BID)])
    assert pv["affected"][0]["strategy"] == "Cost cap"


def test_bid_on_automatic_bidding_is_refused(world):
    pv = world.preview("set_bid", "2", [t("adset", SET_AUTO), t("adset", SET_CBO)])
    assert pv["affected"] == []
    assert {s["reason"] for s in pv["skipped"]} == {"Automatic · no manual bid"}
    with pytest.raises(cmd.CommandRefused):
        world.execute(pv)
    assert world.meta.posts == []


def test_bid_inherits_the_campaign_strategy_when_the_adset_has_none(world):
    world.meta.objs[SET_CBO]["bid_strategy"] = ""
    world.meta.objs[CAMP_CBO]["bid_strategy"] = "LOWEST_COST_WITH_BID_CAP"
    world.meta.objs[SET_CBO]["bid_amount"] = "100"
    pv = world.preview("set_bid", "1.50", [t("adset", SET_CBO)])
    assert pv["affected"][0]["after"] == 150


def test_bid_other_strategies_and_levels_are_skipped(world):
    world.meta.objs[SET_BID]["bid_strategy"] = "LOWEST_COST_WITH_MIN_ROAS"
    pv = world.preview("set_bid", "2", [t("adset", SET_BID), t("campaign", CAMP_A), t("ad", AD_1)])
    assert pv["affected"] == []
    assert "no manual bid" in pv["skipped"][0]["reason"]
    assert {s["reason"] for s in pv["skipped"][1:]} == {"A manual bid exists on ad sets only"}


# ───────────────────────── устаревшее превью ─────────────────────────

def test_stale_preview_writes_nothing(world):
    pv = world.preview("set_budget", "20", [t("adset", SET_BID), t("campaign", CAMP_CBO)])
    world.meta.objs[CAMP_CBO]["daily_budget"] = "7000"          # кто-то поменял в Ads Manager
    world.execute(pv)
    done = world.final(pv)
    assert done["state"] == "Failed" and done["stale"] is True
    assert "Nothing was written" in done["message"]
    assert world.meta.posts == []                                # ни одной записи, даже по неизменённому
    assert world.meta.objs[SET_BID]["daily_budget"] == "3000"
    with pytest.raises(cmd.CommandRefused):                      # превью сгорело: нужен новый
        world.execute(pv, key="key-0000002")


def test_stale_when_the_status_changed(world):
    pv = world.preview("set_status", "ACTIVE", [t("campaign", CAMP_A)])
    world.meta.objs[CAMP_A]["status"] = "ACTIVE"
    world.execute(pv)
    assert world.final(pv)["state"] == "Failed" and world.final(pv)["stale"] is True
    assert world.meta.posts == []


def test_stale_when_a_parent_changed(world):
    pv = world.preview("set_status", "ACTIVE", [t("adset", SET_BID)])
    world.meta.objs[CAMP_A]["status"] = "ACTIVE"                 # ограничение «Campaign paused» ушло
    world.execute(pv)
    assert world.final(pv)["stale"] is True and world.meta.posts == []


def test_stale_when_the_bid_strategy_became_automatic(world):
    pv = world.preview("set_bid", "2", [t("adset", SET_BID)])
    world.meta.objs[SET_BID]["bid_strategy"] = "LOWEST_COST_WITHOUT_CAP"
    world.execute(pv)
    assert world.final(pv)["stale"] is True and world.meta.posts == []


def test_stale_when_the_currency_changed(world):
    pv = world.preview("set_budget", "20", [t("adset", SET_BID)])
    world.meta.currency = "EUR"
    world.execute(pv)
    assert world.final(pv)["state"] == "Failed" and world.meta.posts == []


def test_expired_preview_is_refused(tmp_path):
    clock = [1000.0]
    w = World(tmp_path)
    w.store = cmd.OperationStore(tmp_path / "h.jsonl", clock=lambda: clock[0])
    pv = w.preview("set_status", "ACTIVE", [t("campaign", CAMP_A)])
    clock[0] += cmd.PREVIEW_TTL_S + 1
    with pytest.raises(cmd.CommandRefused) as ei:
        w.execute(pv)
    assert ei.value.code == "expired" and w.meta.posts == []


# ───────────────────────── неизвестный результат ─────────────────────────

def test_unknown_outcome_applied_is_reconciled_by_readback(world):
    world.meta.fail_next[CAMP_A] = "applied"
    pv = world.preview("set_status", "ACTIVE", [t("campaign", CAMP_A)])
    world.execute(pv)
    done = world.final(pv)
    assert done["state"] == "Succeeded"
    assert done["affected"][0]["note"] == "Confirmed by readback after a timeout"
    assert len(world.meta.posts) == 1                            # ни одного повтора
    assert world.clients[-1].unreconciled is None                # флаг сверки снят
    assert world.sleeps == [cmd.RECONCILE_PAUSE_S]


def test_unknown_outcome_not_applied_stays_unknown_and_is_not_retried(world):
    world.meta.fail_next[CAMP_A] = "lost"
    pv = world.preview("set_status", "ACTIVE", [t("campaign", CAMP_A), t("adset", SET_BID)])
    world.execute(pv)
    done = world.final(pv)
    assert done["state"] == "Outcome unknown"
    assert len(world.meta.posts) == 1                            # второй объект не пробовали
    assert [a["result"] for a in done["affected"]] == ["unknown", "not_attempted"]
    assert "not retried" in done["affected"][0]["note"].lower()
    assert world.clients[-1].unreconciled == f"/{CAMP_A}"
    assert world.meta.objs[CAMP_A]["status"] == "PAUSED"


def test_meta_refusal_keeps_the_previous_state(world):
    world.meta.fail_next[SET_BID] = "refuse"
    pv = world.preview("set_budget", "20", [t("adset", SET_BID), t("campaign", CAMP_CBO)])
    world.execute(pv)
    done = world.final(pv)
    assert done["state"] == "Failed"
    assert [a["result"] for a in done["affected"]] == ["refused", "not_attempted"]
    assert world.meta.objs[SET_BID]["daily_budget"] == "3000" and len(world.meta.posts) == 1


def test_readback_mismatch_is_failed_not_succeeded(world):
    world.meta.fail_next[SET_BID] = "drop"
    pv = world.preview("set_budget", "20", [t("adset", SET_BID)])
    world.execute(pv)
    done = world.final(pv)
    assert done["state"] == "Failed" and done["affected"][0]["result"] == "mismatch"
    assert done["affected"][0]["saved"] == 1


def test_a_meta_block_before_writing_fails_cleanly(world):
    pv = world.preview("set_status", "ACTIVE", [t("adset", SET_BID)])
    world.meta.request = lambda method, path, params=None: {"status": 400, "body": {"error": {
        "code": 368, "message": "blocked"}}}                     # правила Meta: стоп, без повторов
    world.execute(pv)
    done = world.final(pv)
    assert done["state"] == "Failed" and "Nothing was written" in done["message"]
    assert world.meta.posts == [] and world.clients[-1].stopped.kind == "policy_block"


# ───────────────────────── идемпотентность ─────────────────────────

def test_same_key_never_writes_twice(world):
    pv = world.preview("set_status", "ACTIVE", [t("campaign", CAMP_A)])
    first = world.execute(pv, key="idem-key-0001")
    again = world.execute(pv, key="idem-key-0001")
    assert first["op_id"] == again["op_id"]
    assert len(world.meta.posts) == 1
    assert again["state"] == "Succeeded" and "idempotency_key" not in again


def test_a_new_key_for_an_executed_preview_is_refused(world):
    pv = world.preview("set_status", "ACTIVE", [t("campaign", CAMP_A)])
    world.execute(pv, key="idem-key-0001")
    with pytest.raises(cmd.CommandRefused) as ei:
        world.execute(pv, key="idem-key-0002")
    assert ei.value.status == 409 and len(world.meta.posts) == 1


def test_same_key_for_a_different_preview_is_refused(world):
    pv1 = world.preview("set_status", "ACTIVE", [t("campaign", CAMP_A)])
    pv2 = world.preview("set_status", "ACTIVE", [t("adset", SET_BID)])
    world.execute(pv1, key="idem-key-0001")
    with pytest.raises(cmd.CommandRefused):
        world.execute(pv2, key="idem-key-0001")
    assert len(world.meta.posts) == 1


@pytest.mark.parametrize("key", ["", "short", "has space in it", "x" * 81, "ключ-ключ-ключ", None])
def test_bad_idempotency_keys_are_refused(world, key):
    pv = world.preview("set_status", "ACTIVE", [t("campaign", CAMP_A)])
    with pytest.raises(cmd.CommandRefused):
        cmd.execute(world.store, world.open_session, revision_id=pv["revision_id"],
                    idempotency_key=key, spawn=lambda fn: fn())
    assert world.meta.posts == []


def test_unknown_revision_is_refused(world):
    with pytest.raises(cmd.CommandRefused) as ei:
        cmd.execute(world.store, world.open_session, revision_id="rev_nope",
                    idempotency_key="idem-key-0001", spawn=lambda fn: fn())
    assert ei.value.status == 404


# ───────────────────────── другие поля и команды ─────────────────────────

@pytest.mark.parametrize("command", ["set_name", "delete", "set_lifetime_budget", "", "set_status ",
                                     "set_effective_status", "archive"])
def test_other_commands_are_refused(world, command):
    with pytest.raises(cmd.CommandRefused):
        world.preview(command, "ACTIVE", [t("campaign", CAMP_A)])
    assert world.sessions == []                                   # профиль даже не открывали


@pytest.mark.parametrize("value", ["ARCHIVED", "DELETED", "active", "", None])
def test_status_values_other_than_active_paused_are_refused(world, value):
    with pytest.raises(cmd.CommandRefused):
        world.preview("set_status", value, [t("campaign", CAMP_A)])


@pytest.mark.parametrize("targets", [None, [], "x", [{"level": "account", "id": "1000001"}],
                                     [{"level": "campaign", "id": "12"}],
                                     [{"level": "campaign", "id": "../1000001"}],
                                     [{"level": "campaign", "id": f"{ACT}"}], ["1000001"],
                                     [t("campaign", str(1000001 + i)) for i in range(cmd.MAX_TARGETS + 1)]])
def test_bad_targets_are_refused(world, targets):
    with pytest.raises(cmd.CommandRefused):
        world.preview("set_status", "ACTIVE", targets)


def test_client_that_cannot_write_cannot_execute(world):
    pv = world.preview("set_status", "ACTIVE", [t("campaign", CAMP_A)])

    @contextmanager
    def readonly(account, commands):
        yield SimpleNamespace(client=make_client(world.meta, commands=False), act=ACT)

    cmd.execute(world.store, readonly, revision_id=pv["revision_id"], idempotency_key="idem-key-0001",
                spawn=lambda fn: fn())
    assert world.final(pv)["state"] == "Failed" and world.meta.posts == []


def test_object_of_another_account_is_refused(world):
    world.meta.objs[CAMP_A]["account_id"] = "1234567890123456"
    with pytest.raises(cmd.CommandRefused):
        world.preview("set_status", "ACTIVE", [t("campaign", CAMP_A)])
    assert world.meta.posts == []


def test_unreadable_object_is_skipped_with_a_reason(world):
    pv = world.preview("set_status", "ACTIVE", [t("campaign", "1999999"), t("campaign", CAMP_A)])
    assert [a["id"] for a in pv["affected"]] == [CAMP_A]
    assert "Could not read" in pv["skipped"][0]["reason"]


def test_the_transport_policy_refuses_anything_else_even_if_the_engine_asked(world):
    """Вторая линия: даже прямой вызов клиента с чужим полем/путём не доходит до Meta."""
    client = make_client(world.meta, commands=True)
    for path, params in [(f"/{CAMP_A}", {"name": "x"}), (f"/{ACT}/campaigns", {"status": "PAUSED"}),
                         (f"/{CAMP_A}", {"status": "ARCHIVED"}), (f"/{CAMP_A}", {"daily_budget": 0})]:
        with pytest.raises(g.GraphStop):
            make_client(world.meta, commands=True).post(path, params)
    assert world.meta.posts == [] and client.writes == 0


def test_call_budget_is_respected(world):
    pv = world.preview("set_status", "ACTIVE", [t("campaign", CAMP_A), t("adset", SET_BID)])
    world.execute(pv)
    client = world.clients[-1]
    assert client.max_calls == cmd.call_budget(2) and client.calls <= client.max_calls


# ───────────────────────── ревью #802 ─────────────────────────

def test_stale_when_the_bid_strategy_changed_between_cap_types(world):
    pv = world.preview("set_bid", "2", [t("adset", SET_BID)])
    assert pv["affected"][0]["strategy"] == "Bid cap"
    world.meta.objs[SET_BID]["bid_strategy"] = "COST_CAP"       # bid_amount теперь значит другое
    world.execute(pv)
    done = world.final(pv)
    assert done["state"] == "Failed" and done["stale"] is True
    assert world.meta.posts == []


def test_execute_rechecks_the_account_status_for_switching_on(world):
    pv = world.preview("set_status", "ACTIVE", [t("campaign", CAMP_A)])
    world.meta.account_status = 2                                # кабинет отключили после превью
    world.execute(pv)
    done = world.final(pv)
    assert done["state"] == "Failed" and done["stale"] is True and "no longer active" in done["message"]
    assert world.meta.posts == []


def test_execute_allows_switching_off_on_an_inactive_account(world):
    pv = world.preview("set_status", "PAUSED", [t("campaign", CAMP_CBO)])
    world.meta.account_status = 2
    world.execute(pv)
    assert world.final(pv)["state"] == "Succeeded"


def test_preview_refuses_switching_on_for_an_inactive_account(world):
    world.meta.account_status = 2
    with pytest.raises(cmd.CommandRefused):
        world.preview("set_status", "ACTIVE", [t("campaign", CAMP_A)])


def test_trailing_newline_in_paths_is_refused():
    for path in ("/123456\n", "/123456\n\n", "/1000001\n"):
        with pytest.raises(wp.WriteRefused):
            wp.check_object_update("POST", path, {"status": "PAUSED"})
    with pytest.raises(wp.WriteRefused):                         # и политика создания
        wp.check(ACT, "POST", f"/{ACT}/campaigns\n", {"name": "C", "objective": "OUTCOME_LEADS",
                                                      "status": "PAUSED"})


def test_session_budget_is_capped_by_the_operator_ceiling(world, monkeypatch):
    monkeypatch.setenv("ACL_FB_API_MAX_CALLS", "10")
    assert cmd.call_ceiling() == 10 and cmd.call_budget(20) == 10
    assert cmd.max_targets_for_ceiling() == 3
    targets = [t("campaign", CAMP_A), t("campaign", CAMP_CBO), t("adset", SET_BID), t("adset", SET_AUTO)]
    with pytest.raises(cmd.CommandRefused) as ei:
        world.preview("set_status", "ACTIVE", targets)
    assert "At most 3 objects" in str(ei.value) and world.sessions == []
    pv = world.preview("set_status", "ACTIVE", targets[:3])
    world.execute(pv)
    assert world.clients[-1].max_calls == 10 and world.final(pv)["state"] == "Succeeded"
    monkeypatch.setenv("ACL_FB_API_MAX_CALLS", "999")
    assert cmd.call_ceiling() == 60
    monkeypatch.setenv("ACL_FB_API_MAX_CALLS", "junk")
    assert cmd.call_ceiling() == 24


def test_get_store_is_created_once_under_concurrency(monkeypatch, tmp_path):
    import threading
    import time as _t

    monkeypatch.setenv("ACL_CONFIG_DIR", str(tmp_path))
    cmd._STORES.clear()
    built = []
    real = cmd.OperationStore

    class Slow(real):
        def __init__(self, *a, **k):
            built.append(1)
            _t.sleep(0.05)
            super().__init__(*a, **k)

    monkeypatch.setattr(cmd, "OperationStore", Slow)
    got = []
    threads = [threading.Thread(target=lambda: got.append(cmd.get_store())) for _ in range(6)]
    [th.start() for th in threads]
    [th.join() for th in threads]
    assert len(built) == 1 and len({id(x) for x in got}) == 1
    cmd._STORES.clear()


def test_session_for_another_account_is_refused_before_any_write(world):
    pv = world.preview("set_status", "ACTIVE", [t("campaign", CAMP_A)])

    @contextmanager
    def other(account, commands):
        yield SimpleNamespace(client=make_client(world.meta, commands=True), act="act_1234567890123456")

    cmd.execute(world.store, other, revision_id=pv["revision_id"], idempotency_key="idem-key-0001",
                spawn=lambda fn: fn())
    done = world.final(pv)
    assert done["state"] == "Failed" and done["stale"] is True and world.meta.posts == []


def test_history_append_is_fsynced(world, monkeypatch):
    import os as _os

    synced = []
    real = _os.fsync
    monkeypatch.setattr(cmd.os, "fsync", lambda fd: synced.append(fd) or real(fd))
    pv = world.preview("set_status", "ACTIVE", [t("campaign", CAMP_A)])
    world.execute(pv)
    assert len(synced) >= 3                                       # Running, sending, Verifying, итог


def test_restart_without_a_sent_write_is_failed_not_unknown(tmp_path):
    path = tmp_path / "h.jsonl"
    store = cmd.OperationStore(path)
    op = {"op_id": "op_a", "state": cmd.RUNNING, "created_at": "x", "revision_id": "rev_a",
          "idempotency_key": "idem-key-0001",
          "affected": [{"id": "1", "result": "pending"}, {"id": "2", "result": "pending"}]}
    store.add(op)
    store.save(op)
    got = cmd.OperationStore(path).get("op_a")
    assert got["state"] == cmd.FAILED and "Nothing was written" in got["message"]
    assert [a["result"] for a in got["affected"]] == ["not_sent", "not_sent"]
    # а запись «в полёте» (sending) могла уйти
    op2 = {"op_id": "op_b", "state": cmd.RUNNING, "created_at": "y", "revision_id": "rev_b",
           "idempotency_key": "idem-key-0002",
           "affected": [{"id": "1", "result": "sending"}, {"id": "2", "result": "pending"}]}
    store.add(op2)
    store.save(op2)
    got = cmd.OperationStore(path).get("op_b")
    assert got["state"] == cmd.UNKNOWN
    assert [a["result"] for a in got["affected"]] == ["unknown", "not_attempted"]


@pytest.mark.parametrize("host,allowed", [("127.0.0.1", True), ("localhost", True), ("::1", True),
                                          ("[::1]", True), ("0.0.0.0", False), ("192.168.1.5", False),
                                          ("", False), ("example.com", False)])
def test_endpoints_refuse_when_not_bound_to_loopback(web, monkeypatch, host, allowed):
    cc, w = web
    monkeypatch.setenv("ACL_FB_BACKEND", "api")
    monkeypatch.setenv("ACL_BIND_HOST", host)
    body = {"account": ACCOUNT, "command": "set_status", "value": "ACTIVE",
            "targets": [t("campaign", CAMP_A)], "revision_id": "rev_x", "idempotency_key": "idem-key-0001"}
    results = (cc.run_preview(body), cc.run_execute(body), cc.get_status("op_x"))
    if allowed:
        assert all(code != 403 for _d, code in results)
    else:
        for data, code in results:
            assert code == 403 and data["code"] == "not_loopback" and "127.0.0.1" in data["error"]
        assert w.sessions == [] and w.meta.posts == []


def test_serve_records_its_bind_host():
    text = (ROOT / "serve.py").read_text(encoding="utf-8")
    assert 'os.environ["ACL_BIND_HOST"] = args.host' in text
    assert text.index('os.environ["ACL_BIND_HOST"]') < text.index("uvicorn.run(")


# ───────────────────────── история ─────────────────────────

def test_history_is_persisted_with_actor_and_values(world, tmp_path):
    pv = world.preview("set_budget", "20", [t("adset", SET_BID)], actor="Dana K")
    world.execute(pv)
    text = (tmp_path / "history.jsonl").read_text(encoding="utf-8")
    rows = [json.loads(line) for line in text.splitlines()]
    last = rows[-1]
    assert last["state"] == "Succeeded" and last["actor"] == "Dana K" and last["created_at"]
    item = last["affected"][0]
    assert (item["id"], item["before"], item["requested"], item["saved"], item["result"]) == (
        SET_BID, 3000, 2000, 2000, "saved")
    assert "Preview" not in {r["state"] for r in rows}            # превью в журнал не пишется
    assert TOKEN not in text and "access_token" not in text
    assert [h["op_id"] for h in world.store.history()] == [pv["op_id"]]


def test_history_survives_restart_and_marks_interrupted_runs(tmp_path):
    path = tmp_path / "h.jsonl"
    store = cmd.OperationStore(path)
    op = {"op_id": "op_x", "state": cmd.RUNNING, "created_at": "2026-10-11T00:00:00Z",
          "idempotency_key": "idem-key-0001", "revision_id": "rev_x",
          "affected": [{"id": "1", "result": "sent"}, {"id": "2", "result": "pending"}]}
    store.add(op)
    store.save(op)
    again = cmd.OperationStore(path)
    got = again.get("op_x")
    assert got["state"] == cmd.UNKNOWN and "stopped before the result was verified" in got["message"]
    assert [a["result"] for a in got["affected"]] == ["unknown", "not_attempted"]
    assert again.by_key("idem-key-0001")["op_id"] == "op_x"       # тот же ключ после рестарта — не пишет


def test_actor_is_only_a_label():
    assert cmd.clean_actor("  Dana\n\tK  ") == "Dana K"
    assert cmd.clean_actor("") == "unlabelled" and cmd.clean_actor(None) == "unlabelled"
    assert len(cmd.clean_actor("x" * 500)) == 80


# ───────────────────────── web-слой: бэкенд rpa и коды ответов ─────────────────────────

@pytest.fixture
def web(monkeypatch, tmp_path):
    from src.web import campaign_commands as cc

    monkeypatch.setenv("ACL_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("ACL_BIND_HOST", "127.0.0.1")
    cmd._STORES.clear()
    w = World(tmp_path)
    w.store = cmd.get_store()
    monkeypatch.setattr(cc, "_sessions", lambda: w.open_session)
    monkeypatch.setattr(cmd, "_spawn_thread", lambda fn: fn())
    yield cc, w
    cmd._STORES.clear()


@pytest.mark.parametrize("backend", [None, "rpa", "", "graph"])
def test_rpa_backend_refuses_every_endpoint(web, monkeypatch, backend):
    cc, w = web
    if backend is None:
        monkeypatch.delenv("ACL_FB_BACKEND", raising=False)
    else:
        monkeypatch.setenv("ACL_FB_BACKEND", backend)
    body = {"account": ACCOUNT, "command": "set_status", "value": "ACTIVE",
            "targets": [t("campaign", CAMP_A)], "revision_id": "rev_x", "idempotency_key": "idem-key-0001"}
    for data, code in (cc.run_preview(body), cc.run_execute(body), cc.get_status("op_x")):
        assert code == 409 and data["code"] == "rpa_backend"
        assert "ACL_FB_BACKEND=api" in data["error"] and "nothing was changed" in data["error"]
    assert w.sessions == [] and w.meta.posts == []


def test_api_backend_preview_execute_status_flow(web, monkeypatch):
    cc, w = web
    monkeypatch.setenv("ACL_FB_BACKEND", "api")
    body = {"account": ACCOUNT, "actor": "Dana", "command": "set_status", "value": "ACTIVE",
            "targets": [t("campaign", CAMP_A)]}
    pv, code = cc.run_preview(body)
    assert code == 200 and pv["state"] == "Preview"
    out, code = cc.run_execute({"revision_id": pv["revision_id"], "idempotency_key": "idem-key-0001"})
    assert code in (200, 202)
    got, code = cc.get_status(pv["op_id"])
    assert code == 200 and got["state"] == "Succeeded" and "_expires_ts" not in got
    assert cc.get_status("op_missing")[1] == 404
    bad, code = cc.run_preview({**body, "command": "set_name"})
    assert code == 400 and "Only" not in bad["error"]
    again, code = cc.run_execute({"revision_id": pv["revision_id"], "idempotency_key": "idem-key-0002"})
    assert code == 409


class _JsonReq:
    """Запрос с JSON — эндпоинты зовутся напрямую, без TestClient (он несовместим с httpx)."""

    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


def test_routes_are_registered_and_refuse_under_rpa(monkeypatch):
    import asyncio

    from src.web import app as web

    monkeypatch.delenv("ACL_FB_BACKEND", raising=False)
    paths = {(r.path, tuple(sorted(r.methods))) for r in web.app.routes if hasattr(r, "methods")}
    assert ("/campaign-commands/preview", ("POST",)) in paths
    assert ("/campaign-commands/execute", ("POST",)) in paths
    assert ("/campaign-commands/{op_id}", ("GET",)) in paths
    for resp in (asyncio.run(web.campaign_commands_preview(_JsonReq({"account": ACCOUNT}))),
                 asyncio.run(web.campaign_commands_execute(_JsonReq({}))),
                 asyncio.run(web.campaign_commands_status("op_x"))):
        body = json.loads(resp.body)
        assert resp.status_code == 409 and "ACL_FB_BACKEND=api" in body["error"]
    assert (ROOT / "src" / "web" / "static" / "acm_commands.js").is_file()


# ───────────────────────── сессия Graph: охлаждение, жизнь профиля, режим записи ─────────────────────────

def test_command_session_is_object_update_only_and_respects_cooldown(monkeypatch, tmp_path):
    from src.analysis_agent import account_spend_api as api
    from tests.test_acl1760_api_read import ACCOUNT as ACC
    from tests.test_acl1760_api_read import World as ReadWorld

    rw = ReadWorld(monkeypatch, tmp_path)
    real = tc.InPageGraph
    seen: list[dict] = []

    def make(tab, commands_act=None):
        kw = {"allow_object_updates": True, "write_act": commands_act} if commands_act else {}
        seen.append(kw)
        return real(tab, sleep=lambda s: None, **kw)

    monkeypatch.setattr(api, "_make_graph", make)
    with api._session(ACC, commands=True) as sess:
        assert sess.client.allow_writes is True
        assert seen == [{"allow_object_updates": True, "write_act": ACT}]
        with pytest.raises(g.GraphStop):                         # путь создания закрыт до страницы
            sess.client.post(f"/{ACT}/campaigns", {"name": "x", "status": "PAUSED"})
        assert not [s for s in rw.page.specs if s.get("method") == "POST"]
    assert rw.soft_stops == []                                    # чужой/живой профиль не останавливали
    with api._session(ACC) as sess:                               # чтение — по-прежнему без записи
        assert sess.client.allow_writes is False
    api._COOLDOWN[api._as._profile_key(ACC)] = (10 ** 12, "checkpoint")
    rw.page.specs.clear()
    with pytest.raises(api.ApiReadError):
        with api._session(ACC, commands=True):
            pass
    assert rw.page.specs == []                                    # во время охлаждения — ни одного вызова


def test_in_page_graph_modes_are_separate():
    with pytest.raises(ValueError):
        tc.InPageGraph(object(), allow_writes=True, allow_object_updates=True, write_act=ACT)
    with pytest.raises(ValueError):
        tc.InPageGraph(object(), allow_object_updates=True)       # нужен проверенный кабинет
    page = tc.InPageGraph(object(), allow_object_updates=True, write_act=ACT)
    with pytest.raises(wp.WriteRefused):
        page.write(f"/{ACT}/campaigns", {"name": "x", "status": "PAUSED"})
    with pytest.raises(ValueError):
        page.stage_file(b"x")
    creator = tc.InPageGraph(object(), allow_writes=True, write_act=ACT)
    with pytest.raises(wp.WriteRefused):
        creator.write(f"/{CAMP_A}", {"status": "ACTIVE"})        # создатель правок не делает


# ───────────────────────── помощник страницы (node) ─────────────────────────

_NODE = r"""
const TOKENV = __TOKEN__;
const calls = [];
const fakeFetch = async (url, init) => {
  calls.push({ url: String(url), method: init.method,
               body: init.body instanceof FormData ? 'formdata' : String(init.body || '') });
  return { status: 200, text: async () => JSON.stringify({ success: true }), headers: { get: () => null } };
};
const loc = { href: 'https://adsmanager.facebook.com/adsmanager/manage/campaigns?act=1',
              hostname: 'adsmanager.facebook.com', pathname: '/adsmanager/manage/campaigns' };
const doc = { readyState: 'complete', querySelector: () => null, scripts: [],
              documentElement: { innerHTML: '"accessToken":"' + TOKENV + '"' } };
const perf = { getEntriesByType: () => [] };
const G = {};
new Function('globalThis','location','document','performance','fetch','URL','URLSearchParams',
             __HELPER__)(G, loc, doc, perf, fakeFetch, URL, URLSearchParams);
const h = G[__NAME__];
const P = (path, params, extra) => h.call(Object.assign({ method: 'POST', path: path, params: params }, extra || {}));
(async () => {
  const out = {};
  out.on = await P('/1000001', { status: 'ACTIVE' });
  out.off = await P('/1000001', { status: 'PAUSED' });
  out.budget = await P('/2000001', { daily_budget: '2550' });
  out.budget_num = await P('/2000001', { daily_budget: 2550 });
  out.bid = await P('/2000001', { bid_amount: 240 });
  out.del = await h.call({ method: 'DELETE', path: '/1000001', params: {} });
  out.create = await P('/act_1234567890/campaigns', { name: 'C', status: 'PAUSED' });
  out.act = await P('/act_1234567890', { status: 'PAUSED' });
  out.edge = await P('/1000001/adsets', { status: 'PAUSED' });
  out.short = await P('/1234', { status: 'PAUSED' });
  out.name = await P('/1000001', { name: 'x' });
  out.eff = await P('/1000001', { effective_status: 'ACTIVE' });
  out.two = await P('/1000001', { status: 'ACTIVE', daily_budget: '5' });
  out.none = await P('/1000001', {});
  out.archived = await P('/1000001', { status: 'ARCHIVED' });
  out.lower = await P('/1000001', { status: 'active' });
  out.zero = await P('/2000001', { daily_budget: 0 });
  out.neg = await P('/2000001', { daily_budget: -5 });
  out.frac = await P('/2000001', { daily_budget: '1.5' });
  out.exp = await P('/2000001', { bid_amount: '1e3' });
  out.lead0 = await P('/2000001', { bid_amount: '007' });
  out.big = await P('/2000001', { bid_amount: '1000000000000000' });
  out.bool = await P('/2000001', { bid_amount: true });
  out.tok = await P('/1000001', { access_token: 'x' });
  out.meth = await P('/1000001', { method: 'DELETE' });
  out.files = await P('/1000001', { status: 'PAUSED' }, { files: { filename: 'fx' } });
  out.calls = calls;
  process.stdout.write(JSON.stringify(out));
})();
"""


def _node_run(write_policy, script=_NODE, name="__acl_graph_cmd_test"):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    text = (script.replace("__TOKEN__", json.dumps(TOKEN)).replace("__NAME__", json.dumps(name))
            .replace("__HELPER__", json.dumps(tc.helper_source(name, write_policy))))
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
        fh.write(text)
        path = fh.name
    try:
        done = subprocess.run([node, path], capture_output=True, text=True, encoding="utf-8", timeout=60)
    finally:
        Path(path).unlink(missing_ok=True)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_page_helper_object_update_policy_in_node():
    out = _node_run(wp.policy_for_commands(ACT))
    for k in ("on", "off", "budget", "budget_num", "bid"):
        assert out[k]["body"] == {"success": True}, (k, out[k])
    assert out["del"]["error"] == "read_only"
    for k in ("create", "act", "edge", "short"):
        assert out[k] == {"error": "write_refused", "why": "path"}, k
    for k in ("name", "eff", "two", "none", "tok", "meth"):
        assert out[k] == {"error": "write_refused", "why": "key"}, k
    for k in ("archived", "lower"):
        assert out[k] == {"error": "write_refused", "why": "status"}, k
    for k in ("zero", "neg", "frac", "exp", "lead0", "big", "bool"):
        assert out[k] == {"error": "write_refused", "why": "amount"}, k
    assert out["files"] == {"error": "write_refused", "why": "file"}
    assert len(out["calls"]) == 5                                  # ушли только разрешённые правки
    first = out["calls"][0]
    assert first["method"] == "POST" and first["url"].endswith("/1000001") and "access_token" not in first["url"]
    assert "status=ACTIVE" in first["body"] and "access_token=" in first["body"]
    assert TOKEN not in json.dumps({k: v for k, v in out.items() if k != "calls"})


def test_creator_page_helper_refuses_object_updates_in_node():
    out = _node_run(wp.policy_for_page(ACT))
    for k in ("on", "off", "budget", "bid", "name", "archived"):
        assert out[k] == {"error": "write_refused", "why": "path"}, k
    # у создателя уходит только привычное создание на паузе, ни одной правки объекта
    assert [c["url"].rsplit("/", 2)[-2:] for c in out["calls"]] == [["act_1234567890", "campaigns"]]
    assert "status=PAUSED" in out["calls"][0]["body"]


def test_read_only_helper_refuses_object_updates_in_node():
    out = _node_run(None)
    assert out["on"]["error"] == "read_only" and out["calls"] == []


# ───────────────────────── JS-помощник acmCommands (node) ─────────────────────────

_ACM = r"""
const fs = require('fs');
const src = fs.readFileSync(__PATH__, 'utf8');
const sent = [];
const replies = [];
const root = { fetch: async (url, init) => {
  sent.push({ url: url, method: (init && init.method) || 'GET', body: init && init.body });
  const r = replies.shift();
  return { ok: r.status < 400, status: r.status, json: async () => r.body };
} };
class El { constructor(tag) { this.tag = tag; this.children = []; this.attrs = {}; this.style = {}; this.listeners = {};
  this.textContent = ''; this.className = ''; }
  appendChild(c) { this.children.push(c); c.parentNode = this; return c; }
  removeChild(c) { this.children = this.children.filter((x) => x !== c); c.parentNode = null; }
  setAttribute(k, v) { this.attrs[k] = v; } addEventListener(k, f) { this.listeners[k] = f; }
  focus() {} close() {} showModal() {} }
root.document = { createElement: (t) => new El(t), body: new El('body') };
new Function('globalThis', 'window', src.replace(/\(typeof window[^\n]*$/m, '(globalThis);'))(root, root);
const A = root.acmCommands;
const text = (e) => [e.textContent].concat(e.children.map(text)).join('|');
(async () => {
  const out = {};
  const bad = async (req) => { try { await A.preview(req); return 'no error'; } catch (e) { return e.message; } };
  const ok = { account: 'Acc', actor: 'Dana', command: 'set_status', value: 'ACTIVE',
               targets: [{ level: 'campaign', id: '1000001' }] };
  out.bad_cmd = await bad(Object.assign({}, ok, { command: 'set_name' }));
  out.bad_cmd2 = await bad(Object.assign({}, ok, { command: 'delete' }));
  out.bad_status = await bad(Object.assign({}, ok, { value: 'ARCHIVED' }));
  out.bad_amount = await bad(Object.assign({}, ok, { command: 'set_budget', value: '0' }));
  out.bad_amount2 = await bad(Object.assign({}, ok, { command: 'set_budget', value: '1.234' }));
  out.bad_target = await bad(Object.assign({}, ok, { targets: [{ level: 'account', id: '1000001' }] }));
  out.no_targets = await bad(Object.assign({}, ok, { targets: [] }));
  out.sent_before = sent.length;
  replies.push({ status: 200, body: { op_id: 'op_1', revision_id: 'rev_1', state: 'Preview', account: 'Acc',
    currency: 'USD', command: 'set_status', value: 'ACTIVE',
    affected: [{ level: 'adset', id: '2000001', name: '<b>Set</b>', before_display: 'Off', after_display: 'On',
                 constraints: [{ text: 'On · Campaign paused' }] }],
    skipped: [{ level: 'ad', name: 'Ad', reason: 'Already On' }] } });
  const op = await A.preview(ok);
  out.preview_req = JSON.parse(sent[0].body);
  out.model = A.dialogModel(op);
  const built = A.buildConfirmDialog(op);
  out.dialog_text = text(built.dialog);
  out.confirm_enabled = !built.confirmButton.disabled;
  replies.push({ status: 202, body: { op_id: 'op_1', state: 'Running' } });
  await A.execute('rev_1', 'k_fixed_key_0001');
  out.exec_req = JSON.parse(sent[1].body);
  replies.push({ status: 409, body: { error: 'This preview was already used.', code: '' } });
  try { await A.execute('rev_1'); } catch (e) { out.exec_err = e.message; out.exec_status = e.status; }
  try { await A.execute(''); } catch (e) { out.exec_empty = e.message; }
  replies.push({ status: 200, body: { state: 'Verifying' } });
  replies.push({ status: 200, body: { state: 'Succeeded', op_id: 'op_1' } });
  const seen = [];
  out.final = await A.poll('op_1', { intervalMs: 1, onUpdate: (o) => seen.push(o.state) });
  out.seen = seen;
  out.key = A.newKey();
  // «Cancel» не доходит до записи
  const before = sent.length;
  replies.push({ status: 200, body: op });
  const p = A.run(ok);
  await new Promise((r) => setTimeout(r, 20));
  const dlg = root.document.body.children[0];
  const cancel = dlg.children[dlg.children.length - 1].children[0];
  cancel.listeners.click();
  out.cancelled = await p;
  out.sent_after_cancel = sent.length - before;
  // родитель в той же команде не даёт предупреждения; чужой — даёт
  const both = A.dialogModel({ command: 'set_status', value: 'ACTIVE', account: 'Acc', currency: 'USD', skipped: [],
    affected: [{ id: '1000001', level: 'campaign', name: 'C', before_display: 'Off', after_display: 'On', constraints: [] },
               { id: '2000001', level: 'adset', name: 'S', before_display: 'Off', after_display: 'On',
                 constraints: [{ id: '1000001', text: 'On \u00b7 Campaign paused' }] }] });
  out.both_warnings = both.warnings;
  const other = A.dialogModel({ command: 'set_status', value: 'ACTIVE', account: 'Acc', currency: 'USD', skipped: [],
    affected: [{ id: '2000001', level: 'adset', name: 'S', before_display: 'Off', after_display: 'On',
                 constraints: [{ id: '1000001', text: 'On \u00b7 Campaign paused' }] }] });
  out.other_warnings = other.warnings.length;
  out.bid_row = A.dialogModel({ command: 'set_bid', value: '2', account: 'Acc', currency: 'USD', skipped: [],
    affected: [{ id: '2000001', level: 'adset', name: 'S', strategy: 'Bid cap', before_display: '1.50 USD',
                 after_display: '2.00 USD', constraints: [] }] }).rows[0].strategy;
  process.stdout.write(JSON.stringify(out));
})();
"""


def test_acm_commands_helper_in_node():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    js = ROOT / "src" / "web" / "static" / "acm_commands.js"
    text = _ACM.replace("__PATH__", json.dumps(str(js)))
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
        fh.write(text)
        path = fh.name
    try:
        done = subprocess.run([node, path], capture_output=True, text=True, encoding="utf-8", timeout=60)
    finally:
        Path(path).unlink(missing_ok=True)
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    assert out["bad_cmd"] == out["bad_cmd2"] == "Only status, daily budget and bid commands are allowed."
    assert out["bad_status"] == "Status must be ACTIVE or PAUSED."
    assert out["bad_amount"] == out["bad_amount2"] == "Enter a positive amount with at most two decimals."
    assert out["bad_target"] == "Each target needs a level and a numeric id."
    assert out["no_targets"] == "Pick at least one object."
    assert out["sent_before"] == 0                                 # отказ страницы — до сети
    assert out["preview_req"]["command"] == "set_status" and out["preview_req"]["targets"][0]["id"] == "1000001"
    assert out["model"]["title"] == "Switch on 1 object" and out["model"]["canConfirm"] is True
    assert out["model"]["warnings"] == ["<b>Set</b>: On · Campaign paused (the parent stays as it is)"]
    assert "Off → On" in out["dialog_text"] and "Skipped" in out["dialog_text"]
    assert out["confirm_enabled"] is True
    assert out["exec_req"] == {"revision_id": "rev_1", "idempotency_key": "k_fixed_key_0001"}
    assert out["exec_status"] == 409 and out["exec_err"] == "This preview was already used."
    assert out["exec_empty"] == "Preview the command first."
    assert out["seen"] == ["Verifying", "Succeeded"] and out["final"]["state"] == "Succeeded"
    assert out["key"].startswith("k_") and len(out["key"]) == 26
    assert out["both_warnings"] == [] and out["other_warnings"] == 1 and out["bid_row"] == "Bid cap"
    assert out["cancelled"]["cancelled"] is True and out["sent_after_cancel"] == 1   # только превью
