"""ACL-1945: выключение кампании через Graph API по id внутри профиля.

Ни Meta, ни Multilogin не трогаются: Graph — это `FakeMeta` в памяти (транспорт
`GraphClient` с той же политикой записи, что у страницы), сессия подменена.
Проверяется: форма запроса (`POST /<id>`, ровно `status=PAUSED`), сверка чтением,
расхождение при сверке, отказ при занятом профиле, запись в журнале
(`route: api`, `matched_by: id`), объявление — только при подтверждённой кампании,
маршрут `/account-spend/turn-off` под `ACL_FB_BACKEND=api`.
"""
from __future__ import annotations

import asyncio
import json
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.analysis_agent import account_spend as sp  # noqa: E402
from src.analysis_agent import account_spend_api as api  # noqa: E402
from src.fb_api import graph as g  # noqa: E402
from src.fb_api import write_policy as wp  # noqa: E402

ACT = "act_1234567890"
ACT_DIGITS = "1234567890"
ACCOUNT = "Acc"
CAMP, CAMP2, AD, ADSET = "1000001", "1000002", "3000001", "2000001"


class FakeMeta:
    """GET по id, POST только через политику правок; ничего другого."""

    def __init__(self):
        self.posts: list[tuple[str, dict]] = []
        self.fail_next: dict[str, str] = {}      # id → applied | lost | refuse | drop | lag
        self.objs = {
            CAMP: {**self._o(CAMP, "Camp One", "ACTIVE", "ACTIVE"), "objective": "OUTCOME_LEADS"},
            CAMP2: {**self._o(CAMP2, "Camp One", "ACTIVE", "ACTIVE"), "objective": "OUTCOME_LEADS"},
            ADSET: self._o(ADSET, "Camp One", "ACTIVE", "ACTIVE"),       # группа с именем кампании
            AD: {**self._o(AD, "Ad one", "ACTIVE", "ACTIVE"),
                 "campaign": {"id": CAMP, "name": "Camp One", "status": "ACTIVE", "effective_status": "ACTIVE"}},
        }

    @staticmethod
    def _o(oid, name, status, effective):
        return {"id": oid, "name": name, "account_id": ACT_DIGITS, "status": status,
                "effective_status": effective}

    def state(self):
        return {"kind": "ads_manager", "has_token": True}

    def reacquire(self, timeout=60.0):
        return self.state()

    def request(self, method, path, params=None):
        assert method == "GET"
        obj = self.objs.get(path.strip("/"))
        if obj is None:
            return {"status": 400, "body": {"error": {"code": 100, "message": "Object does not exist"}}}
        return {"status": 200, "body": dict(obj)}

    def write(self, path, params, files=None, filename=""):
        wp.check_object_update("POST", path, params, files)
        self.posts.append((path, dict(params)))
        oid = path.strip("/")
        mode = self.fail_next.pop(oid, None)
        if mode == "lost":
            return {"error": "call_unknown"}
        if mode == "refuse":
            return {"status": 400, "body": {"error": {"code": 100, "message": "Invalid parameter"}}}
        if mode != "drop":
            self.objs[oid]["status"] = params["status"]
            self.objs[oid]["effective_status"] = "ACTIVE" if mode == "lag" else params["status"]
        if mode == "applied":
            return {"error": "call_unknown"}
        return {"status": 200, "body": {"success": True}}


@pytest.fixture
def meta():
    return FakeMeta()


@pytest.fixture(autouse=True)
def world(tmp_path, monkeypatch, meta):
    monkeypatch.setattr(sp, "_store_path", lambda: str(tmp_path / "spend.json"))
    # ACL-1853: ключ очереди - физический UUID профиля из карты; в тесте он строится на лету.
    entry = SimpleNamespace(profile_id=str(uuid.UUID(int=1945)))
    monkeypatch.setattr(sp, "_entry_for", lambda account: entry)
    monkeypatch.setattr(api, "TURN_OFF_RECONCILE_PAUSE_S", 0.0)
    state = SimpleNamespace(sessions=0, clients=[])

    @contextmanager
    def fake_session(account, *, commands=False):
        state.sessions += 1
        client = g.GraphClient(meta, allow_writes=commands, min_interval_s=0.0, max_calls=80,
                               sleep=lambda s: None)
        state.clients.append(client)
        yield SimpleNamespace(client=client, act=ACT, account=account, notes={})

    monkeypatch.setattr(api, "_session", fake_session)
    return state


def journal() -> list[dict]:
    return sp.turn_off_history()


def off(level="campaign", oid=CAMP, **kw):
    kw.setdefault("expected_name", "Camp One" if oid in (CAMP, CAMP2) else "Ad one")
    if level == "ad":
        kw.setdefault("parent_id", CAMP)
    return api.turn_off(ACCOUNT, level, oid, requester="Dana", **kw)


