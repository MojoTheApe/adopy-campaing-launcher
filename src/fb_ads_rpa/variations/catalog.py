from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from src.fb_ads_rpa.variations.paths import steps_yaml_path

logger = logging.getLogger(__name__)


def load_steps_catalog(path: Path | None = None) -> dict[str, Any]:
    yaml_path = path or steps_yaml_path()
    with yaml_path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def step_risk(step_id: str, catalog: dict | None = None) -> str:
    if catalog is None:
        catalog = load_steps_catalog()
    steps = catalog.get("steps", {})
    if step_id not in steps:
        logger.warning("Unknown step_id %r; defaulting risk to safe", step_id)
        return "safe"
    return steps[step_id].get("risk", "safe")


def default_strategies(step_id: str, catalog: dict | None = None) -> list[str]:
    if catalog is None:
        catalog = load_steps_catalog()
    steps = catalog.get("steps", {})
    if step_id not in steps:
        raise KeyError(f"Unknown step_id: {step_id!r}")
    return list(steps[step_id].get("strategies", []))
