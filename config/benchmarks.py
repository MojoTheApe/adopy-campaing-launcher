"""KPI benchmark loader and data-derived median computation.

Lookup order: country → offer → metric.  Offer-level values override
country-level, which override global defaults.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

import pandas as pd

_CONFIG_PATH = Path(__file__).resolve().parent / "benchmarks.json"

_DEFAULTS = {"max_cpl": None, "max_cpa": None, "min_cr": 1.0}

_cache: Optional[Dict[str, Any]] = None


def _load() -> Dict[str, Any]:
    global _cache
    if _cache is None:
        try:
            _cache = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            _cache = {"defaults": _DEFAULTS, "countries": {}}
    return _cache


def get_benchmark(country: str, offer: Optional[str] = None) -> Dict[str, Any]:
    """Return merged benchmark dict for *country* (+ optional *offer*).

    Keys: ``max_cpl``, ``max_cpa``, ``min_cr`` — each may be ``None``
    (meaning "no limit").
    """
    cfg = _load()
    result = dict(cfg.get("defaults", _DEFAULTS))

    country_cfg = cfg.get("countries", {}).get(country, {})
    for key in ("max_cpl", "max_cpa", "min_cr"):
        if key in country_cfg and country_cfg[key] is not None:
            result[key] = country_cfg[key]

    if offer:
        offer_cfg = country_cfg.get("offers", {}).get(offer, {})
        for key in ("max_cpl", "max_cpa", "min_cr"):
            if key in offer_cfg and offer_cfg[key] is not None:
                result[key] = offer_cfg[key]

    return result


def compute_country_medians(df: pd.DataFrame, country: str) -> Dict[str, Optional[float]]:
    """Compute median CPL, CPA, CR% from live data for *country*.

    Only rows with positive spend and leads are considered for CPL;
    only rows with deposits > 0 are considered for CPA.
    Returns ``{"median_cpl": ..., "median_cpa": ..., "median_cr": ...}``.
    """
    df_c = df[df["Country Final"] == country].copy()

    def _median_or_none(series: pd.Series) -> Optional[float]:
        s = series.dropna()
        return round(float(s.median()), 2) if len(s) > 0 else None

    cpl_col = df_c["CPL"] if "CPL" in df_c.columns else pd.Series(dtype=float)
    cpa_col = df_c["CPA"] if "CPA" in df_c.columns else pd.Series(dtype=float)
    cr_col = df_c["CR"] if "CR" in df_c.columns else pd.Series(dtype=float)

    return {
        "median_cpl": _median_or_none(cpl_col),
        "median_cpa": _median_or_none(cpa_col),
        "median_cr": _median_or_none(cr_col),
    }
