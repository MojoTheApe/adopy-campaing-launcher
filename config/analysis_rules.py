"""Загрузка правил чтения данных для analyze_fb_stats (редактируйте analysis_reading_rules.json)."""
import json
from pathlib import Path
from typing import Any, Dict, List

_CONFIG_DIR = Path(__file__).resolve().parent
_RULES_PATH = _CONFIG_DIR / "analysis_reading_rules.json"

_DEFAULT_TEXT_SEGMENTS: Dict[str, List[str]] = {
    "pro": ["pro", "pro_new", "professional", "pro_v2"],
    "reg": ["reg", "registration", "easy_reg"],
    "learn": ["learn", "learning", "learn_new", "education"],
}


def _load_raw() -> Dict[str, Any]:
    if not _RULES_PATH.is_file():
        return {}
    try:
        with open(_RULES_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def get_text_segment_dictionary() -> Dict[str, List[str]]:
    """Словарь: сегмент (ключ) → список подстрок для поиска в Ad Title."""
    raw = _load_raw()
    segments = raw.get("text_segments")
    if isinstance(segments, dict) and segments:
        out: Dict[str, List[str]] = {}
        for k, v in segments.items():
            if str(k).startswith("_"):
                continue
            if isinstance(v, list):
                out[str(k)] = [str(x) for x in v if x is not None]
        if out:
            return out
    return dict(_DEFAULT_TEXT_SEGMENTS)


def get_text_segment_fallback() -> str:
    raw = _load_raw()
    fb = raw.get("text_segment_fallback")
    if isinstance(fb, str) and fb.strip():
        return fb.strip().upper()
    return "OTHER"


def rules_file_path() -> Path:
    return _RULES_PATH
