"""Web drill-down: optimization-only in template and /details endpoint."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pandas as pd

from src.analysis_agent.analyze_fb_stats import aggregate_combos
from src.analysis_agent.morning_report import build_country_section
from src.web.app import _anchor_slug, details, templates

_MOCK_REPORT = {
    "date": "2026-06-08",
    "my_cm": "Evgeni Sedin",
    "source_excel": "test.xlsx",
    "trend_days": 7,
    "total_combos": 1,
    "countries": [
        {
            "country": "Mexico",
            "cm_comparison": [
                {
                    "cm": "Evgeni Sedin",
                    "deposits": 1,
                    "leads": 10,
                    "spend": 50.0,
                    "net": 30.0,
                    "cpa": 50.0,
                    "roi": 1.2,
                    "is_me": True,
                    "aggregate_key": {
                        "section": "cm_comparison",
                        "country": "Mexico",
                        "cm": "Evgeni Sedin",
                    },
                }
            ],
            "trend": {"has_date": False},
            "top_combos": [],
            "targeting": [],
            "creatives": [],
            "close_recs": {
                "MX-acc-1": [
                    {
                        "adset_name": "ME_test_adset",
                        "spend": 100.0,
                        "leads": 5,
                        "deposits": 0,
                        "cpl": 20.0,
                        "cpa": None,
                        "reason": "0 FTD",
                        "aggregate_key": {
                            "section": "optimization",
                            "country": "Mexico",
                            "account": "MX-acc-1",
                            "adset": "ME_test_adset",
                        },
                    }
                ]
            },
            "open_recs": {},
            "account_rows": [],
        }
    ],
}


def _render_report() -> str:
    return templates.env.get_template("report.html").render(
        request=MagicMock(),
        report=_MOCK_REPORT,
        warnings=[],
        built_at="2026-06-08",
    )


def _details_html(query: str) -> str:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/details",
        "query_string": query.encode(),
        "headers": [],
    }

    async def _run() -> str:
        from starlette.requests import Request

        request = Request(scope)
        response = await details(request)
        return response.body.decode()

    return asyncio.run(_run())


def test_report_template_has_drill_only_in_optimization() -> None:
    html = _render_report()

    assert 'data-drill="optimization"' in html
    assert html.count('class="drillable"') == 1
    assert "hx-get=\"/details?" not in html.split("9. Optimization")[0]


@patch("src.web.app.get_cached_report")
def test_details_rejects_non_optimization_sections(mock_cache) -> None:
    mock_cache.return_value = (_MOCK_REPORT, pd.DataFrame(), [])
    text = _details_html("section=cm_comparison&country=Mexico&cm=Evgeni+Sedin")

    assert "only available in §9 Optimization" in text
    assert "drill-table" not in text


@patch("src.web.app.get_cached_report")
@patch("src.web.app.get_underlying_rows")
def test_details_optimization_returns_fragment(mock_rows, mock_cache) -> None:
    mock_cache.return_value = (_MOCK_REPORT, pd.DataFrame(), [])
    mock_rows.return_value = [
        {
            "ad_title": "Ad A",
            "ad_asset": "trader_pro",
            "spend": 50.0,
            "leads": 10,
            "ftds": 1,
            "cpl": 5.0,
            "cpa": 50.0,
            "date": "2026-06-01",
        }
    ]
    text = _details_html(
        "section=optimization&country=Mexico&account=MX-acc-1&adset=ME_test"
    )

    assert text.count('class="drill-detail"') == 1
    assert "Ad A" in text


def test_optimization_drill_aggregate_keys_match_each_country(
    enriched_df: pd.DataFrame,
    accounts_rows: list,
) -> None:
    """Every country with close_recs gets drill rows scoped to that country."""
    combos = aggregate_combos(enriched_df)
    countries = sorted(enriched_df["Country Final"].dropna().unique().tolist())
    sections = [
        build_country_section(
            enriched_df, combos, country, accounts_rows,
            my_cm="Evgeni Sedin", trend_days=7,
        )
        for country in countries
    ]
    report = {
        "date": "2026-06-08",
        "my_cm": "Evgeni Sedin",
        "source_excel": "fixtures/synthetic.xlsx",
        "trend_days": 7,
        "total_combos": len(combos),
        "countries": sections,
    }
    html = templates.env.get_template("report.html").render(
        request=MagicMock(),
        report=report,
        warnings=[],
        built_at="2026-06-08",
    )

    countries_with_drill = 0
    for section in sections:
        country = section["country"]
        cid = _anchor_slug(country)
        close = section.get("close_recs") or {}
        drill_keys = [
            rec["aggregate_key"]
            for recs in close.values()
            for rec in recs
            if rec.get("aggregate_key")
        ]
        if not drill_keys:
            continue
        countries_with_drill += 1
        assert f'id="country-{cid}-optimization"' in html
        for key in drill_keys:
            assert key["section"] == "optimization"
            assert key["country"] == country
            assert "data-drill=\"optimization\"" in html
    assert countries_with_drill >= 1


def test_open_section_renders_account_adset_ad_titles() -> None:
    html = templates.env.get_template("report.html").render(
        request=MagicMock(),
        report={
            "date": "2026-06-08",
            "my_cm": "Evgeni Sedin",
            "source_excel": "test.xlsx",
            "trend_days": 7,
            "total_combos": 1,
            "countries": [
                {
                    "country": "Mexico",
                    "cm_comparison": [],
                    "trend": {"has_date": False},
                    "top_combos": [],
                    "targeting": [],
                    "creatives": [],
                    "close_recs": {},
                    "open_recs": {
                        "MX-acc-2": [
                            {
                                "strategy_type": 2,
                                "campaign_name": "MX_Trl_Ios_Images_trading_35+_08.06",
                                "creatives": [
                                    {"name": "Creative A"},
                                    {"name": "Creative B"},
                                    {"name": "Creative C"},
                                ],
                            }
                        ],
                        "MX-acc-1": [
                            {
                                "strategy_type": 1,
                                "campaign_name": "MX_Amz_All_Mix_trading_25_08.06",
                                "creatives": [
                                    {"name": "Ad title one"},
                                    {"name": "Ad title two"},
                                ],
                            }
                        ],
                    },
                    "account_rows": [],
                }
            ],
        },
        warnings=[],
        built_at="2026-06-08",
    )

    assert 'class="open-tree"' in html
    assert 'class="open-account-name">MX-acc-1</div>' in html
    assert 'class="open-account-name">MX-acc-2</div>' in html
    assert html.index("MX-acc-1") < html.index("MX-acc-2")
    assert "MX_Amz_All_Mix_trading_25_08.06" in html
    assert "MX_Trl_Ios_Images_trading_35+_08.06" in html
    assert html.count('class="open-creatives"') == 2
    assert "Ad title one" in html
    assert "Creative C" in html
    open_block = html.split("11. What to Open", 1)[1]
    assert "strategy_type" not in open_block
    assert "'campaign_name'" not in open_block
