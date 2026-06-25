"""Scan creatives/input/ and parse file metadata.

File naming convention (same slug as adset names):
    <CountryCode>_<Offer>_<Device>_<MediaType>_<Interest>_<Age>[_<TextSegment>].<ext>
    Example: MX_Amz_All_img_Finance_35_pro.jpg

Falls back to raw filename when parsing fails.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

_PROJ_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJ_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJ_ROOT))

from config.analysis_rules import (
    get_age_range,
    get_creative_skip_prefixes,
    get_device_keywords,
    get_text_segment_dictionary,
    get_text_segment_fallback,
)
from src.analysis_agent.analyze_fb_stats import (
    normalize_text_segment,
    parse_adset_name,
)

SUPPORTED_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
SUPPORTED_VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv"}
SUPPORTED_EXTS = SUPPORTED_IMAGE_EXTS | SUPPORTED_VIDEO_EXTS

_DEFAULT_INPUT_DIR = _PROJ_ROOT / "creatives" / "input"


def _resolve_input_dir() -> Path:
    env_root = os.environ.get("FB_CREATIVES_ROOT", "").strip()
    if env_root:
        p = Path(env_root)
        if p.is_dir():
            return p
    return _DEFAULT_INPUT_DIR


def _parse_text_segment_from_stem(stem: str) -> str:
    """Check the last part of the filename stem for a text segment keyword."""
    parts = stem.split("_")
    if parts:
        last = parts[-1].lower()
        for seg, variants in get_text_segment_dictionary().items():
            for v in variants:
                if v in last:
                    return seg.upper()
    return normalize_text_segment(stem)


def scan_file(path: Path) -> Dict[str, Any]:
    """Parse a single creative file into a metadata dict."""
    stem = path.stem
    ext = path.suffix.lower()
    media_type = "image" if ext in SUPPORTED_IMAGE_EXTS else "video"

    device_kw = get_device_keywords()
    age_min, age_max = get_age_range()
    creative_prefixes = get_creative_skip_prefixes()

    country_code, device, interest, age = parse_adset_name(
        stem, device_kw, age_min, age_max, creative_prefixes
    )

    text_segment = _parse_text_segment_from_stem(stem)

    # Try to infer offer from position 1 in slug (after country code)
    parts = [p.strip() for p in stem.split("_") if p.strip()]
    offer_raw = parts[1] if len(parts) > 1 else ""

    return {
        "path": path,
        "filename": path.name,
        "stem": stem,
        "media_type": media_type,
        "ext": ext,
        "country_code": country_code or "",
        "offer_raw": offer_raw,
        "device": device or "all",
        "interest": interest or "",
        "age": age or "",
        "text_segment": text_segment,
    }


def scan_input_dir(input_dir: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Return metadata dicts for all supported files in input_dir (recursive)."""
    d = input_dir or _resolve_input_dir()
    if not d.is_dir():
        return []
    results = []
    for f in sorted(d.rglob("*")):
        if f.is_file() and f.suffix.lower() in SUPPORTED_EXTS:
            results.append(scan_file(f))
    return results


def find_creative_file(name: str, search_dir: Optional[Path] = None) -> Optional[Path]:
    """Search recursively for a creative file by stem name (case-insensitive).

    Strips extension from `name` if provided, then matches against all file
    stems under search_dir.  Returns the first match or None.
    """
    d = search_dir or _resolve_input_dir()
    if not d.is_dir():
        return None

    # Normalise: strip path separators and extension from the query
    name_clean = Path(name).stem.strip().lower()
    if not name_clean:
        return None

    for f in d.rglob("*"):
        if not f.is_file():
            continue
        if f.suffix.lower() not in SUPPORTED_EXTS:
            continue
        if f.stem.strip().lower() == name_clean:
            return f

    # Partial match fallback: query is a substring of the filename
    for f in d.rglob("*"):
        if not f.is_file():
            continue
        if f.suffix.lower() not in SUPPORTED_EXTS:
            continue
        if name_clean in f.stem.strip().lower():
            return f

    return None
