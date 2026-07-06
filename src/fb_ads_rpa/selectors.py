"""Load Ads Manager selector config."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_DEFAULT = Path(__file__).resolve().parents[2] / "config" / "ads_manager_selectors.yaml"

_cache: dict[str, dict[str, Any]] = {}


def load_selectors(path: str | None = None) -> dict[str, Any]:
    key = str(path) if path else str(_DEFAULT)
    if key in _cache:
        return _cache[key]
    p = Path(key)
    if not p.is_file():
        result: dict[str, Any] = {"urls": {}, "selectors": {}, "timing": {}}
        _cache[key] = result
        return result
    with p.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    _cache[key] = data
    return data


def reload_selectors(path: str | None = None) -> dict[str, Any]:
    """Force re-read from disk (useful during selector tuning)."""
    key = str(path) if path else str(_DEFAULT)
    _cache.pop(key, None)
    return load_selectors(path)
