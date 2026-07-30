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
from src.knowledge_agent.knowledge_base import get_target_context, resolve_targeting_parts


def resolve_interest_alias(text: Optional[str]) -> Dict[str, Any]:
    """Resolve a free-text interest/targeting string to its canonical KB target(s).

    Delegates to knowledge_base.resolve_targeting_parts, which handles both
    `|`/`\\`/`/`-joined compounds AND the dictionary fallback (KB key embedded
    in parsing noise, e.g. "AAY_Day" → Day, "Blue_HV|Investment" parts) — see
    задача Д4 in the Obsidian plan. A single lookup of the whole compound
    string against the KB always fails, since compound combos are never
    modeled as their own KB entries.

    Returns {"raw": text, "matched": bool, "code": str|None, "label": str|None,
    "fb_interest": str|None} — "code" is the canonical KB key(s) joined with
    "|" (e.g. "Day|HV"), distinct from "raw" which is the original text
    (possibly using a different separator, e.g. "Day/Investment").
    """
    raw = (text or "").strip()
    parts = resolve_targeting_parts(raw) if raw else None
    if not parts:
        return {"raw": raw, "matched": False, "code": None, "label": None, "fb_interest": None}

    resolved = [(c, get_target_context(c) or {}) for c in parts]
    return {
        "raw": raw,
        "matched": True,
        "code": "|".join(c for c, _ in resolved),
        "label": " + ".join(e.get("label") or c for c, e in resolved),
        "fb_interest": ", ".join(e.get("fb_interest") or "" for _, e in resolved).strip(", "),
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
