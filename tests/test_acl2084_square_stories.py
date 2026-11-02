"""ACL-2084: квадратный файл в слоте Stories / Reels (шаг 1 - правило соотношений).

Слот Stories / Reels принимает 9:16 или 1:1 (допуск 1,5 %), картинки и видео одинаково; основной
файл по-прежнему 1:1 или 4:5. Остальное - отказ до любой загрузки. Ни одного живого вызова:
Graph - поддельная Meta из теста ACL-1761.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_api import creator as cr  # noqa: E402
from tests.test_acl1761_api_create import FakeMeta, _run  # noqa: E402
from tests.test_acl1774_texts_vertical import _ad_with, _creative  # noqa: E402


@pytest.mark.parametrize("kind", ["image", "video"])
@pytest.mark.parametrize("vertical", [(1080, 1920), (1080, 1080), (1000, 1014), (1014, 1000)])
def test_the_stories_reels_slot_takes_9_16_or_1_1_for_images_and_videos(monkeypatch, kind, vertical):
    sizes = {b"main": (1080, 1080), b"vert": vertical}
    monkeypatch.setattr(cr, "media_size", lambda k, data: sizes[data])
    assert cr.pair_problems(1, kind, b"main", b"vert") == []


@pytest.mark.parametrize("kind", ["image", "video"])
@pytest.mark.parametrize("vertical", [(1200, 628), (1080, 1350), (1000, 1020)])
def test_other_shapes_in_the_slot_are_refused_and_the_message_names_1_1(monkeypatch, kind, vertical):
    sizes = {b"main": (1080, 1080), b"vert": vertical}
    monkeypatch.setattr(cr, "media_size", lambda k, data: sizes[data])
    problems = cr.pair_problems(1, kind, b"main", b"vert")
    assert len(problems) == 1
    assert "must be 9:16 (for example 1080x1920) or 1:1 (for example 1080x1080)" in problems[0]


def test_a_square_stories_reels_file_publishes_as_a_labelled_pair(tmp_path):
    meta = FakeMeta()
    out, _ = _run(meta, _ad_with(tmp_path, main=(1080, 1080), vertical=(1080, 1080)))
    assert out.error == "" and out.readback["ok"], (out.error, out.mismatches)
    fs = _creative(meta)["asset_feed_spec"]
    assert {i["adlabels"][0]["name"] for i in fs["images"]} == {"acl_main", "acl_vertical"}


def test_a_4_5_main_file_and_a_separate_square_stories_reels_file_pass(tmp_path):
    """Решение владельца D3 (2026-11-02): 4:5 в ленте + отдельный 1:1 в Stories / Reels разрешены."""
    meta = FakeMeta()
    out, _ = _run(meta, _ad_with(tmp_path, main=(1080, 1350), vertical=(1080, 1080)))
    assert out.error == "" and out.readback["ok"], (out.error, out.mismatches)
    assert "asset_feed_spec" in _creative(meta)
