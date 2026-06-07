"""Tests for section_filters — the single source of truth for 'which rows
belong to a given aggregate' across morning_report.py and morning_drilldown.py.

For each filter, we assert two properties:
  1. Row-set equivalence with the corresponding aggregator's predicate
     (the same mask applied two ways should select the same rows).
  2. Edge cases: NaN dimension values, unknown values, empty results.
"""
from __future__ import annotations

import pandas as pd
import pytest

from src.analysis_agent.section_filters import (
    NA_SENTINEL,
    filter_cm_rows,
    filter_combo_rows,
    filter_creative_rows,
    filter_targeting_rows,
    filter_trend_rows,
)


# ── filter_cm_rows ─────────────────────────────────────────────────────────────

def test_filter_cm_rows_returns_country_cm_match(enriched_df: pd.DataFrame) -> None:
    rows = filter_cm_rows(enriched_df, "Mexico", "Evgeni Sedin")
    assert len(rows) == 4  # 3 trader_pro all + 1 trader_pro ios
    assert (rows["Country Final"] == "Mexico").all()
    assert (rows["CM"] == "Evgeni Sedin").all()


def test_filter_cm_rows_unknown_cm_empty(enriched_df: pd.DataFrame) -> None:
    rows = filter_cm_rows(enriched_df, "Mexico", "Nobody")
    assert len(rows) == 0


def test_filter_cm_rows_unknown_country_empty(enriched_df: pd.DataFrame) -> None:
    rows = filter_cm_rows(enriched_df, "Atlantis", "Evgeni Sedin")
    assert len(rows) == 0


def test_filter_cm_rows_nan_cm_via_sentinel(enriched_df: pd.DataFrame) -> None:
    rows = filter_cm_rows(enriched_df, "Mexico", NA_SENTINEL)
    assert len(rows) == 1
    assert rows.iloc[0]["Account Alias"] == "MX-acc-4"


def test_filter_cm_rows_matches_aggregator_total(enriched_df: pd.DataFrame) -> None:
    """Sum of $ Spend across filtered rows equals what build_cm_comparison aggregates."""
    from src.analysis_agent.morning_report import build_cm_comparison
    aggs = build_cm_comparison(enriched_df, "Mexico", "Evgeni Sedin")
    me = next(a for a in aggs if a["cm"] == "Evgeni Sedin")
    rows = filter_cm_rows(enriched_df, "Mexico", "Evgeni Sedin")
    assert rows["$ Spend"].sum() == pytest.approx(me["spend"])
    assert int(rows["# FTDs"].sum()) == me["deposits"]


# ── filter_trend_rows ──────────────────────────────────────────────────────────

def test_filter_trend_rows_matches_combo(enriched_df: pd.DataFrame) -> None:
    rows = filter_trend_rows(
        enriched_df, "Mexico", offer="trader_pro", device="all",
        interest="trading", creative="Make money fast",
    )
    assert len(rows) == 3  # all 3 trader_pro/all/trading rows for that ad title
    assert (rows["Country Final"] == "Mexico").all()


def test_filter_trend_rows_unknown_combo_empty(enriched_df: pd.DataFrame) -> None:
    rows = filter_trend_rows(
        enriched_df, "Mexico", offer="nope", device="all",
        interest="trading", creative="Make money fast",
    )
    assert len(rows) == 0


# ── filter_combo_rows ──────────────────────────────────────────────────────────

def test_filter_combo_rows_full_tuple(enriched_df: pd.DataFrame) -> None:
    rows = filter_combo_rows(
        enriched_df, "Mexico",
        offer="trader_pro", device="all", interest="trading",
        age="25", creative="Make money fast", text_segment="make",
    )
    assert len(rows) == 3
    assert (rows["Country Final"] == "Mexico").all()
    assert (rows["Age"] == "25").all()