# ───────────────────────── запрос и сверка ─────────────────────────

def test_posts_paused_to_the_campaign_id_and_confirms_by_readback(meta):
    out = off()

    assert meta.posts == [(f"/{CAMP}", {"status": "PAUSED"})]
    assert out.state == "off" and out.error == ""
    assert out.delivery == "Off"
    assert "read back status=PAUSED" in out.how
    assert meta.objs[CAMP]["status"] == "PAUSED"
    assert meta.objs[CAMP2]["status"] == "ACTIVE", "одноимённая кампания не тронута"


def test_the_journal_names_the_route_and_how_it_was_matched(meta):
    off()

    (row,) = journal()
    assert row["route"] == "api" and row["matched_by"] == "id"
    assert row["state"] == "off" and row["object_id"] == CAMP and row["requester"] == "Dana"


def test_an_already_paused_campaign_sends_nothing(meta):
    meta.objs[CAMP]["status"] = "PAUSED"
    meta.objs[CAMP]["effective_status"] = "PAUSED"

    out = off()

    assert out.state == "already_off" and meta.posts == []
    assert journal()[0]["state"] == "already_off"


def test_readback_that_still_shows_it_on_is_a_failure_not_a_success(meta):
    meta.fail_next[CAMP] = "drop"       # Meta ответила OK, но не сохранила

    out = off()

    assert out.state == "failed" and "read-back shows status ACTIVE" in out.error
    assert journal()[0]["state"] == "failed"


def test_paused_but_delivery_still_active_is_unconfirmed(meta):
    meta.fail_next[CAMP] = "lag"

    out = off()

    assert out.state == "unconfirmed" and "still reports delivery ACTIVE" in out.error


def test_meta_refusal_is_a_failure_and_nothing_is_retried(meta):
    meta.fail_next[CAMP] = "refuse"

    out = off()

    assert out.state == "failed" and out.error.startswith("Meta refused")
    assert len(meta.posts) == 1


def test_a_lost_answer_is_reconciled_by_reading_back_without_a_second_post(meta):
    meta.fail_next[CAMP] = "applied"    # применено, ответ потерян

    out = off()

    assert out.state == "off" and len(meta.posts) == 1


def test_a_lost_answer_that_did_not_apply_is_unconfirmed(meta):
    meta.fail_next[CAMP] = "lost"       # не применено, ответ потерян

    out = off()

    assert out.state == "unconfirmed" and len(meta.posts) == 1
    assert journal()[0]["state"] == "unconfirmed"


def test_a_crash_after_the_post_is_unconfirmed_and_before_it_failed(meta, world, monkeypatch):
    real = FakeMeta.write

    def boom(self, path, params, files=None, filename=""):
        real(self, path, params, files, filename)
        raise RuntimeError("tab died")

    monkeypatch.setattr(FakeMeta, "write", boom)
    out = off()
    assert out.state in ("unconfirmed", "failed")
    assert journal()[0]["state"] == out.state


# ───────────────────────── отказы до записи ─────────────────────────

def test_a_name_that_differs_from_the_one_under_this_id_touches_nothing(meta):
    out = off(expected_name="Another campaign")

    assert out.state == "failed" and "nothing was touched" in out.error
    assert meta.posts == []


def test_an_object_of_another_ad_account_is_refused(meta):
    meta.objs[CAMP]["account_id"] = "999"

    out = off()

    assert out.state == "failed" and "different ad account" in out.error and meta.posts == []


@pytest.mark.parametrize("level,oid,parent", [("adset", CAMP, ""), ("campaign", "abc", ""),
                                              ("campaign", "", ""), ("ad", AD, ""), ("ad", AD, "x1")])
def test_bad_input_is_refused_before_any_session(level, oid, parent, meta, world):
    out = api.turn_off(ACCOUNT, level, oid, parent_id=parent, expected_name="x", requester="Dana")

    assert out.state == "failed" and out.error
    assert world.sessions == 0 and meta.posts == []
    assert journal()[0]["route"] == "api"


def test_a_busy_profile_is_refused_with_the_journal_entry(meta, world):
    out = off(profile_busy=lambda key: key == str(uuid.UUID(int=1945)).lower())

    assert out.state == "failed" and out.error == api.PROFILE_BUSY_ERROR
    assert world.sessions == 0 and meta.posts == []
    assert journal()[0]["error"] == api.PROFILE_BUSY_ERROR


def test_a_busy_check_that_fails_counts_as_busy(meta, world):
    def broken(key):
        raise RuntimeError("no answer")

    out = off(profile_busy=broken)

    assert out.error == api.PROFILE_BUSY_ERROR and meta.posts == []


