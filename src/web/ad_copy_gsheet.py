"""Compatibility entry points for ad-copy consumers, backed by the local library.

Google Sheets only seeds an uninitialized library. Edits, deletions and errors
have the same meaning in the dashboard and the RPA draft builder.
"""
from __future__ import annotations

from typing import Optional

from src.analysis_agent import ad_text_registry as registry


def reload_cache() -> None:
    """Discard sheet bootstrap cache; local records are always read afresh."""
    from src.analysis_agent import gsheet_ad_texts as gs
    with gs._lock:
        gs._cache = gs._Cache()
        gs.last_error = ""


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
            f"Текст «{ad_title}» отсутствует или его имя неоднозначно. Выберите текст из библиотеки."
        )
    return {field: matches[0][field] for field in ("headline", "body", "description")}
