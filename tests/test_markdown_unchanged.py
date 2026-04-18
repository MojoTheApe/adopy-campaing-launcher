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
from pathlib import Path

import pandas as pd
import pytest

from src.analysis_agent.analyze_fb_stats import aggregate_combos
from src.analysis_agent.morning_markdown import render_morning_report_md
from src.analysis_agent.morning_report import build_country_section


SNAPSHOT_PATH = Path(__file__).parent / "fixtures" / "morning_baseline.md"


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
