"""GSheet col B ↔ Excel Country Final matching for morning report sections 6–9."""

from __future__ import annotations

import pandas as pd

from src.analysis_agent.analyze_fb_stats import compute_metrics, enrich_structure
from src.analysis_agent.morning_report import (
    build_morning_report,
    canonical_gsheet_country_code,
)


def test_canonical_gsheet_country_code_kw_and_kuwait() -> None:
    assert canonical_gsheet_country_code("KW") == "kw"
    assert canonical_gsheet_country_code("Kuwait") == "kw"


def test_canonical_gsheet_country_code_ae_maps_to_uae_slug() -> None:
    assert canonical_gsheet_country_code("AE") == "uae"
    assert canonical_gsheet_country_code("United Arab Emirates") == "uae"


def test_kuwait_section_gets_accounts_when_sheet_uses_full_country_name() -> None:
    df_raw = pd.DataFrame(
        [
            {
                "Country": "Kuwait",
                "Account Alias": "FacebookKW1",
                "Adset Name": "KW_Test_Adset",
                "Ad Title": "Creative A",
                "# Leads": 10,
                "# FTDs": 1,
                "$ Spend": 50.0,
                "CM": "Evgeni Sedin",
                "Ad Asset": "Amazon",
                "$ Net": 0,
            },
            {
                "Country": "Kuwait",
                "Account Alias": "FacebookKW2",
                "Adset Name": "KW_Other_Adset",
                "Ad Title": "Creative B",
                "# Leads": 5,
                "# FTDs": 0,
                "$ Spend": 40.0,
                "CM": "Evgeni Sedin",
                "Ad Asset": "Amazon",
                "$ Net": 0,
            },
        ]
    )
    df = compute_metrics(df_raw)
    df = enrich_structure(df)

    account_rows = [
        {
            "cm": "Evgeni Sedin",
            "country_code": "Kuwait",
            "account_label": "FacebookKW1 KW",
            "alias": "FacebookKW1",
            "cost": "",
            "leads": "",
            "ftds": "",
            "stock": "",
            "to_do": "",
            "to_do_extra": "",
            "is_ready": True,
        },
        {
            "cm": "Evgeni Sedin",
            "country_code": "Kuwait",
            "account_label": "FacebookKW2 KW",
            "alias": "FacebookKW2",
            "cost": "",
            "leads": "",
            "ftds": "",
            "stock": "",
            "to_do": "",
            "to_do_extra": "",
            "is_ready": True,
        },
    ]

    report = build_morning_report(
        df=df,
        account_rows=account_rows,
        my_cm="Evgeni Sedin",
        country_filter="Kuwait",
    )

    section = report["countries"][0]
    labels = {r["account_label"] for r in section["account_rows"]}
    assert labels == {"FacebookKW1 KW", "FacebookKW2 KW"}

    close_keys = set(section["close_recs"].keys())
    assert close_keys == {"FacebookKW1 KW", "FacebookKW2 KW"}
