"""Правила чтения (config/analysis_reading_rules.json) читаются один раз, а не на строку.

ACL-1399, замер 30.09: холодная сборка отчёта — 106 с, из них 69 с —
`analysis_rules._load_raw`, вызванный 60 256 раз (open + is_file на каждую
строку книги: `normalize_text_segment` спрашивал словарь сегментов и
fallback для каждой из 33 тысяч строк). Файл за это время не меняется.

Правка файла руками по-прежнему подхватывается — без перезапуска, через
пару секунд: кэш сверяет mtime и размер, только не на каждый вызов.
"""
from __future__ import annotations

import builtins
import json
import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import analysis_rules as ar  # noqa: E402


@pytest.fixture
def rules(tmp_path, monkeypatch):
    path = tmp_path / "analysis_reading_rules.json"
    path.write_text(json.dumps({"text_segments": {"pro": ["pro"]}, "text_segment_fallback": "other"}),
                    encoding="utf-8")
    monkeypatch.setattr(ar, "_RULES_PATH", path)
    ar._forget_cache()
    yield path
    ar._forget_cache()


def _count_opens(monkeypatch, path: Path) -> list:
    opened = []
    real_open = builtins.open

    def spy(file, *a, **kw):
        if Path(str(file)) == path:
            opened.append(file)
        return real_open(file, *a, **kw)

    monkeypatch.setattr(builtins, "open", spy)
    return opened


def test_thousands_of_rows_open_the_file_once(rules, monkeypatch):
    from src.analysis_agent.analyze_fb_stats import normalize_text_segment

    opened = _count_opens(monkeypatch, rules)
    got = [normalize_text_segment(t) for t in ["PRO_ad", "reg thing", None] * 2000]
    assert got[:3] == ["PRO", "OTHER", "OTHER"]
    assert len(opened) == 1, f"rules file opened {len(opened)} times for one build"


def test_a_hand_edit_is_picked_up_without_a_restart(rules, monkeypatch):
    assert ar.get_text_segment_fallback() == "OTHER"
    rules.write_text(json.dumps({"text_segment_fallback": "misc_bucket"}), encoding="utf-8")
    st = rules.stat()
    os.utime(rules, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    # Сверка с диском — не чаще раза в `_RECHECK_S`: сдвигаем часы, а не спим.
    real = time.monotonic
    monkeypatch.setattr(ar.time, "monotonic", lambda: real() + ar._RECHECK_S + 1)
    assert ar.get_text_segment_fallback() == "MISC_BUCKET"


def test_a_missing_or_broken_file_still_falls_back(rules, monkeypatch):
    rules.write_text("{not json", encoding="utf-8")
    ar._forget_cache()
    assert ar.get_text_segment_fallback() == "OTHER"
    assert ar.get_text_segment_dictionary() == ar._DEFAULT_TEXT_SEGMENTS
    rules.unlink()
    ar._forget_cache()
    assert ar.get_age_range() == (18, 65)


def test_callers_cannot_poison_the_cache(rules):
    raw = ar._load_raw()
    raw["text_segment_fallback"] = "HACKED"
    raw.setdefault("text_segments", {})["x"] = ["y"]
    assert ar.get_text_segment_fallback() == "OTHER"
    assert "x" not in ar.get_text_segment_dictionary()


def test_unknown_interests_are_logged_in_one_pass(tmp_path, monkeypatch):
    """Журнал неизвестных таргетов читается один раз на сборку, а не на код.

    Раньше на каждый неизвестный код книга заново резалась целиком и журнал
    перечитывался — ~15 с холодной сборки на двух сотнях кодов.
    """
    import pandas as pd

    from src.analysis_agent import analyze_fb_stats as afs
    from src.knowledge_agent import knowledge_base as kb

    log = tmp_path / "unknown_targets.log"
    log.write_text("old_code | source: x\n", encoding="utf-8")
    monkeypatch.setattr(kb, "_UNKNOWN_LOG", log)
    monkeypatch.setattr(afs, "resolve_targeting_text", lambda t: "known" if t == "known" else None)
    reads = []
    real = kb.logged_unknown_targets
    monkeypatch.setattr(afs, "logged_unknown_targets", lambda: reads.append(1) or real())

    df = pd.DataFrame({
        "Interest": ["new_a", " new_a", "known", "old_code", None, "wide", "new_b", "new_b"],
        "Adset Name": ["SA_new_a_1", "SA_new_a_2", "SA_k", "SA_old", "SA_none", "SA_w", None, "SA_new_b"],
    })
    afs._log_unknown_interests(df)
    afs._log_unknown_interests(df)          # вторая сборка — без дублей

    assert len(reads) == 2, "journal re-read per unknown code"
    lines = log.read_text(encoding="utf-8").splitlines()
    assert lines == ["old_code | source: x", "new_a | source: SA_new_a_1", "new_b | source: SA_new_b"]
