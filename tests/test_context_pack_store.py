from __future__ import annotations

from pathlib import Path

from src.knowledge_agent.context_pack.store import ContextPackStore


def test_store_creates_schema_and_upserts_offer_target_creative(tmp_path: Path) -> None:
    store = ContextPackStore(tmp_path / "kb_context.db")
    store.initialize()

    store.upsert_offer(
        name="Favorita",
        payload={
            "aliases": ["Corporacion Favorita"],
            "audience": "Ecuador retail investors",
            "usp": "Local trusted brand",
            "top_countries": ["EC"],
            "notes": "Known Ecuador winner",
        },
    )
    store.upsert_target(
        code="Trading",
        payload={
            "label": "Trading",
            "aliases": ["trade"],
            "audience": "Active traders",
            "fb_interest": "Day trading",
            "top_countries": ["EC"],
            "notes": "Works in EC",
        },
    )
    store.upsert_creative(
        name="Video3509 The app",
        payload={
            "creative_type": "video",
            "aliases": ["3509 - the app"],
            "offer_hints": ["Favorita"],
            "deposits_summary": 1,
        },
    )

    assert store.get_offer("favorita")["name"] == "Favorita"
    assert store.get_target("trade")["code"] == "Trading"
    assert store.get_creative("3509 - the app")["name"] == "Video3509 The app"


def test_manual_override_wins_over_creative_analysis(tmp_path: Path) -> None:
    store = ContextPackStore(tmp_path / "kb_context.db")
    store.initialize()
    store.upsert_creative("Blue_6t", {"creative_type": "video", "aliases": []})
    store.upsert_creative_analysis(
        "Blue_6t",
        {
            "visual_summary": "Auto summary",
            "hook": "Auto hook",
            "confidence": 0.55,
            "provider": "test",
        },
    )
    store.upsert_manual_override(
        entity_type="creative",
        entity_key="Blue_6t",
        field_name="visual_summary",
        value="Manual summary",
    )

    merged = store.get_creative_context("Blue_6t")
    assert merged["analysis"]["visual_summary"] == "Manual summary"
    assert merged["analysis"]["hook"] == "Auto hook"
