"""USD formatting: ceil, no cents, dollar prefix."""
from __future__ import annotations

import math

from src.analysis_agent.money_format import fmt_usd


def test_fmt_usd_rounds_up_and_drops_cents() -> None:
    assert fmt_usd(50.0) == "$50"
    assert fmt_usd(50.1) == "$51"
    assert fmt_usd(1234.01) == "$1,235"


def test_fmt_usd_handles_empty_and_none() -> None:
    assert fmt_usd(None) == "—"
    assert fmt_usd("") == "—"
    assert fmt_usd("—") == "—"


def test_fmt_usd_parses_string_amounts() -> None:
    assert fmt_usd("100") == "$100"
    assert fmt_usd("$2,500.40") == "$2,501"


def test_fmt_usd_nan() -> None:
    assert fmt_usd(float("nan")) == "—"
