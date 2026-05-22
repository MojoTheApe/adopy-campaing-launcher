"""§10 open recommendations must follow §2 trend buckets (4 strategies)."""
from __future__ import annotations

from datetime import datetime

import pandas as pd

from src.analysis_agent.morning_report import (
    OPEN_STRATEGY_LABELS,
    build_open_recommendations,
    open_recs_for_status_table,
)


def _ecuador_trend() -> dict:
    return {
        "has_date": True,
        "period_label": "May 13 – May 19, 2026 (7d)",
        "trending_up": [
            {
                "offer": "AI Trader",
                "device": "all",
                "interest": "Day",
                "creative": "2046 - Text 4",
                "deposits_total": 1,
                "deposits_recent": 1,
                "aggregate_key": {
                    "creative": "2046 - Text 4",
                    "offer": "AI Trader",
                    "device": "all",
                    "interest": "Day",
                },
            },
            {
                "offer": "AI Trader",
                "device": "all",
                "interest": "TRD",
                "creative": "2041_1_1_OneNight",
                "deposits_total": 1,
                "deposits_recent": 1,
                "aggregate_key": {
                    "creative": "2041_1_1_OneNight",
                    "offer": "AI Trader",
                    "device": "all",
                    "interest": "TRD",
                },
            },
            {
                "offer": "Favorita",
                "device": "all",
                "interest": "Tradingz",
                "creative": "Vid 3 - pro -3",
                "deposits_total": 1,
                "deposits_recent": 1,
                "aggregate_key": {
                    "creative": "Vid 3 - pro -3",
                    "offer": "Favorita",
                    "device": "all",
                    "interest": "Tradingz",
                },
            },
        ],
        "trending_down": [
            {
                "offer": "Favorita",
                "device": "all",
                "interest": "Trading",
                "creative": "1319 3",
                "deposits_total": 5,
                "deposits_recent": 0,
                "aggregate_key": {
                    "creative": "1319 3",
                    "offer": "Favorita",
                    "device": "all",
                    "interest": "Trading",
                },
            },
        ],
        "stable": [],
    }


def _combos_df() -> pd.DataFrame:
    rows = [
        ("Ecuador", "Favorita", "all", "Trading", "35+", "1319 3", "Strong", 5, 100, 500.0, 5.0, 100.0, 0.01, 0.05),
        ("Ecuador", "Favorita", "all", "Tradingz", "35+", "Vid 3 - pro -3", "Strong", 1, 10, 50.0, 5.0, 50.0, 0.1, 0.88),
        ("Ecuador", "AI Trader", "all", "Day", "35+", "2046 - Text 4", "Interesting", 1, 5, 30.0, 6.0, 30.0, 0.2, 0.5),
        ("Ecuador", "AI Trader", "all", "TRD", "35+", "2041_1_1_OneNight", "Interesting", 1, 4, 25.0, 6.25, 25.0, 0.25, 0.4),
    ]
    return pd.DataFrame(
        rows,
        columns=[
            "Country Final",
            "Offer",
            "Device",
            "Interest",
            "Age",
            "Creative",
            "Category",
            "Deposits",
            "Leads",
            "Spend",
            "CPL",
            "CPA",
            "CR",
            "ROI",
        ],
    )


def test_open_recs_variation_uses_new_trend_not_stalled_borrow(monkeypatch) -> None:
    class _Now:
        def strftime(self, fmt: str) -> str:
            return "20.05" if fmt == "%d.%m" else "2026-05-20"

    monkeypatch.setattr(
        "src.analysis_agent.morning_report.datetime",
        type("D", (), {"now": staticmethod(lambda: _Now())}),
    )

    combos = _combos_df()
    df = pd.DataFrame({
        "Country Final": ["Ecuador"],
        "Account Alias": ["MKTO1287"],
        "Interest": ["Tradingz"],
        "$ Spend": [100.0],
    })
    recs = build_open_recommendations(
        combos_df=combos,
        df=df,
        account_alias="MKTO1287",
        account_label="FacebookMKTO1287",
        country_code="EC",
        country_name="Ecuador",
        medians={},
        trend=_ecuador_trend(),
    )

    by_type = {r["strategy_type"]: r for r in recs}
    assert 2 in by_type
    var = by_type[2]
    assert var["targeting"] in ("Day", "TRD", "Tradingz")
    creative_names = [c["name"] for c in var["creatives"]]
    assert any("2046" in n or "2041" in n or "Vid 3" in n for n in creative_names)
    assert not any("1319" in n for n in creative_names)

    assert 3 in by_type
    rec = by_type[3]
    assert rec["targeting"] == "Trading"
    assert any("1319" in c["name"] for c in rec["creatives"])
    assert all(
        "recovery" in (c.get("note") or "").lower() or "1319" in c["name"]
        for c in rec["creatives"][:1]
    )

    # Status table prefers strategies 1–2, not recovery 1319
    status_recs = open_recs_for_status_table(recs, max_campaigns=2)
    assert len(status_recs) <= 2
    for r in status_recs:
        assert r["strategy_type"] in (1, 2)


def test_strategy_labels_complete() -> None:
    assert OPEN_STRATEGY_LABELS[2] == "2 · trend + variation"
