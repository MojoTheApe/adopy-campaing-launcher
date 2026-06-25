"""Build audience profile from Knowledge Base for a given interest/target code.

Looks up knowledge_base.json and returns a structured profile:
  - label, fb_interest, audience description, top_countries, notes
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

_PROJ_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJ_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJ_ROOT))

from src.knowledge_agent.knowledge_base import get_target_context, resolve_target_code, get_all_targets


def _audience_summary(entry: Dict[str, Any]) -> str:
    """One-sentence professional TA description for the creative brief."""
    label = entry.get("label", "")
    audience = entry.get("audience", "")
    fb_interest = entry.get("fb_interest", "")
    notes = entry.get("notes", "")

    parts = []
    if label:
        parts.append(label)
    if audience:
        parts.append(audience)
    if fb_interest:
        parts.append(f"FB interests: {fb_interest}")
    if notes:
        parts.append(notes)
    return " | ".join(parts) if parts else "No audience data in KB."


def _best_interest_fuzzy(raw_interest: str) -> Optional[Dict[str, Any]]:
    """Try to match raw interest string to KB targets by partial name match."""
    if not raw_interest:
        return None
    raw_lower = raw_interest.strip().lower()
    all_targets = get_all_targets()
    if not all_targets:
        return None

    # Exact / alias lookup first
    ctx = get_target_context(raw_interest)
    if ctx:
        return ctx

    # Fuzzy: check if raw_interest substring matches any label or key
    for key, entry in all_targets.items():
        if not isinstance(entry, dict):
            continue
        label = str(entry.get("label", "")).lower()
        if raw_lower in key.lower() or raw_lower in label or key.lower() in raw_lower:
            return entry

    return None


def build_audience_profile(interest: str, country_code: str = "") -> Dict[str, Any]:
    """Return structured audience profile for the given interest code/name.

    Always returns a dict — falls back to minimal profile when KB has no match.
    """
    entry = _best_interest_fuzzy(interest)
    if not entry:
        return {
            "label": interest or "Unknown",
            "fb_interest": "",
            "audience": "Broad audience — no KB record for this interest.",
            "top_countries": [],
            "notes": "",
            "summary": f"Target: {interest or 'unknown'}. No KB record found.",
            "kb_found": False,
        }

    summary = _audience_summary(entry)
    return {
        "label": entry.get("label", interest),
        "fb_interest": entry.get("fb_interest", ""),
        "audience": entry.get("audience", ""),
        "top_countries": entry.get("top_countries", []),
        "notes": entry.get("notes", ""),
        "summary": summary,
        "kb_found": True,
    }


def professional_ta_description(profile: Dict[str, Any], country_code: str = "") -> str:
    """Return a short professional TA description for insertion into a creative brief.

    Format: 2–3 sentences max, suitable for a creative prompt context section.
    """
    label = profile.get("label", "")
    audience = profile.get("audience", "")
    fb_interest = profile.get("fb_interest", "")
    top_countries = profile.get("top_countries", [])
    notes = profile.get("notes", "")

    lines: List[str] = []

    if label and audience:
        lines.append(f"{label}: {audience}.")
    elif label:
        lines.append(f"Target segment: {label}.")
    elif audience:
        lines.append(audience + ".")

    if fb_interest:
        lines.append(f"Facebook targeting: {fb_interest}.")

    geo_note = ""
    if country_code and top_countries:
        geo_note = f"Strong in: {', '.join(top_countries)}."
    elif top_countries:
        geo_note = f"Top geos: {', '.join(top_countries[:4])}."
    if geo_note:
        lines.append(geo_note)

    if notes:
        lines.append(notes)

    return " ".join(lines) if lines else "No audience data available."
