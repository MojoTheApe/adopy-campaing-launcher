from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from src.analysis_agent.morning_markdown import render_morning_report_md
from src.analysis_agent.morning_service import load_morning_report
from src.analysis_agent.news_context import (
    build_asset_queries,
    build_news_context,
    build_thematic_queries,
    classify_news_asset,
    dedupe_results,
    extract_top_interests_for_offer,
    merge_assets_for_news,
)


def test_build_asset_queries_branded_pack() -> None:
    queries = build_asset_queries("Amazon", "Mexico", strategy="branded")

    assert queries[0] == "Amazon Mexico news last 7 days"
    assert "Amazon stock shares Mexico news last 7 days" in queries
    assert "Amazon earnings partnership regulation Mexico" in queries
    assert "Amazon Mexico noticias acciones bolsa regulación" in queries


def test_build_asset_queries_branded_includes_combo_interest_query() -> None:
    queries = build_asset_queries(
        "Amazon",
        "Mexico",
        strategy="branded",
        combo_interests=["SM"],
    )
    assert queries[0] == "Amazon Mexico news last 7 days"
    assert any("Stock Market" in q and "Mexico news last 7 days" in q for q in queries)


def test_build_news_context_amazon_mexico_finds_fresh_country_news() -> None:
    def fake_fetcher(query: str) -> list[dict]:
        if query == "Amazon Mexico news last 7 days":
            return [
                {
                    "title": "When is Amazon Prime Day 2026? Shop exclusive deals for Prime members",
                    "snippet": "",
                    "url": "https://news.example/amz-prime-mx",
                    "source": "Reuters",
                    "published_at": "2026-06-03",
                }
            ]
        return []

    combos = [
        {"offer": "Amazon", "interest": "SM", "deposits": 11, "spend": 11351.0},
        {"offer": "Amazon", "interest": "PF", "deposits": 6, "spend": 8261.0},
    ]
    ref = datetime(2026, 6, 7, tzinfo=timezone.utc)
    ctx = build_news_context(
        "Mexico",
        ["Amazon"],
        top_combos=combos,
        fetcher=fake_fetcher,
        reference_now=ref,
    )
    item = ctx["items"][0]
    assert "Prime Day 2026" in item["summary"]
    assert item["theme_label"] == "SM + PF"
    assert item["interest_source"] == "combo"
    assert item["query_basis"] == "top combos — SM (11 dep), PF (6 dep)"


def test_build_asset_queries_ai_trading_pack() -> None:
    queries = build_asset_queries("AI trading", "Mexico", strategy="ai_trading")

    assert "AI trading artificial intelligence Mexico news last 7 days" in queries
    assert "retail AI trading apps brokers Mexico regulation news" in queries
    assert "AI trading Mexico noticias acciones bolsa regulación" in queries


def test_build_thematic_queries_from_interests() -> None:
    queries = build_thematic_queries(["Day", "Trading"], "Guatemala")

    assert any("Guatemala news last 7 days" in q for q in queries)
    assert any("retail investors regulation Guatemala" in q for q in queries)
    assert any("noticias acciones bolsa" in q for q in queries)


def test_classify_trolley_as_thematic() -> None:
    plan = classify_news_asset("Trolley")
    assert plan.strategy == "thematic"
    assert plan.display_label == "Trolley"

    form_plan = classify_news_asset("Trolley Form")
    assert form_plan.strategy == "thematic"


def test_merge_assets_for_news_dedupes_ai_trader_spellings() -> None:
    merged = merge_assets_for_news(["AI_Trader", "AI Trader", "Trader AI", "Amazon"])
    assert len(merged) == 2
    by_display = {m.display_label: m for m in merged}
    assert by_display["AI Trader"].strategy == "ai_trading"
    assert by_display["Amazon"].strategy == "branded"


def test_extract_top_interests_for_offer() -> None:
    combos = [
        {"offer": "Trolley", "interest": "Day", "deposits": 7, "spend": 100},
        {"offer": "Trolley", "interest": "Trading", "deposits": 4, "spend": 200},
        {"offer": "Amazon", "interest": "HV", "deposits": 10, "spend": 50},
    ]
    rows = extract_top_interests_for_offer(combos, "Trolley")
    assert [r.interest for r in rows] == ["Day", "Trading"]


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
        if "Amazon stock shares Mexico" not in query:
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


def test_build_news_context_filters_betting_noise_for_branded() -> None:
    ref = datetime(2026, 6, 7, tzinfo=timezone.utc)

    def fake_fetcher(query: str) -> list[dict]:
        return [
            {
                "title": "Ecuador vs Guatemala Predictions & Betting Tips - Betfred Insights",
                "snippet": "",
                "url": "https://news.example/betting",
                "source": "Betfred",
                "published_at": "2026-06-07",
            },
            {
                "title": "Old Amazon Kuwait headline",
                "snippet": "",
                "url": "https://news.example/stale",
                "source": "Wire",
                "published_at": "2020-01-01T00:00:00+00:00",
            },
        ]

    ctx = build_news_context(
        "Guatemala",
        ["Amazon"],
        fetcher=fake_fetcher,
        reference_now=ref,
    )
    assert ctx["items"][0]["summary"] == "No reliable recent asset news found."


def test_build_news_context_trolley_uses_thematic_queries() -> None:
    seen_queries: list[str] = []

    def fake_fetcher(query: str) -> list[dict]:
        seen_queries.append(query)
        if "Guatemala news last 7 days" in query and "Day" in query:
            return [
                {
                    "title": "Guatemala retail day trading activity rises on local stock market",
                    "snippet": "",
                    "url": "https://news.example/day-gt",
                    "source": "Reuters",
                    "published_at": "2026-06-06",
                }
            ]
        return []

    combos = [
        {"offer": "Trolley", "interest": "Day", "deposits": 7, "spend": 177.0},
        {"offer": "Trolley", "interest": "Trading", "deposits": 4, "spend": 208.0},
    ]
    ref = datetime(2026, 6, 7, tzinfo=timezone.utc)
    ctx = build_news_context(
        "Guatemala",
        ["Trolley"],
        top_combos=combos,
        fetcher=fake_fetcher,
        reference_now=ref,
    )

    assert "trolley" not in " ".join(seen_queries).lower()
    assert ctx["items"][0]["asset"] == "Trolley"
    assert ctx["items"][0]["theme_label"] == "Day + Trading"
    assert "day trading activity rises" in ctx["items"][0]["summary"].lower()
    assert ctx["items"][0]["query_basis"] == "top combos — Day (7 dep), Trading (4 dep)"


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
                            "asset": "Trolley",
                            "theme_label": "Day + Trading",
                            "signal": "market move",
                            "summary": "Mexico retail day trading rises.",
                            "query_basis": "top combos — Day (7 dep), Trading (4 dep)",
                            "headline_angle": "Use day-trading momentum as the hook.",
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
    assert "**Trolley** _(theme: Day + Trading)_" in md
    assert "- Query basis: top combos — Day (7 dep), Trading (4 dep)" in md
    assert "- Signal: market move" in md
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
