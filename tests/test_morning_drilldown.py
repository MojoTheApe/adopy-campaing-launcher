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

def test_cm_section_returns_one_row_per_account_and_adset(enriched_df: pd.DataFrame) -> None:
    """Identity is the PAIR, not the ad-set name.

    This asserted 4 rows back when grouping was by ad-set name alone. That
    grouping was wrong and was fixed on 2026-08-17: the naming convention
    repeats ad-set names across accounts, so one account's deposits were being
    reported under another's. The fixture holds two (account, ad-set) pairs for
    this CM, so two rows is the correct answer — counting rows was never the
    point, so assert the property instead of the number.
    """
    rows = get_underlying_rows(
        enriched_df, section="cm_comparison",
        country="Mexico", cm="Evgeni Sedin",
    )
    keys = [(r["account"], r["adset"]) for r in rows]
    assert len(keys) == len(set(keys)), "одна пара аккаунт+адсет попала в дрилл дважды"
    assert len(rows) == 2
    assert all(r["ftds"] > 0 for r in rows), "дрилл показывает только строки с депозитами"


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
    def _native(v) -> bool:
        # `variants` is a list of small dicts, which Jinja2 handles fine. The
        # point of this test is that pandas/numpy scalars (NaT, NaN, Timestamp,
        # int64) never reach a template — so look inside containers rather than
        # rejecting them.
        if v is None or isinstance(v, (str, int, float, bool)):
            return True
        if isinstance(v, (list, tuple)):
            return all(_native(x) for x in v)
        if isinstance(v, dict):
            return all(_native(x) for x in v.values())
        return False

    for r in rows:
        for key, v in r.items():
            assert _native(v), f"non-native type in drill output: {key}={type(v).__name__}"
