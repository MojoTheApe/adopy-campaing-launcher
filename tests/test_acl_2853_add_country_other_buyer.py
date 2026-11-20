"""ACL-2853: «Add <country> to the account map» для страны, где лежат только
аккаунты другого байера. Кнопка раньше отвечала ok/changed=false и ничего не
меняла на экране."""
from __future__ import annotations

import asyncio
import csv
import json

import pytest
from starlette.requests import Request

from src.web import account_buyer, account_links as links
from src.web.app import _build_account_map_view, account_links_edit

HEAD = ["account_name", "ad_account_id", "profile_id", "buyer"]


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
    with open(map_path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=HEAD)
        w.writeheader()
        w.writerows([
            {"account_name": "DanAcc", "buyer": "Dan Dicker"},
            {"account_name": "DanAcc2", "buyer": "Dan Dicker"},
            {"account_name": "GilAcc", "buyer": "Gil Zangi"},
        ])
    return map_path


def _select(name):
    from src.web.buyer import set_selected_cm

    set_selected_cm(name)


def _post(body):
    raw = json.dumps(body).encode()

    async def _receive():
        return {"type": "http.request", "body": raw, "more_body": False}

    scope = {"type": "http", "method": "POST", "path": "/account-links",
             "headers": [(b"content-type", b"application/json")]}
    return json.loads(asyncio.run(account_links_edit(Request(scope, receive=_receive))).body)


def _countries():
    view = _build_account_map_view({"countries": []})
    return {c["country"]: sorted(r["account"] for r in c["rows"]) for c in view["countries"]}


def test_other_buyers_country_is_hidden_then_added_for_me(env):
    links.link("DanAcc", "country", "Kuwait")
    _select("Gil Zangi")
    assert "Kuwait" not in _countries()

    assert _post({"action": "add", "kind": "country", "value": "Kuwait"}) == {"ok": True, "changed": True}

    shown = _countries()
    assert shown["Kuwait"] == [], "empty and ready to connect an account"
    assert "DanAcc" not in shown["Kuwait"]


def test_code_and_name_are_one_country(env):
    links.link("DanAcc", "country", "KW")
    _select("Gil Zangi")
    assert _post({"action": "add", "kind": "country", "value": "Kuwait"})["changed"] is True
    assert "Kuwait" in _countries() or "KW" in _countries()
    # и наоборот: хранится имя, добавляют код
    links.link("DanAcc2", "country", "Chile")
    assert _post({"action": "add", "kind": "country", "value": "CL"})["changed"] is True
    assert any(k in _countries() for k in ("CL", "Chile"))


def test_buyer_b_map_is_unchanged_and_nothing_is_moved(env):
    links.link("DanAcc", "country", "Kuwait")
    links.link("DanAcc2", "country", "Kuwait")
    _select("Dan Dicker")
    before = _countries()
    _select("Gil Zangi")
    _post({"action": "add", "kind": "country", "value": "Kuwait"})
    pairs = links.all_pairs("country")
    assert ("DanAcc", "Kuwait") in pairs and ("DanAcc2", "Kuwait") in pairs
    _select("Dan Dicker")
    assert _countries() == before


def test_closing_my_empty_country_does_not_free_other_buyers_accounts(env):
    links.link("DanAcc", "country", "Kuwait")
    _select("Gil Zangi")
    _post({"action": "add", "kind": "country", "value": "Kuwait"})
    out = _post({"action": "close", "kind": "country", "value": "Kuwait"})
    assert out["freed"] == []
    assert ("DanAcc", "Kuwait") in links.all_pairs("country")
    assert "Kuwait" not in _countries()


