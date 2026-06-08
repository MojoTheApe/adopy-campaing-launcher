"""Tests for morning_drilldown.get_underlying_rows.

Verifies dispatch to the right section_filters function, fixed return
column set, and graceful handling of unknown sections / no-match queries.
"""
from __future__ import annotations

import pandas as pd
import pytest

from src.analysis_agent.morning_drilldown import (
    DRILL_COLUMNS,
    get_underlying_rows,
)

# ── dispatch ───────────────────────────────────────────────────────────────────

def test_cm_section_returns_rows(enriched_df: pd.DataFrame) -> None:
    rows = get_underlying_rows(
        enriched_df, section="cm_comparison",
        country="Mexico", cm="Evgeni Sedin",
    )
    assert len(rows) == 4


def test_trend_section_returns_rows(enriched_df: pd.DataFrame) -> None:
    rows = get_underlying_rows(
        enriched_df, section="trend",
        country="Mexico", offer="trader_pro", device="all",
        interest="trading", creative="Make money fast",
    )
    assert len(rows) == 3


def test_top_combos_section_returns_rows(enriched_df: pd.DataFrame) -> None:
    rows = get_underlying_rows(
        enriched_df, section="top_combos",
        country="Mexico", offer="trader_pro", device="all",
        interest="trading", age="25", creative="Make money fast",
        text_segment="make",
    )
    assert len(rows) == 3


def test_targeting_section_returns_rows(enriched_df: pd.DataFrame) -> None:
    rows = get_underlying_rows(
        enriched_df, section="targeting",
        country="Mexico", bucket="Trading",
    )
    assert len(rows) == 4


def test_creatives_section_returns_rows(enriched_df: pd.DataFrame) -> None:
    rows = get_underlying_rows(
        enriched_df, section="creatives",
        country="Mexico", creative="Make money fast",
    )
    assert len(rows) == 3


def test_optimization_section_returns_rows_sorted_by_spend(enriched_df: pd.DataFrame) -> None:
    rows = get_underlying_rows(
        enriched_df,
        section="optimization",
        country="Mexico",
        account="MX-acc-1",
        adset="ME_trader_pro_all_creative_trading_25",
    )
    # Three Excel lines share one Ad Title — drill shows one aggregated row.
    assert len(rows) == 1
    assert rows[0]["ad_title"] == "Make money fast"
    assert rows[0]["spend"] == pytest.approx(135.0)
    assert rows[0]["leads"] == 270
    assert rows[0]["ftds"] == 2


def test_optimization_drill_dedupes_same_ad_title(enriched_df: pd.DataFrame) -> None:
    rows = get_underlying_rows(
        enriched_df,
        section="optimization",
        country="Mexico",
        account="MX-acc-3",
        adset="ME_crypto_invest_all_creative_crypto_25",
    )
    titles = [r["ad_title"] for r in rows]
    assert len(titles) == len(set(titles))
    assert "Buy bitcoin now" in titles


# ── return shape ───────────────────────────────────────────────────────────────

def test_return_columns_are_fixed(enriched_df: pd.DataFrame) -> None:
    rows = get_underlying_rows(
        enriched_df, section="cm_comparison",
        country="Mexico", cm="Evgeni Sedin",
    )
    assert rows
    for r in rows:
        assert set(r.keys()) == set(DRILL_COLUMNS)


def test_return_is_list_of_dicts(enriched_df: pd.DataFrame) -> None:
    rows = get_underlying_rows(
        enriched_df, section="cm_comparison",
        country="Mexico", cm="Evgeni Sedin",
    )
    assert isinstance(rows, list)
    assert all(isinstance(r, dict) for r in rows)


# ── graceful failure ───────────────────────────────────────────────────────────

def test_unknown_section_returns_empty(enriched_df: pd.DataFrame) -> None:
    rows = get_underlying_rows(
        enriched_df, section="not_a_section",
        country="Mexico",
    )
    assert rows == []


def test_no_match_returns_empty(enriched_df: pd.DataFrame) -> None:
    rows = get_underlying_rows(
        enriched_df, section="cm_comparison",
        country="Mexico", cm="Nobody",
    )
    assert rows == []


def test_missing_required_kwarg_returns_empty(enriched_df: pd.DataFrame) -> None:
    """Drill URLs come from user clicks; missing params shouldn't 500."""
    rows = get_underlying_rows(
        enriched_df, section="cm_comparison",
        country="Mexico",  # 'cm' missing
    )
    assert rows == []


# ── values are denormalized scalars (JSON-friendly) ────────────────────────────

def test_no_pandas_native_types_in_output(enriched_df: pd.DataFrame) -> None:
    """Templates render via Jinja2; pandas types (NaT, NaN, Timestamp) leak
    badly through Jinja2 filters. Output should be Python native types."""
    rows = get_underlying_rows(
        enriched_df, section="cm_comparison",
        country="Mexico", cm="Evgeni Sedin",
    )
    for r in rows:
        for v in r.values():
            assert v is None or isinstance(v, (str, int, float)), (
                f"non-native type in drill output: {type(v).__name__}"
            )
