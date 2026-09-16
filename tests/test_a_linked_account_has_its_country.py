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

def test_the_sheet_and_the_links_are_joined_not_ranked():
    """Аккаунт, названный и там, и там, принадлежит обоим спискам.

    Несколько стран у одного аккаунта — обычное дело, а не исключение: ради
    этого и заведено пострановое чтение.
    """
    linked = linked_countries([("acc", "Kuwait")])
    out = resolve_countries({}, [sheet("acc", "Saudi Arabia")], linked)
    assert set(out["acc"]["countries"]) == {"Saudi Arabia", "Kuwait"}
    assert out["acc"]["ambiguous"] is True


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
