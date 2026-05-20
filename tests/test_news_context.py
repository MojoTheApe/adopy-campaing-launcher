from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from src.analysis_agent.morning_markdown import render_morning_report_md
from src.analysis_agent.morning_service import load_morning_report
from src.analysis_agent.news_context import (
    build_asset_queries,
    build_news_context,
    dedupe_results,
    merge_assets_for_news,
)


def test_build_asset_queries_include_hybrid_source_intents() -> None:
    queries = build_asset_queries("Amazon", "Mexico")

    assert "Amazon Mexico news last 7 days" in queries
    assert "Amazon stock shares earnings partnership regulation Mexico" in queries
    assert "Amazon investor relations press release" in queries
    assert "Amazon Mexico noticias acciones bolsa regulación" in queries


def test_build_asset_queries_ai_trading_pack() -> None:
    queries = build_asset_queries("AI trading", "Mexico", ai_trading=True)

    assert "AI trading artificial intelligence Mexico news last 7 days" in queries
    assert "retail AI trading apps brokers Mexico regulation news" in queries
    assert "AI trading Mexico noticias acciones bolsa regulación" in queries


def test_merge_assets_for_news_dedupes_ai_trader_spellings() -> None:
    merged = merge_assets_for_news(["AI_Trader", "AI Trader", "Trader AI", "Amazon"])
    assert len(merged) == 2
    by_display = {m[0]: (m[1], m[2]) for m in merged}
    assert by_display["AI Trader"] == ("AI trading", True)
    assert by_display["Amazon"][1] is False


def test_dedupe_results_prefers_first_normalized_url() -> None:
    rows = [
        {"title": "Amazon opens logistics hub", "url": "https://example.com/a?utm_source=x"},
        {"title": "Amazon opens logistics hub", "url": "https://example.com/a?utm_source=y"},
        {"title": "Amazon earnings rise", "url": "https://example.com/b"},
    ]

    deduped = dedupe_results(rows)

    assert [r["title"] for r in deduped] == [
        "Amazon opens logistics hub",
        "Amazon earnings rise",
    ]


def test_build_news_context_summarizes_mocked_asset_results() -> None:
    def fake_fetcher(query: str) -> list[dict]:
        if "Amazon Mexico" not in query:
            return []
        return [
            {
                "title": "Amazon Mexico expands same-day delivery",
                "snippet": "New logistics investment in Mexico City.",
                "url": "https://news.example/amazon-mx",
                "source": "Reuters",
                "published_at": "2026-05-12",
            }
        ]

    ref = datetime(2026, 5, 13, tzinfo=timezone.utc)
    ctx = build_news_context(
        "Mexico",
        ["Amazon"],
        fetcher=fake_fetcher,
        reference_now=ref,
    )

    assert ctx["country"] == "Mexico"
    assert ctx["items"][0]["asset"] == "Amazon"
    assert "Amazon Mexico expands same-day delivery" in ctx["items"][0]["summary"]
    assert ctx["items"][0]["confidence"] == "medium"
    assert ctx["items"][0]["freshness_days"] == 1.0


def test_build_news_context_prefers_fresh_article_over_stale_high_score() -> None:
    ref = datetime(2026, 5, 13, tzinfo=timezone.utc)

    def fake_fetcher(query: str) -> list[dict]:
        return [
            {
                "title": "Kuwait IPO trolley disrupts retail forever",
                "snippet": "",
                "url": "https://news.example/old-ip",
                "source": "ProCapitas",
                "published_at": "2024-05-01T12:00:00+00:00",
            },
            {
                "title": "Amazon Mexico expands same-day routes",
                "snippet": "",
                "url": "https://news.example/new-amz",
                "source": "Reuters",
                "published_at": "2026-05-11T12:00:00+00:00",
            },
        ]

    ctx = build_news_context(
        "Mexico",
        ["Amazon"],
        fetcher=fake_fetcher,
        reference_now=ref,
        max_news_age_days=7,
    )
    assert "Amazon Mexico expands same-day routes" in ctx["items"][0]["summary"]


