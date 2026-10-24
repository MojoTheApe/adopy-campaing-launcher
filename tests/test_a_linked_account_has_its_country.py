# -*- coding: utf-8 -*-
"""Страна, выбранная оператором на дешборде, доходит до обзора (acl-1196).

16.09 на живой машине: `FacebookDoc19 - Eros Andersson` был подключён к Saudi
Arabia на дешборде, связь лежала на диске —

    account_links.csv:  FacebookDoc19 - Eros Andersson | country | Saudi Arabia

— а обзор аккаунтов показывал «страны нет». Дальше аккаунт падал в «Available
accounts», где у строки нет ни ячейки спенда, ни кнопки Read, то есть прочитать
его было нельзя никак.

Причина: страна собиралась из двух источников — раздел стран в общей таблице и
свежая статистика, — а связи не читались вовсе. Соседние Doc20 и Doc21
выглядели правильно **случайно**: у них была заполнена колонка `country` в
карте, и обзор брал её.

Порядок источников здесь важнее каждого из них, поэтому проверяется отдельно:
сказанное человеком (таблица и связи) говорит, где аккаунт **должен** быть;
статистика говорит, где он **был**, и только разрешает спор между названными
вариантами; суффикс в имени аккаунта источником не является никогда.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.analysis_agent.account_country import (  # noqa: E402
    linked_countries,
    primary_country,
    resolve_countries,
)


def sheet(alias: str, country: str, status: str = "") -> dict:
    return {"account_label": alias, "country_code": country, "account_status": status}


# ── то, что было сломано ──────────────────────────────────────────────────

def test_an_account_known_only_from_a_link_gets_its_country():
    """Ровно случай Doc19: в таблице нет, статистики нет, связь есть."""
    linked = linked_countries([("FacebookDoc19 - Eros Andersson", "Saudi Arabia")])
    out = resolve_countries({}, [], linked)
    single = primary_country(out)
    assert list(single.values()) == ["Saudi Arabia"]


def test_such_an_account_is_no_longer_country_less():
    """Пустая страна отправляла его в Available accounts — без кнопки Read."""
    linked = linked_countries([("FacebookDoc19", "Saudi Arabia")])
    out = resolve_countries({}, [], linked)
    assert all(v["country"] for v in out.values())


# ── порядок источников ────────────────────────────────────────────────────

def test_the_sheet_takes_priority_over_links_and_stats():
    linked = linked_countries([("acc", "Kuwait")])
    out = resolve_countries({"acc": "Kuwait"}, [sheet("acc", "Saudi Arabia")], linked)
    assert out["acc"] == {"country": "Saudi Arabia", "countries": ["Saudi Arabia"], "ambiguous": False}



def test_the_stats_only_break_a_tie_between_stated_options():
    """Статистика выбирает из названного, но сама ничего не добавляет."""
    linked = linked_countries([("acc", "Kuwait"), ("acc", "Saudi Arabia")])
    out = resolve_countries({"acc": "Kuwait"}, [], linked)
    assert out["acc"]["country"] == "Kuwait"
    assert set(out["acc"]["countries"]) == {"Kuwait", "Saudi Arabia"}


def test_stats_naming_something_nobody_stated_do_not_decide():
    """Придуманный ответ здесь наводит кампанию на чужой рынок."""
    linked = linked_countries([("acc", "Kuwait"), ("acc", "Saudi Arabia")])
    out = resolve_countries({"acc": "Chile"}, [], linked)
    assert out["acc"]["country"] == ""
    assert out["acc"]["ambiguous"] is True


def test_an_account_with_nothing_stated_still_says_nothing():
    """Без связи и без статистики страна остаётся пустой, а не угадывается."""
    out = resolve_countries({}, [], {})
    assert out == {}


# ── ничего из прежнего не сломано ─────────────────────────────────────────

def test_the_sheet_alone_still_works():
    out = resolve_countries({}, [sheet("acc", "Chile")], {})
    assert out["acc"]["country"] == "Chile"
    assert out["acc"]["ambiguous"] is False


def test_the_stats_alone_still_work():
    out = resolve_countries({"acc": "Panama"}, [], {})
    assert out["acc"]["country"] == "Panama"


def test_links_are_optional():
    """Старые вызовы с двумя аргументами обязаны работать как прежде."""
    out = resolve_countries({"acc": "Panama"}, [sheet("acc", "Chile")])
    assert out["acc"]["country"] == "Chile"


# ── разбор самих связей ───────────────────────────────────────────────────

def test_the_same_country_twice_is_listed_once():
    got = linked_countries([("acc", "Kuwait"), ("acc", "Kuwait")])
    assert got == {"acc": ["Kuwait"]}


@pytest.mark.parametrize("pair", [("", "Kuwait"), ("acc", ""), ("acc", "   ")])
def test_half_written_links_are_ignored(pair):
    assert linked_countries([pair]) == {}


def test_spacing_around_a_country_does_not_create_a_second_one():
    got = linked_countries([("acc", "Kuwait"), ("acc", "  Kuwait  ")])
    assert got == {"acc": ["Kuwait"]}


def test_country_codes_and_names_are_one_option():
    linked = linked_countries([("acc", "KW"), ("acc", "Kuwait"), ("acc", "Chile")])
    out = resolve_countries({"acc": "kuwait"}, [], linked)
    assert out["acc"] == {"country": "KW", "countries": ["KW", "Chile"], "ambiguous": True}


@pytest.fixture
def overview(monkeypatch, tmp_path):
    import asyncio, importlib, json
    from src.web import account_links as links
    from src.meta_campaign_agent.ad_account_map import AccountMapEntry
    web = importlib.import_module("src.web.app")
    path = tmp_path / "map.csv"
    path.touch()
    rows = []
    entries = {}
    monkeypatch.setattr(links, "get_local_config_dir", lambda: str(tmp_path))
    monkeypatch.setattr(links, "get_ad_account_map_path", lambda: str(path))
    monkeypatch.setattr(web, "get_ad_account_map_path", lambda: str(path))
    monkeypatch.setattr(web, "load_ad_account_map", lambda p: entries)
    monkeypatch.setattr(web, "get_cached_report", lambda: ({}, None, []))
    monkeypatch.setattr("src.analysis_agent.gsheet_accounts.fetch_account_rows", lambda: rows)
    monkeypatch.setattr("src.web.buyer.get_selected_cm", lambda: "")
    monkeypatch.setattr("src.fb_ads_rpa.mlx_profiles.open_profiles_detailed", lambda: [])
    monkeypatch.setattr(web, "_live_spend", lambda *a: {})
    monkeypatch.setattr(web, "_account_status", lambda *a: {})
    def read():
        return json.loads(asyncio.run(web.api_accounts_overview()).body)["accounts"]
    return read, rows, entries, links, web, AccountMapEntry


def test_overview_uses_links_and_never_legacy_map_or_label_suffix(overview):
    read, rows, entries, links, web, Entry = overview
    entries["FacebookReviewA"] = Entry(ad_account_id="", country="Chile")
    entries["FacebookReviewB PA"] = Entry(ad_account_id="", country="Panama")
    links.link("FacebookReviewA", "country", "Saudi Arabia")
    links.link("FacebookReviewA", "country", "Kuwait")
    accounts = {a["label"]: a for a in read()}
    a = accounts["FacebookReviewA"]
    assert set(a["countries"]) == {"Saudi Arabia", "Kuwait"}
    assert a["country"] == a["country_name"] == ""
    assert a["country_ambiguous"] is True
    unknown = accounts["FacebookReviewB PA"]
    assert unknown["country"] == unknown["country_name"] == ""
    assert unknown["countries"] == []


def test_overview_preserves_sheet_priority_and_ambiguous_plural_rows(overview):
    read, rows, entries, links, web, Entry = overview
    rows.extend([sheet("FacebookReviewA", "Chile"), sheet("FacebookReviewA", "Panama")])
    links.link("FacebookReviewA", "country", "Kuwait")
    accounts = read()
    assert len(accounts) == 2
    for a in accounts:
        assert a["countries"] == ["Chile", "Panama"]
        assert a["country"] == a["country_name"] == ""
        assert a["country_ambiguous"] is True


def test_linked_local_account_has_country_spend_cell_and_read_control(overview):
    read, rows, entries, links, web, Entry = overview
    entries["FacebookReviewA"] = Entry(ad_account_id="")
    links.link("FacebookReviewA", "country", "Saudi Arabia")
    account = read()[0]
    assert account["country"] == account["country_name"] == "Saudi Arabia"
    assert account["countries"] == ["Saudi Arabia"]
    view = web._build_account_map_view({}, date_from="2026-09-16", date_to="2026-09-16")
    country = view["countries"][0]
    assert country["rows"][0]["account"] == "FacebookReviewA"
    rendered = web.templates.get_template("_account_map_country.html").render(mc=country, amap=view)
    assert "FacebookReviewA" in rendered
    assert "Campaign list" in rendered
    assert 'onclick="amapReadSpend(this)"' not in rendered
    assert 'class="amap-list"' in rendered
    assert '>Read spend</button>' not in rendered
