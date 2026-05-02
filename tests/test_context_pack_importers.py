from __future__ import annotations

from pathlib import Path

import pandas as pd

from src.knowledge_agent.context_pack.importers import import_knowledge_json, import_performance_rows
from src.knowledge_agent.context_pack.store import ContextPackStore


def test_import_knowledge_json_imports_offers_and_targets(tmp_path: Path) -> None:
    store = ContextPackStore(tmp_path / "kb_context.db")
    store.initialize()
    import_knowledge_json(
        store,
        {
            "offers": {
                "Favorita": {
                    "aliases": ["Corporacion Favorita"],
                    "audience": "EC shoppers",
                    "usp": "Known local brand",
                    "top_countries": ["EC"],
                    "notes": "Strong local trust",
                }
            },
            "targets": {
                "Trading": {
                    "label": "Trading",
                    "aliases": ["trade"],
                    "fb_interest": "Day trading",
                    "audience": "Active traders",
                    "top_countries": ["EC"],
                    "notes": "Works in EC",
                }
            },
        },
    )

    assert store.get_offer("Corporacion Favorita")["audience"] == "EC shoppers"
    assert store.get_target("trade")["fb_interest"] == "Day trading"


def test_import_performance_rows_creates_creatives_texts_and_snapshots(tmp_path: Path) -> None:
    store = ContextPackStore(tmp_path / "kb_context.db")
    store.initialize()
    df = pd.DataFrame(
        [
            {
                "Country Final": "EC",
                "Offer": "Favorita",
                "Interest": "Trading",
                "Creative": "Video3509 The app",
                "TextSegment": "ES Benefit",
                "$ Spend": 120.0,
                "# Leads": 12,
                "# FTDs": 1,
                "CPA": 120.0,
                "ROI": 36.0,
            }
        ]
    )

    import_performance_rows(store, df, source="fixture")

    assert store.get_creative("Video3509 The app")["creative_type"] == "video"
    assert store.get_ad_text("ES Benefit")["source"] == "performance_text_segment"
    rows = store.get_performance(country="EC", offer="Favorita", target="Trading")
    assert len(rows) == 1
    assert rows[0]["deposits"] == 1
