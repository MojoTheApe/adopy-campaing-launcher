"""Regression: optimization rows must include leads without matching spend rows."""

from __future__ import annotations

import pandas as pd
import pytest

from src.analysis_agent.analyze_fb_stats import compute_metrics, enrich_structure
from src.analysis_agent.morning_report import (
    aggregate_adsets_merged_for_account,
    build_close_recommendations,
)


def test_close_recs_keeps_account_level_leads_with_zero_spend() -> None:
    df_raw = pd.DataFrame(
        [
            {
                "Country": "Kuwait",
                "Account Alias": "FacebookMKTO1323",
                "Adset Name": "-",
                "Ad Title": "—",
                "# Leads": 58,
                "# FTDs": 0,
                "$ Spend": 0.0,
                "CM": "Evgeni Sedin",
                "Ad Asset": "Amazon",
                "$ Net": 0,
            },
            {
                "Country": "Kuwait",
                "Account Alias": "FacebookMKTO1323",
                "Adset Name": "KW_ALG_IOS_Images_Day_30_07.05",
                "Ad Title": "c1",
                "# Leads": 0,
                "# FTDs": 0,
                "$ Spend": 400.0,
                "CM": "Evgeni Sedin",
                "Ad Asset": "Amazon",
                "$ Net": 0,
            },
        ]
    )
    df = enrich_structure(compute_metrics(df_raw))

    recs = build_close_recommendations(df, "Kuwait", "FacebookMKTO1323")

    by_adset = {r["adset_name"]: r for r in recs}
    assert "-" in by_adset
    assert by_adset["-"]["leads"] == 58
    assert by_adset["-"]["spend"] == 0.0
    assert by_adset["-"]["cpl"] is None
    assert by_adset["KW_ALG_IOS_Images_Day_30_07.05"]["leads"] == 0


def test_close_recs_merges_same_adset_split_by_offer() -> None:
    """Same Adset Name with different Offer/Ad Asset should be one summed row."""
    df_raw = pd.DataFrame(
        [
            {
                "Country": "Kuwait",
                "Account Alias": "Facebook_Account_Test",
                "Adset Name": "KW_Trl_All_Mix_trading_30_18.05",
                "Ad Title": "Ad A",
                "# Leads": 0,
                "# FTDs": 0,
                "$ Spend": 436.74,
                "CM": "Evgeni Sedin",
                "Ad Asset": "Trader AI",
                "$ Net": 0,
            },
            {
                "Country": "Kuwait",
                "Account Alias": "Facebook_Account_Test",
                "Adset Name": "KW_Trl_All_Mix_trading_30_18.05",
                "Ad Title": "Ad B",
                "# Leads": 2,
                "# FTDs": 0,
                "$ Spend": 0.0,
                "CM": "Evgeni Sedin",
                "Ad Asset": "Amazon",
                "$ Net": 0,
            },
            {
                "Country": "Kuwait",
                "Account Alias": "Facebook_Account_Test",
                "Adset Name": "KW_Trl_All_Mix_trading_30_18.05",
                "Ad Title": "Ad C",
                "# Leads": 2,
                "# FTDs": 1,
                "$ Spend": 0.0,
                "CM": "Evgeni Sedin",
                "Ad Asset": "Trader AI",
                "$ Net": 0,
            },
        ]
    )
    df = enrich_structure(compute_metrics(df_raw))

    recs = build_close_recommendations(df, "Kuwait", "Facebook_Account_Test")
    matching = [r for r in recs if r["adset_name"] == "KW_Trl_All_Mix_trading_30_18.05"]

    assert len(matching) == 1
    r = matching[0]
    assert r["spend"] == pytest.approx(436.74)
    assert r["leads"] == 4
    assert r["deposits"] == 1


def test_aggregate_adsets_merged_matches_close_recommendations_fixture() -> None:
    df_raw = pd.DataFrame(
        [
            {
                "Country": "Kuwait",
                "Account Alias": "Facebook_Account_Test",
                "Adset Name": "KW_Trl_All_Mix_trading_30_18.05",
                "Ad Title": "Ad A",
                "# Leads": 0,
                "# FTDs": 0,
                "$ Spend": 436.74,
                "CM": "Evgeni Sedin",
                "Ad Asset": "Trader AI",
                "$ Net": 0,
            },
            {
                "Country": "Kuwait",
                "Account Alias": "Facebook_Account_Test",
                "Adset Name": "KW_Trl_All_Mix_trading_30_18.05",
                "Ad Title": "Ad B",
                "# Leads": 2,
                "# FTDs": 0,
                "$ Spend": 0.0,
                "CM": "Evgeni Sedin",
                "Ad Asset": "Amazon",
                "$ Net": 0,
            },
        ]
    )
    df = enrich_structure(compute_metrics(df_raw))
    df_slice = df[
        (df["Country Final"] == "Kuwait")
        & (df["Account Alias"].astype(str) == "Facebook_Account_Test")
    ]
    g = aggregate_adsets_merged_for_account(df_slice)
    rows = g[g["Adset Name"].astype(str) == "KW_Trl_All_Mix_trading_30_18.05"]
    assert len(rows) == 1
    assert float(rows.iloc[0]["Spend"]) == pytest.approx(436.74)
    assert int(rows.iloc[0]["Leads"]) == 2