def test_a_press_that_changes_nothing_gives_a_reason(env):
    _select("Gil Zangi")
    links.link("GilAcc", "country", "Peru")
    out = _post({"action": "add", "kind": "country", "value": "Peru"})
    assert out["ok"] and out["changed"] is False
    assert "already on your account map" in out["reason"]
    # повторное добавление после успешного — тоже с причиной, а не тишина
    links.link("DanAcc", "country", "Kuwait")
    assert _post({"action": "add", "kind": "country", "value": "Kuwait"})["changed"] is True
    assert _post({"action": "add", "kind": "country", "value": "Kuwait"})["changed"] is False


def test_another_buyer_linking_an_account_keeps_my_empty_country(env):
    links.link("DanAcc", "country", "Kuwait")
    _select("Gil Zangi")
    _post({"action": "add", "kind": "country", "value": "Kuwait"})
    _post_link = links.link("DanAcc2", "country", "Kuwait", buyer="Dan Dicker")
    assert _post_link
    assert "Kuwait" in _countries()


def test_old_file_without_buyer_column_still_loads_and_stays_identical(env):
    links.link("GilAcc", "country", "Peru")
    text = links.links_path().read_text(encoding="utf-8")
    assert text.splitlines()[0] == "account_name,kind,value"
    assert links.load() == [{"account_name": "GilAcc", "kind": "country", "value": "Peru"}]


def test_template_shows_reason_instead_of_reloading():
    src = open("src/web/templates/report.html", encoding="utf-8").read()
    assert "data.changed === false && data.reason" in src


# ── Round 2 ───────────────────────────────────────────────────────────
def test_code_name_add_leaves_other_buyers_map_exactly_as_it_was(env):
    links.link("DanAcc", "country", "KW")
    _select("Dan Dicker")
    assert _countries() == {"KW": ["DanAcc"]} or _countries() == {"Kuwait": ["DanAcc"]}
    before = _countries()
    _select("Gil Zangi")
    assert _post({"action": "add", "kind": "country", "value": "Kuwait"})["changed"] is True
    assert "Kuwait" in _countries()
    _select("Dan Dicker")
    assert _countries() == before


def test_brand_new_country_of_a_stays_off_b_and_b_cannot_close_it(env):
    _select("Gil Zangi")
    _post({"action": "add", "kind": "country", "value": "Brazil"})
    assert "Brazil" in _countries()
    _select("Dan Dicker")
    assert "Brazil" not in _countries()
    out = _post({"action": "close", "kind": "country", "value": "Brazil", "confirm": True})
    assert out["changed"] is False and out["reason"]
    _select("Gil Zangi")
    assert "Brazil" in _countries()


def test_close_removes_my_placeholder(env):
    _select("Gil Zangi")
    _post({"action": "add", "kind": "country", "value": "Brazil"})
    out = _post({"action": "close", "kind": "country", "value": "Brazil", "confirm": True})
    assert out["changed"] is True
    assert "Brazil" not in _countries() and links.load() == []


def test_close_untagged_empty_country_as_on_main(env):
    links.add_value("country", "Peru")
    _select("Gil Zangi")
    assert "Peru" in _countries()
    assert _post({"action": "close", "kind": "country", "value": "Peru", "confirm": True})["changed"] is True
    assert links.load() == []


def test_similar_prefix_countries_are_not_the_same():
    assert not links._same_country("UAE", "Ukraine")
    assert not links._same_country("Guinea", "Guinea-Bissau")
    assert links._same_country("Kuwait", "KW") and links._same_country("KW", "kuwait")
    assert links._same_country("United Arab Emirates", "AE")


def test_adding_ukraine_when_uae_exists_adds_it(env):
    links.link("GilAcc", "country", "UAE")
    _select("Gil Zangi")
    assert _post({"action": "add", "kind": "country", "value": "Ukraine"})["changed"] is True


def test_default_buyer_writes_no_tag_and_no_column(env):
    _select("Owner")
    before = links.links_path().exists()
    assert _post({"action": "add", "kind": "country", "value": "Chile"})["changed"] is True
    assert links.links_path().read_text(encoding="utf-8").splitlines() == [
        "account_name,kind,value", ",country,Chile"]
    assert before is False
