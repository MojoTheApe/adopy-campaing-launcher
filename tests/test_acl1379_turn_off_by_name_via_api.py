"""ACL-1379: выключить кампанию по точному имени там, где таблица не даёт id.

Построено поверх ACL-1945 (выключение по id через Graph API из профиля): имя
превращается в единственный id чтением списка кампаний, дальше идёт тот же путь.
Meta и Multilogin не трогаются: `FakeMeta` из теста 1945 в памяти.
Проверяется: единственное совпадение -> ровно один `POST /<id>` и сверка чтением;
ноль и несколько совпадений, неполный список -> отказ без записи; занятый профиль;
журнал (`route: api`, `matched_by: name`); маршрут без `object_id`; объявление по имени не идёт.
"""
from __future__ import annotations

import pytest

from src.analysis_agent import account_spend as sp
from src.analysis_agent import account_spend_api as api
from tests.test_acl1945_turn_off_via_api import (  # noqa: F401  (world - autouse-фикстура)
    ACCOUNT, AD, ADSET, CAMP, CAMP2, FakeMeta, _post, journal, world,
)

UNIQUE = "Unique Camp"


class ListingMeta(FakeMeta):
    """К FakeMeta добавлен список кампаний аккаунта."""

    def __init__(self):
        super().__init__()
        self.partial = False
        self.list_calls = 0
        self.objs[CAMP]["name"] = UNIQUE
        # CAMP2 остался с именем "Camp One"; ADSET (не кампания) носит то же имя.

    def request(self, method, path, params=None):
        if method == "GET" and path.strip("/").endswith("/campaigns"):
            self.list_calls += 1
            rows = [{"id": o["id"], "name": o["name"], "status": o["status"],
                     "effective_status": o["effective_status"]}
                    for o in self.objs.values() if o.get("objective")]
            body = {"data": rows}
            if self.partial:
                body["paging"] = {"cursors": {"after": "x"}, "next": "https://graph/next"}
            return {"status": 200, "body": body}
        return super().request(method, path, params)


@pytest.fixture
def meta():
    return ListingMeta()


def by_name(name, **kw):
    return api.turn_off_by_name(ACCOUNT, name, requester="Dana", **kw)


def test_a_unique_name_pauses_exactly_that_campaign_and_confirms_by_readback(meta):
    out = by_name(UNIQUE)

    assert out.state == "off" and out.error == ""
    assert out.object_id == CAMP and out.name == UNIQUE
    assert meta.posts == [(f"/{CAMP}", {"status": "PAUSED"})]
    assert meta.objs[CAMP]["status"] == "PAUSED" and meta.objs[CAMP2]["status"] == "ACTIVE"


def test_the_journal_says_it_was_matched_by_name_through_the_api(meta):
    by_name(UNIQUE)

    row = journal()[0]
    assert row["route"] == "api" and row["matched_by"] == "name"
    assert row["object_id"] == CAMP and row["state"] == "off"


def test_whitespace_in_the_requested_name_is_normalised(meta):
    out = by_name("  Unique    Camp ")

    assert out.state == "off" and out.object_id == CAMP


def test_a_name_that_only_partly_matches_is_not_a_match(meta):
    out = by_name("Unique")

    assert out.state == "failed" and "No campaign named" in out.error
    assert meta.posts == []


def test_a_missing_name_is_refused_and_nothing_is_sent(meta):
    out = by_name("Nope")

    assert out.state == "failed" and "No campaign named" in out.error
    assert meta.posts == [] and journal()[0]["matched_by"] == "name"


def test_a_duplicate_name_is_refused_and_nothing_is_sent(meta):
    meta.objs[CAMP]["name"] = "Camp One"          # теперь их две: CAMP и CAMP2

    out = by_name("Camp One")

    assert out.state == "failed" and "2 campaigns are named" in out.error
    assert meta.posts == []
    assert meta.objs[CAMP]["status"] == meta.objs[CAMP2]["status"] == "ACTIVE"


