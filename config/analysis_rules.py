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


def get_device_keywords() -> Dict[str, List[str]]:
    """Словарь: тип девайса → список ключевых слов (lower case) для поиска в Adset Name."""
    raw = _load_raw()
    kw = raw.get("device_keywords")
    if isinstance(kw, dict) and kw:
        out: Dict[str, List[str]] = {}
        for k, v in kw.items():
            if isinstance(v, list):
                out[str(k)] = [str(x).lower() for x in v]
        if out:
            return out
    return {"all": ["all"], "ios": ["ios"]}


def get_age_range() -> tuple:
    """Допустимый диапазон возраста в Adset Name: (min, max)."""
    raw = _load_raw()
    rng = raw.get("age_range")
    if isinstance(rng, dict):
        mn = rng.get("min")
        mx = rng.get("max")
        if isinstance(mn, int) and isinstance(mx, int):
            return (mn, mx)
    return (18, 65)


def get_creative_skip_prefixes() -> List[str]:
    """Префиксы частей Adset Name, относящихся к креативу (пропускаются при поиске Interest)."""
    raw = _load_raw()
    prfx = raw.get("creative_skip_prefixes")
    if isinstance(prfx, list) and prfx:
        return [str(x).lower() for x in prfx]
    return ["vid", "video", "image", "images", "img", "photo", "creativ", "mix"]


def rules_file_path() -> Path:
    return _RULES_PATH
