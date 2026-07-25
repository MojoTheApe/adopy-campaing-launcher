"""Helpers for the Campaign Builder's "Send to Builder" prefill feature.

Resolves free-text targeting strings (as typed by whoever created a real ad
set, or as parsed out of a raw ad set name) against the canonical Knowledge
Base targets — the same resolution used elsewhere in the pipeline via
``aliases[]`` in config/knowledge_base.json, just not previously wired into
the Campaign Builder UI.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from config.analysis_rules import (
    get_age_range,
    get_creative_skip_prefixes,
    get_device_keywords,
)
from src.analysis_agent.analyze_fb_stats import parse_adset_name
from src.knowledge_agent.knowledge_base import get_target_context, resolve_target_code


def resolve_interest_alias(text: Optional[str]) -> Dict[str, Any]:
    """Resolve a free-text interest/targeting string to its canonical KB target.

    Returns {"raw": text, "matched": bool, "code": str|None, "label": str|None,
    "fb_interest": str|None} — "code" is the canonical KB key (e.g. "Trading"),
    distinct from "raw" which may be an alias (e.g. "Trade") or unrecognized text.
    """
    raw = (text or "").strip()
    if not raw:
        return {"raw": raw, "matched": False, "code": None, "label": None, "fb_interest": None}

    canonical = resolve_target_code(raw)
    if not canonical:
        return {"raw": raw, "matched": False, "code": None, "label": None, "fb_interest": None}

    entry = get_target_context(canonical) or {}
    return {
        "raw": raw,
        "matched": True,
        "code": canonical,
        "label": entry.get("label"),
        "fb_interest": entry.get("fb_interest"),
    }


def parse_adset_for_builder(name: Optional[str]) -> Dict[str, Any]:
    """Parse a raw ad set name and resolve its interest token via the KB.

    Format assumed: CountryCode_OfferName_Device_Creative[_CreativeNum]_Interest_Age[_Date]
    (see analyze_fb_stats.parse_adset_name for the exact algorithm).
    """
    name = name or ""
    country_code, device, interest_raw, age = parse_adset_name(
        name,
        get_device_keywords(),
        *get_age_range(),
        get_creative_skip_prefixes(),
    )
    targeting = resolve_interest_alias(interest_raw)
    return {
        "country_code": country_code,
        "device": device,
        "age": age,
        "targeting": targeting,
    }
