"""Where an account is used: one store, read by two screens.

The account map answers "which country is this campaign for" — one value per
publish. The dashboard asks "where is this account used", and that is genuinely
several countries at once. These tests hold the properties that keep the second
question from corrupting the first.
"""
from __future__ import annotations

import csv

import pytest

from src.web import account_links as links


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(links, "get_local_config_dir", lambda: str(tmp_path))
    monkeypatch.setattr(links, "get_ad_account_map_path", lambda: str(tmp_path / "ad_account_map.csv"))
    yield


def _account_map(tmp_path, rows: list[tuple[str, str]]) -> None:
    with open(tmp_path / "ad_account_map.csv", "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["account_name", "ad_account_id", "country"])
        writer.writeheader()
        for name, country in rows:
            writer.writerow({"account_name": name, "ad_account_id": "act_1", "country": country})


# ── the property the single column could not hold ──────────────────────────

def test_one_account_lives_in_several_countries_at_once():
    links.link("FacebookUL869", "country", "Chile")
    links.link("FacebookUL869", "country", "Peru")
    links.link("FacebookUL869", "asset", "Ait")

    assert links.values_for("FacebookUL869", "country") == ["Chile", "Peru"]
    assert links.values_for("FacebookUL869", "asset") == ["Ait"]
    assert links.accounts_for("country", "Peru") == ["FacebookUL869"]


def test_the_same_link_added_twice_is_one_link():
    assert links.link("Acc", "country", "Chile") is True
    assert links.link("Acc", "country", "chile") is False

    assert links.values_for("Acc", "country") == ["Chile"]


def test_removing_a_country_from_an_account_leaves_its_other_countries():
    links.link("Acc", "country", "Chile")
    links.link("Acc", "country", "Peru")

    assert links.unlink("Acc", "country", "Chile") is True

    assert links.values_for("Acc", "country") == ["Peru"]


# ── closing a country ──────────────────────────────────────────────────────

def test_closing_a_country_frees_its_accounts_instead_of_deleting_them():
    """They fall into Available accounts — still configured, just not placed."""
    links.link("A1", "country", "Chile")
    links.link("A2", "country", "Chile")
    links.link("A2", "country", "Peru")

    freed = links.unlink_all_for("country", "Chile")

    assert freed == ["A1", "A2"]
    assert links.accounts_for("country", "Chile") == []
    # A2 was also in Peru and stays there; only A1 is now unplaced.
    assert links.available_accounts(["A1", "A2"]) == ["A1"]


def test_an_account_with_an_asset_but_no_country_is_still_available():
    """Tagged is not placed. An account with nowhere to run must stay visible."""
    links.link("Acc", "asset", "Ait")

    assert links.available_accounts(["Acc"]) == ["Acc"]


# ── seeding, so a working install does not look empty ──────────────────────

def test_the_existing_country_column_is_carried_over_once(tmp_path):
    _account_map(tmp_path, [("A1", "Chile"), ("A2", "Peru"), ("A3", "")])

    assert links.seed_from_account_map() == 2
    assert links.accounts_for("country", "Chile") == ["A1"]
    assert links.available_accounts(["A1", "A2", "A3"]) == ["A3"]

    # Second call must not undo a link somebody removed on purpose.
    links.unlink("A1", "country", "Chile")
    assert links.seed_from_account_map() == 0
    assert links.accounts_for("country", "Chile") == []


def test_seeding_does_nothing_when_there_is_no_account_map():
    assert links.seed_from_account_map() == 0
    assert links.load() == []


# ── the store itself ───────────────────────────────────────────────────────

def test_an_unknown_kind_is_refused_rather_than_stored():
    with pytest.raises(ValueError):
        links.link("Acc", "planet", "Mars")


def test_rows_survive_a_reread_exactly():
    links.link("Acc One", "country", "Costa Rica")

    assert links.load() == [
        {"account_name": "Acc One", "kind": "country", "value": "Costa Rica"}
    ]