def test_an_ad_set_with_the_same_name_is_not_a_campaign_candidate(meta):
    assert meta.objs[ADSET]["name"] == "Camp One"

    out = by_name("Camp One")                      # CAMP2 единственная кампания с этим именем

    assert out.state == "off" and out.object_id == CAMP2
    assert meta.posts == [(f"/{CAMP2}", {"status": "PAUSED"})]


def test_a_partial_campaign_list_is_not_trusted(meta):
    meta.partial = True

    out = by_name(UNIQUE)

    assert out.state == "failed" and "partial" in out.error
    assert meta.posts == []


def test_an_empty_name_is_refused_before_any_session(meta, world):
    out = by_name("   ")

    assert out.state == "failed" and world.sessions == 0 and meta.posts == []


def test_a_busy_profile_is_refused_before_the_list_is_even_read(meta, world):
    out = by_name(UNIQUE, profile_busy=lambda key: True)

    assert out.error == api.PROFILE_BUSY_ERROR
    assert world.sessions == 0 and meta.list_calls == 0 and meta.posts == []
    assert journal()[0]["error"] == api.PROFILE_BUSY_ERROR


def test_an_already_paused_campaign_is_reported_and_nothing_is_sent(meta):
    meta.objs[CAMP]["status"] = meta.objs[CAMP]["effective_status"] = "PAUSED"

    out = by_name(UNIQUE)

    assert out.state == "already_off" and meta.posts == []


def test_a_readback_that_still_shows_it_on_is_not_reported_as_success(meta):
    meta.fail_next[CAMP] = "drop"

    out = by_name(UNIQUE)

    assert out.state != "off" and "read-back" in out.error


# ───────────────────────── маршрут ─────────────────────────

def test_the_route_turns_off_by_name_when_there_is_no_id_under_the_api_backend(meta, monkeypatch):
    monkeypatch.setenv("ACL_FB_BACKEND", "api")
    monkeypatch.setattr(sp, "turn_off", lambda *a, **k: pytest.fail("the RPA path must not run"))

    r = _post({"account": ACCOUNT, "level": "campaign", "object_id": "", "name": UNIQUE})

    body = r.json()
    assert r.status_code == 200 and body["ok"] is True
    assert body["result"]["state"] == "off" and body["result"]["matched_by"] == "name"
    assert meta.posts == [(f"/{CAMP}", {"status": "PAUSED"})]


def test_the_route_refuses_a_duplicate_name_with_a_clear_message(meta, monkeypatch):
    monkeypatch.setenv("ACL_FB_BACKEND", "api")
    meta.objs[CAMP]["name"] = "Camp One"

    r = _post({"account": ACCOUNT, "level": "campaign", "object_id": "", "name": "Camp One"})

    res = r.json()["result"]
    assert res["state"] == "failed" and "2 campaigns are named" in res["error"]
    assert meta.posts == []


def test_the_route_refuses_while_a_publish_job_runs_in_the_profile(meta, monkeypatch):
    monkeypatch.setenv("ACL_FB_BACKEND", "api")
    from src.web import publish_jobs

    monkeypatch.setattr(publish_jobs, "profile_in_use", lambda key, since="": True)

    r = _post({"account": ACCOUNT, "level": "campaign", "object_id": "", "name": UNIQUE})

    assert r.status_code == 409 and r.json()["busy"] is True
    assert meta.posts == [] and meta.list_calls == 0


def test_an_ad_cannot_be_turned_off_by_name(meta, monkeypatch):
    monkeypatch.setenv("ACL_FB_BACKEND", "api")

    r = _post({"account": ACCOUNT, "level": "ad", "object_id": "", "name": "Ad one", "parent_id": CAMP})

    assert r.status_code == 400 and meta.posts == [] and AD not in str(meta.posts)


def test_without_the_api_backend_an_id_is_still_required(meta, monkeypatch):
    monkeypatch.delenv("ACL_FB_BACKEND", raising=False)

    r = _post({"account": ACCOUNT, "level": "campaign", "object_id": "", "name": UNIQUE})

    assert r.status_code == 400 and meta.posts == []
