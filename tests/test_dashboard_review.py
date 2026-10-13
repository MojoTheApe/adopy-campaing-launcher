"""Behavioral regressions for the header/theme/account-drill integration."""
from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

import pandas as pd
import pytest
from starlette.requests import Request

from src.web import account_links, app as web_app


@pytest.fixture
def isolated_links(monkeypatch, tmp_path):
    monkeypatch.setattr(account_links, "get_local_config_dir", lambda: str(tmp_path))
    monkeypatch.setattr(account_links, "get_ad_account_map_path", lambda: str(tmp_path / "missing.csv"))
    monkeypatch.setattr(web_app, "get_ad_account_map_path", lambda: str(tmp_path / "missing.csv"))
    monkeypatch.setattr(web_app, "_account_status", lambda label: {})


def _frame(first_alias="BookOneX01", second_alias="BookTwoX01"):
    return pd.DataFrame({
        "Country Final": ["Colombia", "Colombia"],
        "Account Alias": [first_alias, second_alias],
        "Date": ["2026-09-01", "2026-09-02"],
        "Adset Name": ["Older campaign", "Another account campaign"],
        "Ad Title": ["Older ad", "Another account ad"],
        "Ad Asset": ["demo", "demo"],
        "CM": ["Demo", "Demo"],
        "# FTDs": [0, 0], "# Leads": [1, 2], "$ Spend": [10.0, 20.0],
    })


def _details(monkeypatch, frame, account, section="account", extra=""):
    monkeypatch.setattr(web_app, "get_cached_report", lambda: ({}, frame, []))
    query = (f"section={section}&country=Colombia&account={account}"
             "&date_from=2026-09-02&date_to=2026-09-02" + extra)
    request = Request({"type": "http", "method": "GET", "path": "/details",
                       "query_string": query.encode(), "headers": []})
    return asyncio.run(web_app.details(request)).body.decode()


@pytest.mark.parametrize("first_alias", ["BookOneX01", "Linked_X01"])
def test_period_does_not_change_account_identity(isolated_links, monkeypatch, first_alias):
    """An older exact match or tail collision cannot disappear from resolution."""
    df = _frame(first_alias=first_alias)
    account_links.link("Linked_X01", "country", "CO")
    view = web_app._build_account_map_view({}, df, "2026-09-02", "2026-09-02")
    row = view["countries"][0]["rows"][0]
    assert not row["has_data"], "selected period borrowed another account's spend"
    assert row["workbook_alias"] == ""
    html = _details(monkeypatch, df, "Linked_X01")
    assert "No matching rows" in html
    assert "Another account campaign" not in html


def test_unique_account_drills_into_zero_deposit_ads_in_the_same_period(isolated_links, monkeypatch):
    df = _frame("BookOneX01", "BookOneX01")
    account_links.link("Linked_X01", "country", "CO")
    view = web_app._build_account_map_view({}, df, "2026-09-02", "2026-09-02")
    row = view["countries"][0]["rows"][0]
    assert row["workbook_alias"] == "BookOneX01"
    assert row["spend"] == 20.0 and row["deposits"] == 0
    html = _details(monkeypatch, df, "Linked_X01")
    assert "1 ad set" in html and "with deposits" not in html
    assert "Another account campaign" in html and "Older campaign" not in html
    assert "account=BookOneX01" in html
    nested = _details(monkeypatch, df, "BookOneX01", "optimization", "&adset=Another%20account%20campaign")
    assert "Another account ad" in nested and "Older ad" not in nested


def test_alias_resolution_does_not_borrow_from_another_country():
    df = _frame()
    df["Country Final"] = "Mexico"
    assert web_app.workbook_alias_for(df, "Colombia", "Linked_X01") == "Linked_X01"


@pytest.mark.parametrize("scenario", [
    "automatic", "media_change", "explicit", "unreadable_storage", "unwritable_storage",
])
def test_theme_preference_behavior(scenario):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for theme event behavior")
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [node, str(root / "tests/theme_ui_harness.cjs"),
         str(root / "src/web/templates/report.html"), scenario],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert result.returncode == 0, result.stdout + result.stderr


