"""ACL-2084: квадратный файл в слоте Stories / Reels.

Слот Stories / Reels принимает 9:16 или 1:1 (допуск 1,5 %), картинки и видео одинаково; основной
файл по-прежнему 1:1 или 4:5. Остальное - отказ до любой загрузки. Ни одного живого вызова:
Graph - поддельная Meta из теста ACL-1761.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_api import creator as cr  # noqa: E402
from tests.test_acl1761_api_create import FakeMeta, _draft, _run  # noqa: E402
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


# ───────────────────────── тот же файл в обоих слотах: один раз ─────────────────────────
# Живой прогон 2026-11-04 (Doc120): Meta отказала в ленте с одним хэшем под двумя метками -
# «All ad asset values must be unique». Тот же файл (путь или байты) поэтому идёт одним файлом.

class UniqueAssetsMeta(FakeMeta):
    """Поддельная Meta с живым правилом: значения ассетов ленты должны быть уникальны."""

    def write(self, path, params, *args, **kw):
        spec = params.get("asset_feed_spec") if isinstance(params, dict) else None
        if isinstance(spec, str):
            spec = json.loads(spec)
        if isinstance(spec, dict):
            ids = [i.get("hash") for i in spec.get("images") or []] + [v.get("video_id") for v in spec.get("videos") or []]
            if len(ids) != len(set(ids)):
                raise AssertionError("Meta: All ad asset values must be unique")
        return super().write(path, params, *args, **kw)


def _same_file(tmp_path, *, main=(1080, 1080), texts=0, copy=False):
    d = _ad_with(tmp_path, main=main, vertical=None, texts=texts)
    ad = d.ad_sets[0].ads[0]
    if copy:
        other = tmp_path / "copy" / ("copy_" + Path(ad.creative_path).name)
        other.parent.mkdir(exist_ok=True)
        other.write_bytes(Path(ad.creative_path).read_bytes())
        ad.vertical_path, ad.vertical_filename = str(other), other.name
    else:
        ad.vertical_path, ad.vertical_filename = ad.creative_path, Path(ad.creative_path).name
    return d


def _uploads(meta, edge):
    return [p for p, _ in meta.posts if p.endswith("/" + edge)]


def _said(out, text):
    return [w for w in out.warnings if text in w.lower()]


@pytest.mark.parametrize("copy", [False, True])
@pytest.mark.parametrize("main", [(1080, 1080), (1080, 1350)])
def test_the_same_image_in_both_slots_goes_once_as_a_plain_ad(tmp_path, copy, main):
    meta = UniqueAssetsMeta()
    out, _ = _run(meta, _same_file(tmp_path, main=main, copy=copy))
    assert out.error == "" and out.readback["ok"], (out.error, out.mismatches)
    assert "asset_feed_spec" not in _creative(meta)
    assert len(_uploads(meta, "adimages")) == 1
    assert len(_said(out, "same file")) == 1, out.warnings


@pytest.mark.parametrize("copy", [False, True])
def test_the_same_video_in_both_slots_is_uploaded_once_as_a_plain_ad(tmp_path, monkeypatch, copy):
    from tests.test_acl1819_preflight import _mp4
    from tests.test_acl1891_video_chunks import ChunkMeta
    monkeypatch.setattr(cr, "VIDEO_SINGLE_MAX_BYTES", 512)
    draft = _draft(tmp_path, video=True, ads=1)
    ad = draft.ad_sets[0].ads[0]
    Path(ad.creative_path).write_bytes(_mp4(15, 1080, 1080) + b"m" * 2048)
    if copy:
        other = tmp_path / "copy.mp4"
        other.write_bytes(Path(ad.creative_path).read_bytes())
        ad.vertical_path, ad.vertical_filename = str(other), other.name
    else:
        ad.vertical_path, ad.vertical_filename = ad.creative_path, Path(ad.creative_path).name
    meta = ChunkMeta()
    meta.next_id = 100000
    out, _ = _run(meta, draft)
    assert not out.error and out.readback["ok"], out.error or out.mismatches
    assert len(meta.sessions) == 1
    assert "asset_feed_spec" not in _creative(meta)


def test_a_copy_of_a_large_video_is_not_counted_in_the_chunk_budget(tmp_path, monkeypatch):
    from tests.test_acl1819_preflight import _audit, _mp4
    monkeypatch.setattr(cr, "VIDEO_SINGLE_MAX_BYTES", 512)
    draft = _draft(tmp_path, video=True, ads=1)
    ad = draft.ad_sets[0].ads[0]
    Path(ad.creative_path).write_bytes(_mp4(15, 1080, 1080) + b"m" * 4096)
    other = tmp_path / "copy.mp4"
    other.write_bytes(Path(ad.creative_path).read_bytes())
    ad.vertical_path, ad.vertical_filename = str(other), other.name
    seen = []
    original = cr.upload_calls
    monkeypatch.setattr(cr, "upload_calls", lambda n: seen.append(n) or original(n))
    out = _audit(FakeMeta(), draft)
    assert len(seen) == 1
    row = out.plan["summary"]["ad_sets"][0]["ads"][0]
    assert "stories_reels_file" not in row


def test_a_copy_under_another_name_with_several_texts_keeps_the_existing_error(tmp_path):
    with pytest.raises(cr.PlanError, match="can't be combined"):
        cr.plan_from_draft(_same_file(tmp_path, copy=True, texts=1))


def test_two_different_square_files_pass_the_unique_assets_rule(tmp_path):
    meta = UniqueAssetsMeta()
    out, _ = _run(meta, _ad_with(tmp_path, main=(1080, 1080), vertical=(1080, 1080)))
    assert out.error == "" and out.readback["ok"], (out.error, out.mismatches)
    assert len({i["hash"] for i in _creative(meta)["asset_feed_spec"]["images"]}) == 2


def test_check_plan_names_the_stories_reels_file_of_a_pair(tmp_path):
    from tests.test_acl1819_preflight import _audit
    pair = _audit(FakeMeta(), _ad_with(tmp_path, vertical=(1080, 1080))).plan["summary"]["ad_sets"][0]["ads"][0]
    assert pair["stories_reels_file"] == "1000_story.png"
