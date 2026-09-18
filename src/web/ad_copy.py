"""Ad-copy lookups for the RPA draft builder, backed by the team ad text library.

Edits, deletions and errors mean the same in the dashboard and the draft builder.
"""
from __future__ import annotations

from typing import Optional

from src.analysis_agent import ad_text_registry as registry


def load_ad_copy() -> dict[str, dict[str, str]]:
    return {t.name: {key: getattr(t, key) for key in
                    ("headline", "body", "description", "asset", "language")}
            for t in registry.builder_texts()}


def lookup_ad_copy_by_title(ad_title: Optional[str]) -> dict[str, str]:
    key = registry.normalise_one(ad_title or "")
    if not key:
        return {"headline": "", "body": "", "description": ""}
    matches = [entry for name, entry in load_ad_copy().items()
               if registry.normalise_one(name) == key]
    if len(matches) != 1:
        raise registry.AdTextError(
            f"The ad text “{ad_title}” is missing or its title is ambiguous. Choose a text from the library."
        )
    return {field: matches[0][field] for field in ("headline", "body", "description")}
