"""Single source of truth for 'which rows belong to a given aggregate'.

Both the aggregators in `morning_report.py` AND the drilldown in
`morning_drilldown.py` import these. If the predicate ever changes (e.g.
country-name normalization, NaN handling), it changes in one place and
the displayed aggregate stays consistent with the drilled rows.

NA handling:
  pandas groupby(dropna=False) puts NaN into its own group. The display
  layer renders NaN as "(unknown)" / "(no title)" / "". To round-trip
  through a URL unambiguously we use the literal string `__NA__` as the
  sentinel for "match rows where this column is NaN or empty".
"""
from __future__ import annotations

from typing import Optional

import pandas as pd

NA_SENTINEL = "__NA__"


def _na_match(series: pd.Series, value: Optional[str]) -> pd.Series:
    """Return a boolean mask of rows where `series == value`, treating
    `__NA__` as 'NaN or empty/whitespace string'."""
    if value == NA_SENTINEL:
        coerced = series.astype(str).str.strip().str.lower()
        return series.isna() | coerced.isin(["", "nan", "none"])
    return series.astype(str).str.strip() == str(value).strip()


def filter_cm_rows(df: pd.DataFrame, country: str, cm: str) -> pd.DataFrame:
    """Rows aggregated by `build_cm_comparison(country, cm)`."""
    mask = (df["Country Final"] == country) & _na_match(df["CM"], cm)
    return df[mask]


def filter_trend_rows(
    df: pd.DataFrame,
    country: str,
    *,
    offer: str,
    device: str,
    interest: str,
    creative: str,
) -> pd.DataFrame:
    """Rows for one (offer, device, interest, creative) combo within country.

    `build_trend` groups by these 4 dimensions per country. The drill spans
    the FULL country period (not just the recent window) so the user can
    see what's behind both the recent and older deposits for the combo.
    """
    mask = (
        (df["Country Final"] == country)
        & _na_match(df["Offer"], offer)
        & _na_match(df["Device"], device)
        & _na_match(df["Interest"], interest)
        & _na_match(df["Creative"], creative)
    )
    return df[mask]


def filter_combo_rows(
    df: pd.DataFrame,
    country: str,
    *,
    offer: str,
    device: str,
    interest: str,
    age: str,
    creative: str,
    text_segment: str,
) -> pd.DataFrame:
    """Rows for one combo as defined by `aggregate_combos` (7-dim group)."""
    mask = (
        (df["Country Final"] == country)
        & _na_match(df["Offer"], offer)
        & _na_match(df["Device"], device)
        & _na_match(df["Interest"], interest)
        & _na_match(df["Age"], age)
        & _na_match(df["Creative"], creative)
        & _na_match(df["TextSegment"], text_segment)
    )
    return df[mask]


def filter_targeting_rows(df: pd.DataFrame, country: str, bucket: str) -> pd.DataFrame:
    """Rows aggregated by `build_targeting_breakdown` (groupby Interest)."""
    mask = (df["Country Final"] == country) & _na_match(df["Interest"], bucket)
    return df[mask]


def filter_creative_rows(df: pd.DataFrame, country: str, creative: str) -> pd.DataFrame:
    """Rows aggregated by `build_creative_breakdown` (groupby Ad Title)."""
    mask = (df["Country Final"] == country) & _na_match(df["Ad Title"], creative)
    return df[mask]
