"""Single source of truth for 'which rows belong to a given aggregate'.

Both the aggregators in `morning_report.py` AND the drilldown in
`morning_drilldown.py` import these. If the predicate ever changes (e.g.
country-name normalization, NaN handling), it changes in one place and
the displayed aggregate stays consistent with the drilled rows.

NA handling:
  pandas groupby(dropna=False) puts NaN into its own group. The display
  layer renders NaN as empty / "(no title)" / "". To round-trip
  through a URL unambiguously we use the literal string `__NA__` as the
  sentinel for "match rows where this column is NaN or empty".
"""
from __future__ import annotations

from typing import Any, Optional

import pandas as pd

from src.analysis_agent.analyze_fb_stats import alias_matches, split_ad_title

from src.knowledge_agent.knowledge_base import get_target_context, resolve_targeting_text

NA_SENTINEL = "__NA__"


def interest_bucket_key(raw: Any) -> str:
    """Stable targeting bucket for §4 aggregates (case + KB aliases).

    Uses resolve_targeting_text (Д4): compound-aware ("HV|Trading" buckets as
    the canonical compound, not raw-lowercase) + dictionary fallback for KB
    keys buried in parsing noise ("AAY_Day" → Day). Unresolvable values keep
    the old raw-lowercase bucket so nothing disappears from the report.
    """
    if raw is None:
        return NA_SENTINEL
    try:
        if pd.isna(raw):
            return NA_SENTINEL
    except (TypeError, ValueError):
        pass
    s = str(raw).strip()
    if not s or s.lower() in ("nan", "none"):
        return NA_SENTINEL
    canonical = resolve_targeting_text(s)
    if canonical:
        return canonical
    return s.lower()


def interest_bucket_keys(values: pd.Series) -> pd.Series:
    """`values.map(interest_bucket_key)`, но ключ считается раз на значение.

    Разбор таргета идёт через словарь KB и стоил ~6 с холодного открытия,
    потому что звался на каждую строку каждой страны (ACL-1380). Результат
    тот же: map остаётся map, меняется только число вызовов. Ключ памяти
    включает тип — 1 и "1" дают разные ведра и не должны склеиться.
    """
    memo: dict = {}

    def once(raw: Any) -> str:
        try:
            key = (type(raw), raw)
            hit = memo.get(key)
        except TypeError:  # нехэшируемое значение — просто посчитать
            return interest_bucket_key(raw)
        if hit is None:
            hit = memo[key] = interest_bucket_key(raw)
        return hit

    return values.map(once)


def interest_bucket_display(bucket_key: str) -> str:
    """Compact human label for a merged targeting bucket."""
    if bucket_key == NA_SENTINEL:
        return "(unknown)"
    if get_target_context(bucket_key):
        return bucket_key
    if bucket_key == bucket_key.lower() and bucket_key.isascii():
        return bucket_key.title()
    return bucket_key


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
    """Rows aggregated by `build_targeting_breakdown` (merged interest buckets)."""
    if "Interest" not in df.columns:
        return df.iloc[0:0]
    keys = interest_bucket_keys(df["Interest"])
    mask = (df["Country Final"] == country) & (keys == bucket)
    return df[mask]


def filter_creative_rows(
    df: pd.DataFrame, country: str, creative: str, text: str | None = None
) -> pd.DataFrame:
    """Rows behind one row of `build_creative_breakdown`.

    That breakdown groups by the parsed (creative, text) pair rather than the
    raw Ad Title, because the same ad is spelled several ways. So match the
    same way — an exact match on the cell would miss every spelling but one.
    `text=None` keeps the old behaviour for any caller still passing a title.
    """
    in_country = df["Country Final"] == country
    if text is None:
        return df[in_country & _na_match(df["Ad Title"], creative)]

    parsed = df["Ad Title"].apply(split_ad_title)
    mask = (
        in_country
        & (parsed.apply(lambda t: t[0]) == str(creative or "").strip())
        & (parsed.apply(lambda t: t[1]) == str(text or "").strip())
    )
    return df[mask]


def filter_account_rows(df: pd.DataFrame, country: str, account: str) -> pd.DataFrame:
    """Every row of one account in one country — the account map's drill.

    Same shape as the CM drill one level up: aggregated into campaigns and ad
    sets, each of which drills further into its ads. The account map asks the
    same question the CM comparison asks, just sliced by account instead of by
    person, so it reuses that path rather than inventing a second one.
    """
    mask = (df["Country Final"] == country) & alias_matches(df["Account Alias"], account)
    return df[mask]


def filter_optimization_rows(
    df: pd.DataFrame,
    country: str,
    account: str,
    adset: str,
) -> pd.DataFrame:
    """Ad-level rows for one ad set within a ready account (§9 drill-down)."""
    mask = (
        (df["Country Final"] == country)
        & alias_matches(df["Account Alias"], account)
        & _na_match(df["Adset Name"], adset)
    )
    rows = df[mask]
    if rows.empty or "$ Spend" not in rows.columns:
        return rows
    return rows.sort_values("$ Spend", ascending=False)
