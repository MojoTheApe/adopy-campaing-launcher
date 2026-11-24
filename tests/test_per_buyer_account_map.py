"""Per-buyer account map: the machine map is tagged by the buyer it was
connected under, and every account view follows the "Logged as" switch."""
from __future__ import annotations

import asyncio
import csv
import json

import pytest

from src.web import account_buyer, account_links as links
from src.web.app import _add_linked_accounts_to_builder, _build_account_map_view, account_map_patch

HEAD = ["account_name", "ad_account_id", "profile_id"]


@pytest.fixture
def env(monkeypatch, tmp_path):
    map_path = tmp_path / "ad_account_map.csv"
    monkeypatch.setenv("MY_CM_NAME", "Owner")
    monkeypatch.setattr("config.paths.get_local_config_dir", lambda: str(tmp_path))
    monkeypatch.setattr(links, "get_local_config_dir", lambda: str(tmp_path))
    for mod in (links, account_buyer):
        monkeypatch.setattr(mod, "get_ad_account_map_path", lambda: str(map_path))
    monkeypatch.setattr("src.web.app.get_ad_account_map_path", lambda: str(map_path))
    monkeypatch.setattr("src.web.buyer._path", lambda: tmp_path / "selected_cm.txt")
    return map_path


def _write_map(path, rows, head=HEAD):
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=head, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def _select(name):
    from src.web.buyer import set_selected_cm
    set_selected_cm(name)


def _patch(body):
    from starlette.requests import Request

    raw = json.dumps(body).encode()

    async def _receive():
        return {"type": "http.request", "body": raw, "more_body": False}

    scope = {"type": "http", "method": "POST", "path": "/x", "headers": [(b"content-type", b"application/json")]}
    return asyncio.run(account_map_patch(Request(scope, receive=_receive)))


def _view_accounts():
    view = _build_account_map_view({"countries": []})
    return sorted(r["account"] for c in view["countries"] for r in c["rows"])


def test_untagged_rows_belong_to_default_buyer(env):
    _write_map(env, [{"account_name": "KLD_F92"}])
    assert account_buyer.owner_of("KLD_F92") == "Owner"
    assert account_buyer.is_visible("KLD_F92", "Owner")
    assert not account_buyer.is_visible("KLD_F92", "Dan Dicker")


def test_connecting_under_another_buyer_tags_the_row(env):
    _write_map(env, [{"account_name": "KLD_F92", "ad_account_id": "act_11111111"}])
    _select("Dan Dicker")
    assert json.loads(_patch({"account_name": "DanAcc1", "ad_account_id": "22222222"}).body)["ok"]
    rows = list(csv.DictReader(open(env, encoding="utf-8")))
    by = {r["account_name"]: r for r in rows}
    assert by["DanAcc1"]["buyer"] == "Dan Dicker"
    assert by["KLD_F92"]["buyer"] == ""  # existing row untouched


def test_connecting_under_default_buyer_leaves_tag_blank(env):
    _patch({"account_name": "OwnAcc"})
    rows = list(csv.DictReader(open(env, encoding="utf-8")))
    assert rows[0].get("buyer", "") == ""


def test_editing_an_existing_row_keeps_its_tag(env):
    _write_map(env, [{"account_name": "KLD_F92"}])
    _select("Dan Dicker")
    _patch({"account_name": "KLD_F92", "profile_id": "p1"})
    rows = list(csv.DictReader(open(env, encoding="utf-8")))
    assert rows[0]["buyer"] == ""
    assert rows[0]["profile_id"] == "p1"


def test_account_map_view_follows_selected_buyer_and_returns(env):
    _write_map(env, [{"account_name": "KLD_F92"}])
    links.link("KLD_F92", "country", "Kuwait")
    _select("Dan Dicker")
    _patch({"account_name": "DanAcc1", "ad_account_id": "22222222"})
    links.link("DanAcc1", "country", "Chile")

    assert _view_accounts() == ["DanAcc1"]
    _select("Owner")
    assert _view_accounts() == ["KLD_F92"]
    _select("")
    assert _view_accounts() == ["KLD_F92"]


def test_builder_accounts_follow_selected_buyer(env):
    _write_map(env, [{"account_name": "KLD_F92"}])
    links.link("KLD_F92", "country", "Kuwait")
    _select("Dan Dicker")
    _patch({"account_name": "DanAcc1"})
    links.link("DanAcc1", "country", "Kuwait")

    def labels():
        report = {"countries": []}
        _add_linked_accounts_to_builder(report)
        return sorted(k for c in report["countries"] for k in c["open_recs"])

    assert labels() == ["DanAcc1"]
    _select("Owner")
    assert labels() == ["KLD_F92"]


def test_profile_and_ids_lookups_ignore_the_selected_buyer(env):
    from src.meta_campaign_agent.ad_account_map import load_ad_account_map

    _write_map(env, [{"account_name": "KLD_F92", "ad_account_id": "act_11111111", "profile_id": "p9"}])
    _select("Dan Dicker")
    entry = load_ad_account_map(str(env))["KLD_F92"]
    assert entry.profile_id == "p9" and entry.ad_account_id == "act_11111111"


def test_older_reader_still_reads_a_tagged_file(env):
    from src.meta_campaign_agent.ad_account_map import load_ad_account_map

    _write_map(env, [{"account_name": "DanAcc1", "buyer": "Dan Dicker", "profile_id": "p2"}], head=HEAD + ["buyer"])
    assert load_ad_account_map(str(env))["DanAcc1"].profile_id == "p2"


def test_unmapped_accounts_stay_visible(env):
    assert account_buyer.is_visible("NeverConnected", "Dan Dicker")


def test_without_a_default_buyer_untagged_rows_are_visible_to_all(env, monkeypatch):
    monkeypatch.delenv("MY_CM_NAME", raising=False)
    _write_map(env, [{"account_name": "KLD_F92"}])
    assert account_buyer.is_visible("KLD_F92", "Dan Dicker")


def test_accounts_overview_hides_other_buyers_accounts(env, monkeypatch):
    import httpx
    import pandas as pd
    from src.web import app as web_app

    _write_map(env, [{"account_name": "KLD_F92", "profile_id": "p1"}])
    _select("Dan Dicker")
    _patch({"account_name": "DanAcc1", "profile_id": "p2"})
    monkeypatch.setattr("src.analysis_agent.gsheet_accounts.fetch_account_rows", lambda *a, **k: [])
    monkeypatch.setattr("src.fb_ads_rpa.mlx_profiles.open_profiles_detailed", lambda: [])
    monkeypatch.setattr(web_app, "get_cached_report", lambda *a, **k: ({}, pd.DataFrame(), []))

    def overview():
        async def go():
            t = httpx.ASGITransport(app=web_app.app, raise_app_exceptions=False)
            async with httpx.AsyncClient(transport=t, base_url="http://127.0.0.1") as c:
                return (await c.get("/api/accounts-overview")).json()
        return sorted(a["label"] for a in asyncio.run(go())["accounts"])

    assert overview() == ["DanAcc1"]
    _select("Owner")
    assert overview() == ["KLD_F92"]
