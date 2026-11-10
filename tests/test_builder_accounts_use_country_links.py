"""Аккаунт, которому страну назначили на Accounts, должен быть в билдере.

Билдер строил список из листа и колонки `country` карты и не читал
account_links.csv — поэтому аккаунт с пустой колонкой, но со страной KW в
ссылках («FacebookDoc283 - Toby Keith») в билдере не появлялся ни в одной стране.
"""
from __future__ import annotations

import csv

import pytest

from src.analysis_agent import morning_service as ms
from src.web import account_links as links


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(links, "get_local_config_dir", lambda: str(tmp_path))
    monkeypatch.setattr(links, "get_ad_account_map_path", lambda: str(tmp_path / "ad_account_map.csv"))
    monkeypatch.setattr(ms, "get_ad_account_map_path", lambda: str(tmp_path / "ad_account_map.csv"))


def _map(tmp_path, rows):
    with open(tmp_path / "ad_account_map.csv", "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["account_name", "ad_account_id", "country"])
        w.writeheader()
        for name, country in rows:
            w.writerow({"account_name": name, "ad_account_id": "act_1", "country": country})


def _codes(rows, label):
    return sorted(r["country_code"] for r in rows if r["account_label"] == label)


def test_country_linked_on_accounts_page_reaches_the_builder(tmp_path):
    _map(tmp_path, [("FacebookDoc283 - Toby Keith", "")])
    links.link("FacebookDoc283 - Toby Keith", "country", "KW")
    assert _codes(ms._fallback_accounts_from_csv("cm"), "FacebookDoc283 - Toby Keith") == ["KW"]


def test_account_in_several_countries_gets_a_row_per_country(tmp_path):
    _map(tmp_path, [("KLD_BFL_B91", "")])
    links.link("KLD_BFL_B91", "country", "KW")
    links.link("KLD_BFL_B91", "country", "ecuador")
    assert _codes(ms._fallback_accounts_from_csv("cm"), "KLD_BFL_B91") == ["ECUADOR", "KW"]


def test_map_column_is_used_when_there_are_no_links(tmp_path):
    _map(tmp_path, [("KLD_BFL_B95", "cl")])
    assert _codes(ms._fallback_accounts_from_csv("cm"), "KLD_BFL_B95") == ["CL"]
