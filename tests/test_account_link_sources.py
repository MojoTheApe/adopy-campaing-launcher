"""Where an account's tracking link, pixel and domain come from.

Until 2026-09-01 the only source was the Accounts sheet of Upload FB.xlsx. An
account connected on the dashboard in one click therefore existed on the
machine but not in that workbook, and stayed unusable until someone edited the
spreadsheet by hand. The only symptom was a refusal to save the tracking link —
long after the account showed ad_account_id, profile_id and page name all set.

The rule now: **the workbook wins wherever it has a row**, because it is the
shared source of truth several people maintain. The machine's own account map
answers only where the workbook is silent, so a newly connected account works
immediately without anyone touching a spreadsheet.
"""
from __future__ import annotations

import csv

import pytest

from src.upload_agent.build_upload_excel import get_account_info


@pytest.fixture()
def machine_map(tmp_path, monkeypatch):
    """A machine account map holding one account with a link."""
    path = tmp_path / "ad_account_map.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "account_name", "ad_account_id", "profile_id",
                "tracking_link", "pixel_id", "domain",
            ],
        )
        w.writeheader()
        w.writerow({
            "account_name": "FacebookMKTOBFLW27 CR Ait",
            "ad_account_id": "act_1234567890123456",
            "profile_id": "550e8400-e29b-41d4-a716-446655440000",
            "tracking_link": "https://tkr.example.com/machine",
            "pixel_id": "111",
            "domain": "machine.example.com",
        })
    monkeypatch.setenv("META_AD_ACCOUNT_MAP_PATH", str(path))
    return path


def test_the_workbook_still_wins_where_it_has_the_account(machine_map):
    """The shared source stays authoritative; nothing changes for existing rows."""
    ref = {"accounts": {"FacebookMKTOBFLW27 CR Ait": {
        "link": "https://tkr.example.com/workbook", "pixel": "999", "domain": "book.example.com",
    }}}
    info = get_account_info(ref, "FacebookMKTOBFLW27 CR Ait")
    assert info["link"] == "https://tkr.example.com/workbook"
    assert info["pixel"] == "999"


def test_the_machine_map_answers_when_the_workbook_is_silent(machine_map):
    """The reported case: account fully configured here, absent from the workbook."""
    info = get_account_info({"accounts": {}}, "FacebookMKTOBFLW27 CR Ait")
    assert info["link"] == "https://tkr.example.com/machine"
    assert info["pixel"] == "111"
    assert info["domain"] == "machine.example.com"


def test_an_account_nobody_knows_still_returns_empty(machine_map):
    """Absent everywhere must stay empty, not fall back to some other account.

    Guessing here would attach one account's tracker to another's campaign —
    the same class of mistake the workbook's own ambiguity rule refuses to make.
    """
    info = get_account_info({"accounts": {}}, "FacebookNeverHeardOfIt XX")
    assert info == {"pixel": None, "domain": None, "link": None}


def test_partial_names_match_the_machine_map_too(machine_map):
    """The stats write the bare name; the map carries country and asset."""
    info = get_account_info({"accounts": {}}, "FacebookMKTOBFLW27")
    assert info["link"] == "https://tkr.example.com/machine"


def test_a_map_without_the_new_columns_is_not_a_crash(tmp_path, monkeypatch):
    """Older machines have a map with none of these columns."""
    path = tmp_path / "ad_account_map.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["account_name", "ad_account_id", "profile_id"])
        w.writeheader()
        w.writerow({
            "account_name": "FacebookOld CR Ait",
            "ad_account_id": "act_1234567890123456",
            "profile_id": "550e8400-e29b-41d4-a716-446655440000",
        })
    monkeypatch.setenv("META_AD_ACCOUNT_MAP_PATH", str(path))
    assert get_account_info({"accounts": {}}, "FacebookOld CR Ait") == {
        "pixel": None, "domain": None, "link": None,
    }