def test_filter_combo_rows_text_segment_disambiguates(
    enriched_df: pd.DataFrame,
) -> None:
    """Two combo rows that differ only in text_segment should not collide.

    The Mexico/Other CM/crypto_invest/all/crypto/25 combo has TWO ad titles
    ('Buy bitcoin now' and 'Crypto for beginners') with different first-words.
    Filtering by text_segment must separate them.
    """
    bitcoin = filter_combo_rows(
        enriched_df, "Mexico",
        offer="crypto_invest", device="all", interest="crypto",
        age="25", creative="Buy bitcoin now", text_segment="buy",
    )
    crypto = filter_combo_rows(
        enriched_df, "Mexico",
        offer="crypto_invest", device="all", interest="crypto",
        age="25", creative="Crypto for beginners", text_segment="crypto",
    )
    assert len(bitcoin) == 3
    assert len(crypto) == 1
    assert set(bitcoin["Ad Title"].unique()) == {"Buy bitcoin now"}
    assert set(crypto["Ad Title"].unique()) == {"Crypto for beginners"}


# ── filter_targeting_rows ──────────────────────────────────────────────────────

def test_filter_targeting_rows_by_interest(enriched_df: pd.DataFrame) -> None:
    rows = filter_targeting_rows(enriched_df, "Mexico", "trading")
    # Mexico/trading: 3 (Evgeni/trader_pro/all) + 1 (Evgeni/trader_pro/ios) = 4
    assert len(rows) == 4
    assert (rows["Interest"] == "trading").all()


def test_filter_targeting_rows_unknown_bucket(enriched_df: pd.DataFrame) -> None:
    """The NaN/(unknown) bucket must be reachable via the sentinel."""
    rows = filter_targeting_rows(enriched_df, "Mexico", NA_SENTINEL)
    assert len(rows) == 1
    assert rows.iloc[0]["Account Alias"] == "MX-acc-4"


def test_filter_targeting_rows_matches_aggregator(
    enriched_df: pd.DataFrame,
) -> None:
    from src.analysis_agent.morning_report import build_targeting_breakdown
    aggs = build_targeting_breakdown(enriched_df, "Mexico")
    crypto_agg = next(a for a in aggs if a["interest"] == "crypto")
    crypto_rows = filter_targeting_rows(enriched_df, "Mexico", "crypto")
    assert int(crypto_rows["# FTDs"].sum()) == crypto_agg["deposits"]
    assert crypto_rows["$ Spend"].sum() == pytest.approx(crypto_agg["spend"])


# ── filter_creative_rows ───────────────────────────────────────────────────────

def test_filter_creative_rows_by_ad_title(enriched_df: pd.DataFrame) -> None:
    rows = filter_creative_rows(enriched_df, "Mexico", "Make money fast")
    assert len(rows) == 3
    assert (rows["Ad Title"] == "Make money fast").all()


def test_filter_creative_rows_unknown_creative_empty(
    enriched_df: pd.DataFrame,
) -> None:
    rows = filter_creative_rows(enriched_df, "Mexico", "Nonexistent ad")
    assert len(rows) == 0


def test_filter_creative_rows_matches_aggregator(
    enriched_df: pd.DataFrame,
) -> None:
    from src.analysis_agent.morning_report import build_creative_breakdown
    aggs = build_creative_breakdown(enriched_df, "Mexico")
    target = next(a for a in aggs if a["ad_title"] == "Buy bitcoin now")
    rows = filter_creative_rows(enriched_df, "Mexico", "Buy bitcoin now")
    assert int(rows["# FTDs"].sum()) == target["deposits"]
    assert rows["$ Spend"].sum() == pytest.approx(target["spend"])


# ── filter_optimization_rows ───────────────────────────────────────────────────

def test_filter_optimization_rows_returns_ad_level_rows(enriched_df: pd.DataFrame) -> None:
    from src.analysis_agent.section_filters import filter_optimization_rows

    adset = "ME_trader_pro_all_creative_trading_25"
    rows = filter_optimization_rows(enriched_df, "Mexico", "MX-acc-1", adset)
    assert len(rows) == 3
    assert (rows["Adset Name"] == adset).all()
    assert list(rows["$ Spend"]) == sorted(rows["$ Spend"], reverse=True)


def test_filter_optimization_rows_unknown_adset_empty(enriched_df: pd.DataFrame) -> None:
    from src.analysis_agent.section_filters import filter_optimization_rows

    rows = filter_optimization_rows(enriched_df, "Mexico", "MX-acc-1", "nonexistent")
    assert len(rows) == 0
