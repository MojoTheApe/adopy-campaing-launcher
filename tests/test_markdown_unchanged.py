"""IRON RULE regression: morning_markdown output must not change.

This pins the existing Markdown contract. Adding `aggregate_key` to
report rows is a non-display field — the renderer reads specific keys
only and ignores it. Any future structural change in `morning_report.py`
or `morning_markdown.py` that affects output bytes must update the
snapshot deliberately, with a manual review.

The snapshot fixture file lives at:
    tests/fixtures/morning_baseline.md

If you genuinely intend to change the rendered output, regenerate the
snapshot with:
    UPDATE_MORNING_SNAPSHOT=1 python -m pytest tests/test_markdown_unchanged.py
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pandas as pd
import pytest

from src.analysis_agent.analyze_fb_stats import aggregate_combos
from src.analysis_agent.morning_markdown import render_morning_report_md
from src.analysis_agent.morning_report import build_country_section


SNAPSHOT_PATH = Path(__file__).parent / "fixtures" / "morning_baseline.md"


@pytest.fixture(autouse=True)
def _freeze_morning_report_clock_for_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    """`build_open_recommendations` embeds datetime.now() in campaign names — freeze for snapshots."""

    class _Now:
        def strftime(self, fmt: str) -> str:
            if fmt == "%d.%m":
                return "02.05"
            if fmt == "%Y-%m-%d":
                return "2026-04-18"
            raise AssertionError(f"unexpected strftime format: {fmt!r}")

    class _Datetime:
        @staticmethod
        def now():
            return _Now()

    monkeypatch.setattr("src.analysis_agent.morning_report.datetime", _Datetime)


def _build_report_from_fixture(
    enriched_df: pd.DataFrame, accounts_rows: list
) -> dict:
    """Build a MorningReport dict directly from an enriched fixture DataFrame.

    Bypasses build_morning_report() (which calls pd.read_excel) so the test
    is hermetic and deterministic across machines. The renderer doesn't
    care which path produced the dict — same shape, same output.
    """
    combos = aggregate_combos(enriched_df)
    countries = sorted(enriched_df["Country Final"].dropna().unique().tolist())
    country_sections = [
        build_country_section(
            enriched_df, combos, c, accounts_rows,
            my_cm="Evgeni Sedin", trend_days=7,
        )
        for c in countries
    ]
    return {
        "date": "2026-04-18",  # frozen for snapshot stability
        "source_excel": "fixtures/synthetic.xlsx",
        "my_cm": "Evgeni Sedin",
        "trend_days": 7,
        "countries": country_sections,
        "total_combos": len(combos),
        "has_gsheet": True,
    }


def test_aggregate_key_not_in_rendered_markdown(
    enriched_df: pd.DataFrame, accounts_rows: list
) -> None:
    """The new `aggregate_key` field on rows must not leak into Markdown.

    morning_markdown reads specific keys (cm, deposits, spend, etc.) and
    ignores anything else. If this assertion ever fails, the renderer
    started iterating over all keys and something is wrong.
    """
    report = _build_report_from_fixture(enriched_df, accounts_rows)
    md = render_morning_report_md(report)
    assert "aggregate_key" not in md
    assert "section=" not in md  # URL fragments must not appear either


def test_report_static_copy_is_english(
    enriched_df: pd.DataFrame, accounts_rows: list
) -> None:
    """Morning report labels should be English; source data stays unchanged."""
    report = _build_report_from_fixture(enriched_df, accounts_rows)
    md = render_morning_report_md(report)

    assert "### 1. Evgeni vs Team" in md
    assert "### 2. Trend (Recent Days)" in md
    assert "### 8. What to Close (Optimization)" in md
    assert "### 9. What to Open (2 Campaigns per Account)" in md

    old_russian_labels = [
        "команда",
        "Тренд",
        "Рабочие комбинации",
        "Контекст для AI",
        "Гипотезы",
        "Статусы аккаунтов",
        "Что закрыть",
        "Что открыть",
    ]
    for label in old_russian_labels:
        assert label not in md
    assert re.search(r"[А-Яа-яЁё]", md) is None


def test_close_recs_show_account_and_candidate_totals(
    enriched_df: pd.DataFrame, accounts_rows: list
) -> None:
    """Close tables should not look like full-account performance totals."""
    extra = enriched_df.iloc[0].copy()
    extra["Adset Name"] = "MX_zero_ftd_candidate"
    extra["Ad Title"] = "Zero FTD candidate"
    extra["Creative"] = "Zero FTD candidate"
    extra["# Leads"] = 10
    extra["# FTDs"] = 0
    extra["$ Spend"] = 200.0
    extra["$ Net"] = 0.0
    extra["CPL"] = 20.0
    extra["CPA"] = pd.NA
    extra["CR"] = 0.0
    extra["Profit"] = -200.0
    extra["ROI"] = 0.0
    df = pd.concat([enriched_df, pd.DataFrame([extra])], ignore_index=True)

    report = _build_report_from_fixture(df, accounts_rows)
    md = render_morning_report_md(report)

    assert "Account total: 280 leads, 2 FTDs, $335.00 spend" in md
    assert "Close candidates: 10 leads, 0 FTDs, $200.00 spend" in md
    assert "MX_zero_ftd_candidate" in md


def test_markdown_matches_snapshot(
    enriched_df: pd.DataFrame, accounts_rows: list
) -> None:
    """Byte-for-byte regression against tests/fixtures/morning_baseline.md.

    First run: creates the snapshot.
    Subsequent runs: diff against it.
    Set UPDATE_MORNING_SNAPSHOT=1 to regenerate after a deliberate change.
    """
    report = _build_report_from_fixture(enriched_df, accounts_rows)
    actual = render_morning_report_md(report)

    if os.environ.get("UPDATE_MORNING_SNAPSHOT") == "1" or not SNAPSHOT_PATH.exists():
        SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT_PATH.write_text(actual, encoding="utf-8")
        pytest.skip(f"Snapshot written to {SNAPSHOT_PATH}")

    expected = SNAPSHOT_PATH.read_text(encoding="utf-8")
    assert actual == expected, (
        f"Markdown drift detected. If intentional, run with "
        f"UPDATE_MORNING_SNAPSHOT=1 to refresh {SNAPSHOT_PATH}."
    )
