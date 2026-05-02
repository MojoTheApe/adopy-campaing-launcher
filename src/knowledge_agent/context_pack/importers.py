"""Import current project knowledge into the context pack store."""
from __future__ import annotations

from typing import Any

import pandas as pd

from src.knowledge_agent.context_pack.store import ContextPackStore


def import_knowledge_json(store: ContextPackStore, data: dict[str, Any]) -> None:
    for name, payload in (data.get("offers") or {}).items():
        if isinstance(payload, dict):
            store.upsert_offer(str(name), payload)
    for code, payload in (data.get("targets") or {}).items():
        if isinstance(payload, dict):
            store.upsert_target(str(code), payload)


def import_performance_rows(store: ContextPackStore, df: pd.DataFrame, *, source: str) -> None:
    for _, row in df.iterrows():
        country = _text(row.get("Country Final") or row.get("Country"))
        offer = _text(row.get("Offer") or row.get("Ad Asset"))
        target = _text(row.get("Interest"))
        creative = _text(row.get("Creative") or row.get("Ad Title"))
        text_key = _text(row.get("TextSegment"))

        if creative:
            ad_title = _text(row.get("Ad Title"))
            store.upsert_creative(
                creative,
                {
                    "creative_type": _creative_type(creative),
                    "aliases": [ad_title] if ad_title and ad_title != creative else [],
                    "offer_hints": [offer] if offer else [],
                    "deposits_summary": _safe_int(row.get("# FTDs")),
                },
            )

        if text_key:
            store.upsert_ad_text(
                text_key,
                {
                    "body": "",
                    "language": "",
                    "hook": "",
                    "cta": "",
                    "offer": offer,
                    "risk_flags": [],
                    "source": "performance_text_segment",
                },
            )
            if creative:
                store.link_creative_text(creative, text_key)

        store.insert_performance_snapshot(
            {
                "country": country,
                "offer": offer,
                "target": target,
                "creative": creative,
                "ad_text_key": text_key,
                "spend": _safe_float(row.get("$ Spend")),
                "leads": _safe_int(row.get("# Leads")),
                "deposits": _safe_int(row.get("# FTDs")),
                "cpa": _safe_float(row.get("CPA")),
                "roi": _safe_float(row.get("ROI")),
                "source": source,
            }
        )


def _creative_type(value: str) -> str:
    lowered = value.lower()
    if "video" in lowered or "vid" in lowered or lowered.endswith(".mp4"):
        return "video"
    if "image" in lowered or "img" in lowered or lowered.endswith((".jpg", ".jpeg", ".png", ".webp")):
        return "image"
    return ""


def _text(value: Any) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    return str(value).strip()


def _safe_int(value: Any) -> int:
    try:
        if pd.isna(value):
            return 0
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _safe_float(value: Any) -> float:
    try:
        if pd.isna(value):
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0
