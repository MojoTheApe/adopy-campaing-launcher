"""ACL-2084: квадратный файл в слоте Stories / Reels (шаг 1 - правило соотношений).

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


# ───────────────────────── тот же квадратный файл в обоих слотах (шаг 4) ─────────────────────────

def manual(facebook=(), instagram=()):
    return {"mode": "manual", "facebook_positions": list(facebook), "instagram_positions": list(instagram)}


def _same_file(tmp_path, *, main=(1080, 1080), texts=0, placements=None, copy=False):
    """Черновик, где в слоте Stories / Reels выбран тот же файл (или его копия под другим именем)."""
    d = _ad_with(tmp_path, main=main, vertical=None, texts=texts)
    ad = d.ad_sets[0].ads[0]
    if copy:
        other = tmp_path / "copy" / ("copy_" + Path(ad.creative_path).name)
        other.parent.mkdir(exist_ok=True)
        other.write_bytes(Path(ad.creative_path).read_bytes())
        ad.vertical_path, ad.vertical_filename = str(other), other.name
    else:
        ad.vertical_path, ad.vertical_filename = ad.creative_path, Path(ad.creative_path).name
    if placements is not None:
        d.ad_sets[0].placements = placements
    return d


def _uploads(meta, edge):
    return [p for p, _ in meta.posts if p.endswith("/" + edge)]


def _said(out, text):
    return [w for w in out.warnings if text in w.lower()]


@pytest.mark.parametrize("placements", [None, manual(facebook=["feed", "story", "facebook_reels"],
                                                     instagram=["stream", "story", "reels"])])
def test_the_same_square_image_goes_to_both_slots_with_one_upload(tmp_path, placements):
    meta = FakeMeta()
    out, _ = _run(meta, _same_file(tmp_path, placements=placements))
    assert out.error == "" and out.readback["ok"], (out.error, out.mismatches)
    fs = _creative(meta)["asset_feed_spec"]
    assert {i["adlabels"][0]["name"] for i in fs["images"]} == {"acl_main", "acl_vertical"}
    assert len({i["hash"] for i in fs["images"]}) == 1
    assert len(fs["asset_customization_rules"]) == 2
    assert len(_uploads(meta, "adimages")) == 1
    assert not _said(out, "same file") and not _said(out, "meta will crop")
    assert any("Stories / Reels use the main file (no second upload)" in s for s in out.steps)


@pytest.mark.parametrize("copy", [False, True])
def test_the_same_square_video_is_uploaded_once_and_bound_twice(tmp_path, monkeypatch, copy):
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
    videos = _creative(meta)["asset_feed_spec"]["videos"]
    assert len(videos) == 2 and len({v["video_id"] for v in videos}) == 1


def test_a_copy_of_the_square_image_under_another_name_is_uploaded_once(tmp_path):
    meta = FakeMeta()
    out, _ = _run(meta, _same_file(tmp_path, copy=True))
    assert out.error == "" and out.readback["ok"], (out.error, out.mismatches)
    assert len(_uploads(meta, "adimages")) == 1
    assert len({i["hash"] for i in _creative(meta)["asset_feed_spec"]["images"]}) == 1


def test_a_copy_of_a_large_video_is_not_counted_twice_in_the_chunk_budget(tmp_path, monkeypatch):
    from tests.test_acl1819_preflight import _mp4
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
    from tests.test_acl1819_preflight import _audit
    _audit(FakeMeta(), draft)
    assert len(seen) == 1


@pytest.mark.parametrize("case,placements,texts,crop", [
    ("portrait", None, 0, False),
    ("several texts", None, 1, False),
    ("advantage+", {"mode": "advantage_plus"}, 0, True),
    ("feed only", manual(facebook=["feed"], instagram=["stream"]), 0, False),
    ("stories only", manual(facebook=["story"], instagram=["story"]), 0, True),
])
def test_when_a_pair_is_not_possible_the_file_is_used_once_with_one_warning(tmp_path, case, placements, texts, crop):
    """Решение владельца D4 (2026-11-02): один файл с предупреждением, никогда не ошибка."""
    main = (1080, 1920) if case == "portrait" else (1080, 1080)
    meta = FakeMeta()
    out, _ = _run(meta, _same_file(tmp_path, main=main, texts=texts, placements=placements))
    assert out.error == "" and out.readback["ok"], (case, out.error, out.mismatches)
    assert len(_said(out, "same file")) == 1, out.warnings
    assert len(_said(out, "meta will crop")) == (1 if crop else 0), out.warnings
    cre = _creative(meta)
    assert "asset_customization_rules" not in (cre.get("asset_feed_spec") or {})
    assert len(_uploads(meta, "adimages")) == 1


def test_an_instant_form_ad_set_never_takes_the_pair():
    from types import SimpleNamespace as NS
    ad = NS(multi_text=False)
    assert cr.pair_possible(ad, NS(instant=False, placements=None))
    assert not cr.pair_possible(ad, NS(instant=True, placements=None))
    assert not cr.pair_possible(NS(multi_text=True), NS(instant=False, placements=None))


def test_a_copy_under_another_name_with_several_texts_keeps_the_existing_error(tmp_path):
    d = _same_file(tmp_path, copy=True, texts=1)
    with pytest.raises(cr.PlanError, match="can't be combined"):
        cr.plan_from_draft(d)


def test_the_call_estimate_does_not_change_with_the_same_file_pair(tmp_path):
    plain = cr.plan_from_draft(_ad_with(tmp_path, vertical=None))
    paired = cr.plan_from_draft(_same_file(tmp_path))
    assert cr.estimate_create_calls(plain) == cr.estimate_create_calls(paired)


def test_a_square_ad_without_a_slot_choice_stays_the_old_creative(tmp_path):
    """Требование 4 issue: без выбора байера ничего не меняется (решение D1, вариант A)."""
    from tests.test_acl1819_preflight import _audit
    meta = FakeMeta()
    out, _ = _run(meta, _ad_with(tmp_path, vertical=None))
    assert out.error == "" and "asset_feed_spec" not in _creative(meta)
    row = _audit(FakeMeta(), _ad_with(tmp_path, vertical=None)).plan["summary"]["ad_sets"][0]["ads"][0]
    assert "stories_reels_same_as_main" not in row and "stories_reels_file" not in row


def test_check_plan_says_stories_reels_use_the_same_square_file(tmp_path):
    from tests.test_acl1819_preflight import _audit
    out = _audit(FakeMeta(), _same_file(tmp_path))
    assert out.error == "", out.error
    row = out.plan["summary"]["ad_sets"][0]["ads"][0]
    assert row["stories_reels_same_as_main"] is True
    assert row["placements_by_file"] == "Stories and Reels use the same square file as Feed"
    pair = _audit(FakeMeta(), _ad_with(tmp_path, vertical=(1080, 1080))).plan["summary"]["ad_sets"][0]["ads"][0]
    assert pair["stories_reels_file"] == "1000_story.png" and "stories_reels_same_as_main" not in pair


def test_a_repeat_run_finds_the_pair_and_uploads_nothing_again(tmp_path):
    meta = FakeMeta()
    draft = _same_file(tmp_path)
    first, _ = _run(meta, draft)
    assert first.error == "" and first.readback["ok"]
    posts = len(meta.posts)
    second, _ = _run(meta, draft)
    assert second.error == "" and second.readback["ok"], second.mismatches
    assert len(meta.posts) == posts


def test_a_rerun_over_a_plain_ad_published_before_stays_green_and_writes_nothing(tmp_path):
    meta = FakeMeta()
    first, _ = _run(meta, _ad_with(tmp_path, vertical=None))
    assert first.error == "" and first.readback["ok"]
    posts = len(meta.posts)
    out, _ = _run(meta, _same_file(tmp_path))
    assert out.error == "" and out.readback["ok"], out.mismatches
    assert len(meta.posts) == posts
    assert any("already published with one file" in w for w in out.warnings)


@pytest.mark.parametrize("main,status", [((1080, 1080), "ok"), ((1080, 1920), "ok")])
def test_check_plan_and_publish_reach_the_same_result(tmp_path, main, status):
    from tests.test_acl1819_preflight import _audit
    audit = _audit(FakeMeta(), _same_file(tmp_path, main=main))
    rows = {v["rule"]: v["status"] for v in audit.audit}
    assert rows["vertical_ratio"] == status
    meta = FakeMeta()
    out, _ = _run(meta, _same_file(tmp_path, main=main))
    paired = "asset_customization_rules" in (_creative(meta).get("asset_feed_spec") or {})
    summary_pair = audit.plan["summary"]["ad_sets"][0]["ads"][0].get("stories_reels_same_as_main", False)
    assert paired == summary_pair == (main == (1080, 1080))


def test_telemetry_has_one_square_pair_row_and_one_fallback_row(tmp_path, monkeypatch):
    from src.fb_ads_rpa import run_telemetry as rt
    rows: list = []
    monkeypatch.setattr(rt, "record", lambda step, ok=None, **kw: rows.append((step, ok, kw)))
    _run(FakeMeta(), _same_file(tmp_path))
    _run(FakeMeta(), _same_file(tmp_path / "p" if (tmp_path / "p").mkdir() is None else tmp_path, main=(1080, 1920)))
    pairs = [(step, ok, kw["diag"]) for step, ok, kw in rows if (kw.get("diag") or {}).get("metric") == "square_pair"]
    assert [(s, ok, d["origin"], d.get("reason")) for s, ok, d in pairs] == [
        ("api_create_step", True, "same_path", None), ("api_create_step", None, "same_path", "not_square")]
    assert all("png" not in json.dumps(d) for _, _, d in pairs), "no file names in telemetry"
