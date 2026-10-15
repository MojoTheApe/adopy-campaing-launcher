"""Загрузка правил чтения данных для analyze_fb_stats (редактируйте analysis_reading_rules.json)."""
import copy
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List

_CONFIG_DIR = Path(__file__).resolve().parent
_RULES_PATH = _CONFIG_DIR / "analysis_reading_rules.json"

_DEFAULT_TEXT_SEGMENTS: Dict[str, List[str]] = {
    "pro": ["pro", "pro_new", "professional", "pro_v2"],
    "reg": ["reg", "registration", "easy_reg"],
    "learn": ["learn", "learning", "learn_new", "education"],
}


# ACL-1399: файл читался на КАЖДУЮ строку книги — normalize_text_segment
# спрашивает словарь и fallback для каждой из ~33 тысяч строк. Замер 30.09:
# 60 256 вызовов, 69 с из 106 с холодной сборки ушли на open + is_file.
# Теперь файл разбирается один раз и сверяется с диском (mtime + размер) не
# чаще раза в _RECHECK_S — правка руками подхватывается без перезапуска.
_RECHECK_S = 2.0
_cache_lock = threading.Lock()
_cache: Dict[str, Any] = {"stamp": None, "checked": None, "path": None, "data": {}}


def _stamp(path: Path):
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _read(path: Path) -> Dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _rules() -> Dict[str, Any]:
    """Разобранный файл правил из кэша. Только для чтения внутри модуля."""
    now = time.monotonic()
    path = _RULES_PATH
    with _cache_lock:
        fresh = (_cache["path"] == path and _cache["checked"] is not None
                 and now - _cache["checked"] < _RECHECK_S)
        if fresh:
            return _cache["data"]
        stamp = _stamp(path)
        if _cache["path"] != path or stamp != _cache["stamp"]:
            _cache["data"] = _read(path) if stamp is not None else {}
            _cache["stamp"] = stamp
            _cache["path"] = path
        _cache["checked"] = now
        return _cache["data"]


def _forget_cache() -> None:
    with _cache_lock:
        _cache.update({"stamp": None, "checked": None, "path": None, "data": {}})


def _load_raw() -> Dict[str, Any]:
    """Копия правил: вызывающий может её менять, кэш от этого не портится."""
    return copy.deepcopy(_rules())


def get_text_segment_dictionary() -> Dict[str, List[str]]:
    """Словарь: сегмент (ключ) → список подстрок для поиска в Ad Title."""
    raw = _rules()
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
    raw = _rules()
    fb = raw.get("text_segment_fallback")
    if isinstance(fb, str) and fb.strip():
        return fb.strip().upper()
    return "OTHER"


def get_device_keywords() -> Dict[str, List[str]]:
    """Словарь: тип девайса → список ключевых слов (lower case) для поиска в Adset Name."""
    raw = _rules()
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
    raw = _rules()
    rng = raw.get("age_range")
    if isinstance(rng, dict):
        mn = rng.get("min")
        mx = rng.get("max")
        if isinstance(mn, int) and isinstance(mx, int):
            return (mn, mx)
    return (18, 65)


def get_creative_skip_prefixes() -> List[str]:
    """Префиксы частей Adset Name, относящихся к креативу (пропускаются при поиске Interest)."""
    raw = _rules()
    prfx = raw.get("creative_skip_prefixes")
    if isinstance(prfx, list) and prfx:
        return [str(x).lower() for x in prfx]
    return ["vid", "video", "image", "images", "img", "photo", "creativ", "mix"]


def rules_file_path() -> Path:
    return _RULES_PATH