# ── ACL-1824: подпись «<имя>_<id кабинета>» и алиас книги ──────────────
def _id_frame(*aliases, country="Peru"):
    n = len(aliases)
    return pd.DataFrame({
        "Country Final": [country] * n,
        "Account Alias": list(aliases),
        "Date": ["2026-10-11"] * n,
        "Adset Name": [f"adset {i}" for i in range(n)],
        "Ad Title": [f"ad {i}" for i in range(n)],
        "Ad Asset": ["demo"] * n,
        "CM": ["Demo"] * n,
        "# FTDs": [1] * n, "# Leads": [6] * n, "$ Spend": [30.0] * n,
    })


def test_label_with_ad_account_id_suffix_matches_the_workbook_alias():
    df = _id_frame("Facebook_5265", "Facebook_5414")
    assert web_app.workbook_alias_for(df, "Peru", "Facebook_5265_61579362958966") == "Facebook_5265"
    assert web_app.workbook_alias_for(df, "Peru", "Facebook_5414_61579362958967") == "Facebook_5414"


def test_exact_alias_still_wins_over_the_suffix_rule():
    df = _id_frame("Facebook_5265", "Facebook_5265_61579362958966")
    assert web_app.workbook_alias_for(df, "Peru", "Facebook_5265_61579362958966") == "Facebook_5265_61579362958966"


def test_existing_tail_match_wins_over_the_id_suffix_rule():
    df = _id_frame("Acme", "Legacy678")
    assert web_app.workbook_alias_for(df, "Peru", "Acme_12345678") == "Legacy678"


def test_ambiguous_tail_does_not_fall_back_to_the_id_suffix():
    label = "Acme_12345678"
    df = _id_frame("Acme", "Legacy678", "Other678")
    assert web_app.workbook_alias_for(df, "Peru", label) == label


def test_explicit_name_rule_still_wins_over_tail_and_id_suffix():
    label = "KLD_BFL_A_12345678"
    exact = "FacebookMKTOBFLA_12345678"
    df = _id_frame("KLD_BFL_A", "Legacy678", exact)
    assert web_app.workbook_alias_for(df, "Peru", label) == exact


def test_explicit_country_link_keeps_tail_match_and_its_numbers(isolated_links):
    label = "Acme_12345678"
    linked = _id_frame("Acme", "Legacy678")
    linked.loc[1, ["$ Spend", "# Leads", "# FTDs"]] = [42.0, 5, 2]
    df = pd.concat([linked, _id_frame(label, country="Colombia")], ignore_index=True)
    account_links.link(label, "country", "Peru")

    view = web_app._build_account_map_view({}, df, "2026-10-11", "2026-10-11")
    assert len(view["countries"]) == 1
    country = view["countries"][0]
    row = country["rows"][0]
    assert row["account"] == label and row["countries"] == ["Peru"]
    assert row["workbook_alias"] == "Legacy678"
    assert row["has_data"] and row["spend"] == 42.0 and row["leads"] == 5 and row["deposits"] == 2
    assert country["spend"] == 42.0 and country["leads"] == 5 and country["deposits"] == 2


def test_two_candidate_aliases_give_no_match():
    # два разных алиаса под одним ключом — не выбираем первый
    label = "Facebook_5265_61579362958966"
    got = web_app._alias_without_id_suffix(label, ["Facebook_5265", "FACEBOOK_5265"])
    assert got == ""
    assert web_app.workbook_alias_for(_id_frame("Facebook_5265", "FACEBOOK_5265"), "Peru", label) == label


def test_short_numeric_suffix_is_not_stripped():
    df = _id_frame("Facebook_5265")
    assert web_app.workbook_alias_for(df, "Peru", "Facebook_5265_1234567") == "Facebook_5265_1234567"
    assert web_app.workbook_alias_for(df, "Peru", "Facebook_5265_12345678") == "Facebook_5265"


def test_id_suffix_rule_is_scoped_to_the_country():
    df = _id_frame("Facebook_5265", country="Peru")
    label = "Facebook_5265_61579362958966"
    assert web_app.workbook_alias_for(df, "Colombia", label) == label