def test_build_news_context_all_stale_yields_fallback() -> None:
    ref = datetime(2026, 5, 13, tzinfo=timezone.utc)

    def fake_fetcher(query: str) -> list[dict]:
        return [
            {
                "title": "Old Amazon Kuwait headline",
                "snippet": "",
                "url": "https://news.example/stale",
                "source": "Wire",
                "published_at": "2020-01-01T00:00:00+00:00",
            },
        ]

    ctx = build_news_context(
        "Mexico",
        ["Amazon"],
        fetcher=fake_fetcher,
        reference_now=ref,
    )
    assert ctx["items"][0]["summary"] == "No reliable recent asset news found."


def test_build_news_context_one_pass_for_duplicate_ai_trader_labels() -> None:
    seen_queries: list[str] = []

    def fake_fetcher(query: str) -> list[dict]:
        seen_queries.append(query)
        if "AI trading artificial intelligence Mexico" in query:
            return [
                {
                    "title": "Retail AI trading apps face new scrutiny in Mexico",
                    "snippet": "Regulators review broker marketing.",
                    "url": "https://news.example/ai-trade-mx",
                    "source": "Reuters",
                    "published_at": "2026-05-12",
                }
            ]
        return []

    ctx = build_news_context(
        "Mexico",
        ["AI_Trader", "AI Trader", "Trader AI"],
        fetcher=fake_fetcher,
        max_assets=3,
        max_queries_per_asset=2,
        reference_now=datetime(2026, 5, 13, tzinfo=timezone.utc),
    )

    assert len(ctx["items"]) == 1
    assert ctx["items"][0]["asset"] == "AI Trader"
    assert "Retail AI trading apps face new scrutiny" in ctx["items"][0]["summary"]
    assert sum("AI trading artificial intelligence Mexico" in q for q in seen_queries) == 1


def test_render_news_context_before_country_metrics() -> None:
    report = {
        "date": "2026-05-12",
        "source_excel": "fixture.xlsx",
        "my_cm": "Evgeni Sedin",
        "trend_days": 7,
        "total_combos": 0,
        "has_gsheet": False,
        "countries": [
            {
                "country": "Mexico",
                "news_context": {
                    "items": [
                        {
                            "asset": "Amazon",
                            "signal": "local business story",
                            "summary": "Amazon Mexico expands same-day delivery.",
                            "headline_angle": "Use faster access and convenience as the hook.",
                            "confidence": "medium",
                        }
                    ]
                },
                "cm_comparison": [],
                "asset_summary": [],
                "trend": {},
                "top_combos": [],
                "ai_context": "",
                "hypotheses": "",
                "account_rows": [],
                "close_recs": {},
                "open_recs": {},
            }
        ],
    }

    md = render_morning_report_md(report)

    assert md.index("### 0. News Context") < md.index("### 1. Evgeni vs Team")
    assert "**Amazon**" in md
    assert "- Signal: local business story" in md
    assert "Avoid:" not in md
    assert "Sources:" not in md


def test_load_morning_report_attaches_news_context_only_when_enabled(
    monkeypatch,
    tmp_path,
) -> None:
    df = pd.DataFrame(
        [
            {
                "Country": "Mexico",
                "Account Alias": "MX-acc-1",
                "Adset Name": "MX_Amazon_All_Image_PF_35",
                "Ad Title": "Image 1",
                "# Leads": 1,
                "# FTDs": 0,
                "$ Spend": 10,
                "CM": "Evgeni Sedin",
                "Ad Asset": "Amazon",
                "$ Net": 0,
            }
        ]
    )
    excel_path = tmp_path / "stats.xlsx"
    df.to_excel(excel_path, index=False)

    monkeypatch.setattr(
        "src.analysis_agent.morning_service.fetch_account_rows",
        lambda my_cm: [],
    )

    report_without_news, _, _ = load_morning_report(
        excel_path=str(excel_path),
        skip_gsheet=True,
    )
    assert "news_context" not in report_without_news["countries"][0]

    report_with_news, _, _ = load_morning_report(
        excel_path=str(excel_path),
        skip_gsheet=True,
        with_news=True,
        news_fetcher=lambda query: [],
    )
    assert report_with_news["countries"][0]["news_context"]["items"][0]["asset"] == "Amazon"
