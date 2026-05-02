from __future__ import annotations

from pathlib import Path

from src.knowledge_agent.context_pack.builder import ContextPackBuilder
from src.knowledge_agent.context_pack.markdown import render_context_pack
from src.knowledge_agent.context_pack.models import ComboKey
from src.knowledge_agent.context_pack.store import ContextPackStore


def _seed_store(tmp_path: Path) -> ContextPackStore:
    store = ContextPackStore(tmp_path / "kb_context.db")
    store.initialize()
    store.upsert_offer(
        "Favorita",
        {
            "aliases": [],
            "audience": "Ecuador residents who know Favorita",
            "usp": "Invest in a trusted local retail brand",
            "top_countries": ["EC"],
            "notes": "Known Ecuador winner",
        },
    )
    store.upsert_target(
        "Trading",
        {
            "label": "Trading",
            "aliases": ["trade"],
            "audience": "Active traders interested in markets",
            "fb_interest": "Day trading",
            "top_countries": ["EC"],
            "notes": "Works in EC",
        },
    )
    store.upsert_creative("Video3509 The app", {"creative_type": "video", "aliases": [], "offer_hints": ["Favorita"]})
    store.upsert_creative_analysis(
        "Video3509 The app",
        {
            "visual_summary": "Mobile investment app demo",
            "hook": "easy start",
            "confidence": 0.82,
            "provider": "test",
        },
    )
    store.upsert_ad_text(
        "ES Benefit",
        {"body": "", "hook": "benefit angle", "cta": "Solicitar informacion", "offer": "Favorita"},
    )
    store.insert_performance_snapshot(
        {
            "country": "EC",
            "offer": "Favorita",
            "target": "Trading",
            "creative": "Video3509 The app",
            "ad_text_key": "ES Benefit",
            "spend": 120,
            "leads": 12,
            "deposits": 1,
            "cpa": 120,
            "roi": 36,
            "source": "fixture",
        }
    )
    return store


def test_combo_context_pack_combines_offer_target_creative_text_and_performance(tmp_path: Path) -> None:
    builder = ContextPackBuilder(_seed_store(tmp_path))
    pack = builder.build_combo_pack(
        ComboKey(
            country="EC",
            offer="Favorita",
            target="Trading",
            creative="Video3509 The app",
            ad_text_key="ES Benefit",
        )
    )

    rendered = render_context_pack(pack)
    assert "EC / Favorita / Trading / Video3509 The app / ES Benefit" in rendered
    assert "Invest in a trusted local retail brand" in rendered
    assert "Active traders interested in markets" in rendered
    assert "Mobile investment app demo" in rendered
    assert "1 deposits" in rendered
    assert "ad_text.body" in rendered


def test_offer_country_pack_summarizes_best_patterns(tmp_path: Path) -> None:
    builder = ContextPackBuilder(_seed_store(tmp_path))
    pack = builder.build_offer_country_pack(offer="Favorita", country="EC")

    assert pack.kind == "offer_country"
    rendered = render_context_pack(pack)
    assert "Offer + Country: Favorita / EC" in rendered
    assert "Best targets" in rendered
    assert "Trading" in rendered
    assert "Best creative patterns" in rendered
