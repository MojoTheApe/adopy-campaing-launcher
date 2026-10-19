"""Оценка вызовов Graph считает тяжёлое видео сжатым; вертикальный ролик тоже сжимается.

Живой Check 19.10 (TEST9095w, FacebookDoc120): основной и Stories/Reels ролики по 1,29 ГБ.
Оценка брала размер исходника (2 × ~158 кусков по 8 МБ) — 367 вызовов, отказ «split it»,
хотя оба ролика уходят сжатыми до ~45 МБ (Issue #1000) одним запросом. А вертикальный ролик
вообще не сжимался и ушёл бы кусками через вкладку Ads Manager.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_api import creator as cr  # noqa: E402
from tests.test_acl1761_api_create import FakeMeta, _draft, _run  # noqa: E402

MB = 1024 * 1024
HUGE = 1293 * MB


def _square_and_story(tmp_path):
    """Одно видео-объявление: основной ролик и вертикальный Stories/Reels."""
    d = _draft(tmp_path, video=True, ads=1)
    ad = d.ad_sets[0].ads[0]
    v = tmp_path / "1000_story.mp4"
    v.write_bytes(b"story")
    ad.vertical_filename, ad.vertical_path = v.name, str(v)
    return d


def test_huge_videos_count_as_compressed_when_ffmpeg_is_there(tmp_path, monkeypatch):
    monkeypatch.setattr(cr, "_compress_available", lambda: True)
    plan = cr.plan_from_draft(_square_and_story(tmp_path))
    small = cr.estimate_create_calls(plan, lambda p: 10)
    assert cr.estimate_create_calls(plan, lambda p: HUGE, compressed=True) == small
    # Точные размеры (настоящая сборка, файлы уже в руках) — куски считаются как раньше.
    assert cr.estimate_create_calls(plan, lambda p: HUGE) > 300


def test_without_ffmpeg_the_estimate_stays_honest(tmp_path, monkeypatch):
    monkeypatch.setattr(cr, "_compress_available", lambda: False)
    plan = cr.plan_from_draft(_square_and_story(tmp_path))
    assert (cr.estimate_create_calls(plan, lambda p: HUGE, compressed=True)
            == cr.estimate_create_calls(plan, lambda p: HUGE))


def test_a_light_video_is_not_touched(monkeypatch):
    monkeypatch.setattr(cr, "_compress_available", lambda: True)
    assert cr.expected_upload_size(50 * MB) == 50 * MB
    assert cr.expected_upload_size(HUGE) < cr.VIDEO_SINGLE_MAX_BYTES


def test_the_budget_check_lets_the_doc120_plan_through(tmp_path, monkeypatch):
    from src.web import publish_api

    plan = cr.plan_from_draft(_square_and_story(tmp_path))
    monkeypatch.setattr(cr, "_disk_size", lambda p: HUGE)
    monkeypatch.setattr(cr, "_compress_available", lambda: True)
    assert publish_api.create_call_budget(plan)[1] == ""
    monkeypatch.setattr(cr, "_compress_available", lambda: False)
    assert "split it" in publish_api.create_call_budget(plan)[1]


def test_the_stories_file_is_compressed_too(tmp_path, monkeypatch):
    monkeypatch.setattr(cr, "pair_problems", lambda *a, **k: [])
    calls = []

    def fake(path, data, index, step, *, warn):
        calls.append((Path(path).name, index))
        return b"small"

    _run(FakeMeta(), _square_and_story(tmp_path), compress_video=fake)
    assert calls == [("1000_clip.mp4", 1), ("1000_story.mp4", 1)]
