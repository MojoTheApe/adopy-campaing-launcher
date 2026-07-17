from __future__ import annotations

from pathlib import Path


def repo_root() -> Path:
    # src/fb_ads_rpa/variations/paths.py -> parents[3] == repo root
    return Path(__file__).resolve().parents[3]


def get_variations_dir() -> Path:
    return repo_root() / "config" / "rpa_variations"


def steps_yaml_path() -> Path:
    return get_variations_dir() / "steps.yaml"


def ranks_json_path() -> Path:
    return get_variations_dir() / "strategy_ranks.json"
