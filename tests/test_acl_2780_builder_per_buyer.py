"""ACL-2780: Campaign Builder следует за выбранным байером.

Черновики (keep/jobs/ui) хранятся по байерам; существующие принадлежат байеру
по умолчанию; строки отчёта в билдере — только аккаунты выбранного байера."""
from __future__ import annotations

import asyncio
import csv
import json

import pytest

from src.web import account_buyer, builder_drafts as bd
from src.web import app as web

HEAD = ["account_name", "ad_account_id", "profile_id", "buyer"]


@pytest.fixture
def env(monkeypatch, tmp_path):
    map_path = tmp_path / "ad_account_map.csv"
    monkeypatch.setenv("MY_CM_NAME", "Owner")
    monkeypatch.setenv("ACL_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr("config.paths.get_local_config_dir", lambda: str(tmp_path))
    monkeypatch.setattr(account_buyer, "get_ad_account_map_path", lambda: str(map_path))
    monkeypatch.setattr("src.web.buyer._path", lambda: tmp_path / "selected_cm.txt")
    with open(map_path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=HEAD)
        w.writeheader()
        w.writerow({"account_name": "OWN_1", "buyer": ""})
        w.writerow({"account_name": "BOB_1", "buyer": "Bob"})
    return tmp_path


def _select(name):
    from src.web.buyer import set_selected_cm
    set_selected_cm(name)


class _Req:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


def _post(name, text, at=1000, buyer=None):
    body = {"name": name, "saved_at": at, "data": {"text": text}}
    if buyer is not None:
        body["buyer"] = buyer
    return json.loads(asyncio.run(web.api_builder_state_set(_Req(body))).body)


def _get(buyer=None):
    return json.loads(web.api_builder_state_get(buyer).body)["docs"]


def test_preexisting_drafts_belong_to_default_buyer(env):
    # файл прежнего имени, написанный до ACL-2780
    (env / "builder_drafts.json").write_text(json.dumps(
        {"docs": {"jobs": {"saved_at": 5, "data": {"x": 1}}}}), encoding="utf-8")
    assert _get()["jobs"]["data"] == {"x": 1}
    _select("Bob")
    assert _get() == {}
    _select("Owner")
    assert _get()["jobs"]["data"] == {"x": 1}
    assert (env / "builder_drafts.json").exists()


def test_switch_buyer_hides_other_drafts_and_switch_back_restores(env):
    _post("jobs", "owner-draft")
    _select("Bob")
    assert _get() == {}
    assert _post("jobs", "bob-draft")["accepted"] is True
    assert _get()["jobs"]["data"] == {"text": "bob-draft"}
    _select("Owner")
    assert _get()["jobs"]["data"] == {"text": "owner-draft"}
    _select("Bob")
    assert _get()["jobs"]["data"] == {"text": "bob-draft"}


def test_explicit_buyer_wins_over_selected(env):
    """Вкладка, оставшаяся от прежнего байера, не пишет в набор нового."""
    _select("Bob")
    _post("jobs", "stale-owner-tab", buyer="Owner")
    assert _get() == {}
    assert _get("Owner")["jobs"]["data"] == {"text": "stale-owner-tab"}


def test_buyer_names_do_not_collide(env):
    _post("ui", "a", buyer="Bob B")
    _post("ui", "b", buyer="Bob_B")
    assert _get("Bob B")["ui"]["data"] == {"text": "a"}
    assert _get("Bob_B")["ui"]["data"] == {"text": "b"}


def test_report_rows_exclude_other_buyers_accounts(env):
    def report():
        return {"countries": [{
            "country": "Peru",
            "open_recs": {"OWN_1": [1], "BOB_1": [2], "GHOST": [3]},
            "account_rows": [{"account_label": "OWN_1"}, {"account_label": "BOB_1"}, {"account_label": "GHOST"}],
        }]}

    r = report()
    web._drop_other_buyers_builder_rows(r)
    c = r["countries"][0]
    assert sorted(c["open_recs"]) == ["GHOST", "OWN_1"]   # без строки в карте — ничей, остаётся
    assert [x["account_label"] for x in c["account_rows"]] == ["OWN_1", "GHOST"]
    _select("Bob")
    r = report()
    web._drop_other_buyers_builder_rows(r)
    c = r["countries"][0]
    assert sorted(c["open_recs"]) == ["BOB_1", "GHOST"]
    assert [x["account_label"] for x in c["account_rows"]] == ["BOB_1", "GHOST"]


def test_page_scope_suffix(env):
    assert web._builder_buyer_scope() == {"name": "Owner", "suffix": ""}
    _select("Bob")
    s = web._builder_buyer_scope()
    assert s["name"] == "Bob" and s["suffix"].startswith(".bob-")
