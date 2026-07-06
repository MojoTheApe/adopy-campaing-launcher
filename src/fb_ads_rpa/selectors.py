"""Load Ads Manager selector config."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

_DEFAULT = Path(__file__).resolve().parents[2] / "config" / "ads_manager_selectors.yaml"


@lru_cache(maxsize=1)
def load_selectors(path: str | None = None) -> dict[str, Any]:
    p = Path(path) if path else _DEFAULT
    if not p.is_file():
        return {"urls": {}, "selectors": {}, "timing": {}}
    with p.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data
