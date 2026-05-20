"""Regression: optimization rows must include leads without matching spend rows."""

from __future__ import annotations

import pandas as pd

from src.analysis_agent.analyze_fb_stats import compute_metrics, enrich_structure
from src.analysis_agent.morning_report import build_close_recommendations


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