def test_account_map_row_shows_numbers_for_suffixed_label(isolated_links):
    df = _id_frame("Facebook_5265")
    account_links.link("Facebook_5265_61579362958966", "country", "Peru")
    view = web_app._build_account_map_view({}, df, "2026-10-11", "2026-10-11")
    row = view["countries"][0]["rows"][0]
    assert row["workbook_alias"] == "Facebook_5265"
    assert row["has_data"] and row["leads"] == 6 and row["deposits"] == 1 and row["spend"] == 30.0
    assert view["countries"][0]["leads"] == 6


def test_suffixed_account_row_shows_elsewhere_for_its_resolved_alias(isolated_links):
    label = "Acme_12345678"
    older = _id_frame("Acme")
    older["Date"] = "2026-10-10"
    df = pd.concat([older, _id_frame("Acme", country="Colombia")], ignore_index=True)
    for account in (label, "Acme"):
        account_links.link(account, "country", "Peru")

    country = web_app._build_account_map_view({}, df, "2026-10-11", "2026-10-11")["countries"][0]
    rows = {row["account"]: row for row in country["rows"]}
    assert rows[label]["workbook_alias"] == "Acme"
    assert rows[label]["in_workbook"] and not rows[label]["has_data"]
    assert rows["Acme"]["elsewhere"] == {"Colombia": 30.0}
    assert rows[label]["elsewhere"] == {"Colombia": 30.0}


@pytest.mark.parametrize("aliases", [
    ("Unrelated",),
    ("Acme", "ACME"),
    ("Acme", "Legacy678", "Other678"),
], ids=["absent_from_linked_country", "ambiguous_suffix", "ambiguous_tail"])
def test_elsewhere_does_not_guess_an_unresolved_suffix_alias(isolated_links, aliases):
    label = "Acme_12345678"
    older = _id_frame(*aliases)
    older["Date"] = "2026-10-10"
    df = pd.concat([older, _id_frame("Acme", country="Colombia")], ignore_index=True)
    account_links.link(label, "country", "Peru")

    row = web_app._build_account_map_view({}, df, "2026-10-11", "2026-10-11")["countries"][0]["rows"][0]
    assert row["countries"] == ["Peru"]
    assert row["workbook_alias"] == "" and not row["has_data"]
    assert row["elsewhere"] == {}, "the other country must not supply a guessed alias"


def test_country_totals_do_not_double_count_a_linked_alias(isolated_links):
    df = _id_frame("Facebook_5265")
    account_links.link("Facebook_5265_61579362958966", "country", "Peru")
    account_links.link("Facebook_5265", "country", "Peru")
    view = web_app._build_account_map_view({}, df, "2026-10-11", "2026-10-11")
    country = view["countries"][0]
    assert len(country["rows"]) == 2 and all(r["has_data"] for r in country["rows"])
    assert country["leads"] == 6 and country["deposits"] == 1 and country["spend"] == 30.0


def test_country_totals_sum_two_different_aliases(isolated_links):
    df = _id_frame("Facebook_5265", "Facebook_5414")
    account_links.link("Facebook_5265_61579362958966", "country", "Peru")
    account_links.link("Facebook_5414_61579362958967", "country", "Peru")
    country = web_app._build_account_map_view({}, df, "2026-10-11", "2026-10-11")["countries"][0]
    assert all(r["has_data"] for r in country["rows"]) and len(country["rows"]) == 2
    assert country["leads"] == 12 and country["deposits"] == 2 and country["spend"] == 60.0


def test_country_totals_count_duplicate_once_and_keep_distinct(isolated_links):
    df = _id_frame("Facebook_5265", "Facebook_5414")
    for label in ("Facebook_5265_61579362958966", "Facebook_5265", "Facebook_5414_61579362958967"):
        account_links.link(label, "country", "Peru")
    country = web_app._build_account_map_view({}, df, "2026-10-11", "2026-10-11")["countries"][0]
    assert len(country["rows"]) == 3
    assert country["leads"] == 12 and country["deposits"] == 2 and country["spend"] == 60.0