def test_a_read_only_session_cannot_write(meta, monkeypatch):
    @contextmanager
    def readonly(account, *, commands=False):
        yield SimpleNamespace(client=g.GraphClient(meta, allow_writes=False, min_interval_s=0.0,
                                                   sleep=lambda s: None), act=ACT, account=account, notes={})

    monkeypatch.setattr(api, "_session", readonly)

    out = off()

    assert out.state == "failed" and "cannot write" in out.error and meta.posts == []


def test_a_session_that_cannot_open_is_a_failure_with_the_journal_entry(meta, monkeypatch):
    @contextmanager
    def closed(account, *, commands=False):
        raise api.ApiReadError("The profile could not be opened", code="x")
        yield

    monkeypatch.setattr(api, "_session", closed)

    out = off()

    assert out.state == "failed" and "could not be opened" in out.error
    assert journal()[0]["route"] == "api"


# ───────────────────────── объявление ─────────────────────────

def test_an_ad_is_turned_off_only_under_the_campaign_the_api_confirms(meta):
    out = off("ad", AD)

    assert out.state == "off" and meta.posts == [(f"/{AD}", {"status": "PAUSED"})]


def test_an_ad_under_a_different_campaign_is_refused(meta):
    out = off("ad", AD, parent_id=CAMP2)

    assert out.state == "failed" and "does not confirm" in out.error and meta.posts == []


def test_an_ad_without_a_parent_in_the_answer_is_refused(meta):
    meta.objs[AD].pop("campaign")

    out = off("ad", AD)

    assert out.state == "failed" and meta.posts == []


# ───────────────────────── маршрут ─────────────────────────

def _post(form: dict):
    import httpx

    from src.web.app import app

    async def go():
        t = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=t, base_url="http://127.0.0.1") as c:
            return await c.post("/account-spend/turn-off", data=form)

    return asyncio.run(go())


def test_the_route_turns_off_by_id_without_a_period_under_the_api_backend(meta, monkeypatch):
    monkeypatch.setenv("ACL_FB_BACKEND", "api")
    monkeypatch.setattr(sp, "turn_off", lambda *a, **k: pytest.fail("the RPA path must not run"))

    r = _post({"account": ACCOUNT, "level": "campaign", "object_id": CAMP, "name": "Camp One"})

    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["result"]["state"] == "off"
    assert body["result"]["route"] == "api" and body["result"]["matched_by"] == "id"
    assert meta.posts == [(f"/{CAMP}", {"status": "PAUSED"})]


def test_the_route_refuses_while_a_publish_job_runs_in_the_profile(meta, monkeypatch):
    monkeypatch.setenv("ACL_FB_BACKEND", "api")
    from src.web import publish_jobs

    monkeypatch.setattr(publish_jobs, "profile_in_use", lambda key, since="": True)

    r = _post({"account": ACCOUNT, "level": "campaign", "object_id": CAMP, "name": "Camp One"})

    assert r.status_code == 409
    body = r.json()
    assert body["ok"] is False and body["busy"] is True and body["error"] == api.PROFILE_BUSY_ERROR
    assert meta.posts == []
    assert journal()[0]["error"] == api.PROFILE_BUSY_ERROR and journal()[0]["route"] == "api"


def test_the_route_keeps_the_rpa_path_when_the_backend_is_rpa(monkeypatch):
    monkeypatch.delenv("ACL_FB_BACKEND", raising=False)
    seen = {}

    def rpa(account, level, object_id, **kw):
        seen["called"] = (account, level, object_id)
        return sp.TurnOffResult(account=account, level=level, object_id=object_id, state="off")

    monkeypatch.setattr(sp, "turn_off", rpa)
    monkeypatch.setattr(api, "turn_off", lambda *a, **k: pytest.fail("the API path must not run"))

    r = _post({"account": ACCOUNT, "level": "campaign", "object_id": CAMP, "name": "Camp One",
               "date_from": "2026-10-01", "date_to": "2026-10-01"})

    assert r.status_code == 200 and seen["called"] == (ACCOUNT, "campaign", CAMP)


def test_the_journal_line_is_valid_json_with_the_new_field(meta):
    off()
    line = sp._turn_off_log().read_text(encoding="utf-8").splitlines()[-1]
    assert json.loads(line)["matched_by"] == "id"


def test_an_ad_set_id_sent_as_a_campaign_is_refused_even_with_the_same_name(meta):
    out = off("campaign", ADSET, expected_name="Camp One")

    assert out.state == "failed" and "does not confirm" in out.error
    assert meta.posts == [] and meta.objs[ADSET]["status"] == "ACTIVE"
