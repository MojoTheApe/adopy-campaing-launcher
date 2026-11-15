"""ACL-2370: бюджет вызовов кампании считает куски больших видео (>200 МБ), а не только записи.

Размеры подменяются через `cr._disk_size` (настоящие гигабайтные файлы не создаются).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tests.test_acl1768_ad_sets as T  # noqa: E402
from src.fb_api import creator as cr  # noqa: E402
from src.fb_api import graph as g  # noqa: E402
from src.web import publish_api  # noqa: E402
from tests.test_acl1761_api_create import ACT  # noqa: E402
from tests.test_acl1891_video_chunks import ChunkMeta, _big_draft, _run, small  # noqa: E402,F401

MiB = 1024 * 1024
CEILING = publish_api.DEFAULT_CREATE_MAX_CALLS + 90


@pytest.fixture(autouse=True)
def _no_compressor(monkeypatch):
    """Default: no ffmpeg and no cached copy, so the original size is counted (pre-ACL-2621 behaviour)."""
    from src.fb_ads_rpa import creative_compress as cc
    monkeypatch.setattr(cc, "ffmpeg_exe", lambda: "")
    monkeypatch.delenv("ACL_VIDEO_COMPRESS_ABOVE_MB", raising=False)


def _slack(chunks: int) -> int:
    return chunks + max(2, -(-chunks * 5 // 100))


def _plan(tmp_path, sizes_mib: list[int], monkeypatch, *, sets: int = 1, vertical: dict | None = None):
    """План из видео-объявлений (по одному на размер); `vertical` - {индекс объявления: МиБ}."""
    sizes: dict[str, int] = {}
    d = T._draft(tmp_path, sets=2, ads=max(1, len(sizes_mib) // sets))
    d.ad_sets = d.ad_sets[:sets]
    plan = cr.plan_from_draft(d)
    ads = [a for s in plan.ad_sets for a in s.ads]
    for ad, mib in zip(ads, sizes_mib):
        ad.path = str(tmp_path / f"v{ad.index}.mp4")
        ad.kind = "video"
        sizes[ad.path] = mib * MiB
    for idx, mib in (vertical or {}).items():
        ad = ads[idx]
        ad.vertical_path = str(tmp_path / f"v{ad.index}_story.mp4")
        ad.vertical_kind = "video"
        sizes[ad.vertical_path] = mib * MiB
    monkeypatch.setattr(cr, "_disk_size", lambda p: sizes.get(p, 100))
    return plan


def _creator(meta, *, writes, max_calls):
    client = g.GraphClient(meta, allow_writes=writes, sleep=lambda s: None, min_interval_s=0.0, max_calls=max_calls)
    facts = cr.preflight_account(client, ACT)
    return cr.CampaignCreator(client, facts, cr.TargetingResolver(client), sleep=lambda s: None), client


def test_small_videos_and_images_add_nothing_and_keep_the_floor(tmp_path, monkeypatch):
    plain = cr.plan_from_draft(T._draft(tmp_path, sets=2, ads=2))
    assert cr.estimate_upload_calls(plain) == 0
    assert publish_api.create_call_budget(plain) == (150, "")
    at_threshold = _plan(tmp_path, [200], monkeypatch)          # ровно на пороге - один запрос
    assert cr.estimate_upload_calls(at_threshold) == 0
    assert publish_api.create_call_budget(at_threshold)[0] == 150


def test_a_gigabyte_video_grows_the_budget_by_its_chunks(tmp_path, monkeypatch):
    plan = _plan(tmp_path, [1000], monkeypatch)
    assert cr.upload_calls(1000 * MiB) == 128
    assert cr.estimate_upload_calls(plan) == _slack(128)
    budget, refusal = publish_api.create_call_budget(plan)
    assert refusal == ""
    assert budget == cr.estimate_create_calls(plan) + cr.estimate_targeting_reads(plan) + _slack(128)


def test_the_owner_1_29_gb_video_is_accepted(tmp_path, monkeypatch):
    plan = _plan(tmp_path, [1230], monkeypatch)                 # ~1,29 ГБ (десятичных) = 1230 МиБ
    chunks = cr.upload_calls(1230 * MiB)
    assert 150 <= chunks <= 165
    budget, refusal = publish_api.create_call_budget(plan)
    assert refusal == "" and budget >= chunks + cr.estimate_create_calls(plan)


def test_the_largest_file_fits_at_defaults_and_stays_under_the_ceiling(tmp_path, monkeypatch):
    plan = _plan(tmp_path, [1536], monkeypatch)
    assert cr.upload_calls(1536 * MiB) == 195
    budget, refusal = publish_api.create_call_budget(plan)
    assert refusal == "" and budget <= CEILING
    assert budget >= 195 + cr.estimate_create_calls(plan)


def test_two_largest_videos_are_refused_with_exact_numbers(tmp_path, monkeypatch):
    plan = _plan(tmp_path, [1536, 1536], monkeypatch, sets=2)
    budget, refusal = publish_api.create_call_budget(plan)
    chunks = cr.estimate_upload_calls(plan)
    rest = cr.estimate_create_calls(plan) + cr.estimate_targeting_reads(plan)
    assert budget == CEILING
    assert refusal == (f"This campaign needs about {chunks + rest} Meta calls ({chunks} to upload large videos in "
                       f"chunks, {rest} for the rest), more than ACL allows in one run ({CEILING}). "
                       "This is ACL's own per-run limit, not Meta's. Let ACL compress the video "
                       "(needs ffmpeg), use a shorter video, or split the campaign. Nothing was created.")


def test_a_lowered_env_ceiling_refuses_a_big_video(tmp_path, monkeypatch):
    plan = _plan(tmp_path, [1000], monkeypatch)
    monkeypatch.setenv(publish_api.CREATE_MAX_CALLS_ENV, "160")
    budget, refusal = publish_api.create_call_budget(plan)
    assert budget == 160 and refusal.startswith("This campaign needs about ") and "(160)" in refusal


def test_writes_over_the_cap_keep_the_old_split_it_text(tmp_path):
    plan = cr.plan_from_draft(T._draft(tmp_path, sets=10, ads=4))
    need = cr.estimate_create_calls(plan)
    assert need > publish_api.DEFAULT_CREATE_MAX_CALLS
    assert publish_api.create_call_budget(plan)[1] == (
        f"This campaign needs about {need} Meta calls, more than one run allows — split it. Nothing was created.")


def test_mixed_creatives_count_only_large_files_including_the_vertical_one(tmp_path, monkeypatch):
    plan = _plan(tmp_path, [50, 300], monkeypatch, sets=2, vertical={0: 400})
    expected = cr.upload_calls(300 * MiB) + cr.upload_calls(400 * MiB)
    assert cr.estimate_upload_calls(plan) == _slack(expected)


def test_one_file_in_two_ads_is_counted_once(small, tmp_path):
    d, f = _big_draft(tmp_path)
    plan = cr.plan_from_draft(d)
    ad = plan.ad_sets[0].ads[0]
    second = cr.plan_from_draft(d).ad_sets[0].ads[0]            # то же видео вторым объявлением
    second.index = 2
    plan.ad_sets[0].ads.append(second)
    assert cr.video_files_of(plan) == [ad.path]
    assert cr.estimate_upload_calls(plan, size_of=lambda p: f.stat().st_size) == _slack(cr.upload_calls(f.stat().st_size))


def test_the_estimate_covers_a_real_chunked_run(small, tmp_path):
    d, f = _big_draft(tmp_path)
    plan = cr.plan_from_draft(d)
    estimate = cr.estimate_upload_calls(plan)
    meta = ChunkMeta()
    budget = cr.estimate_create_calls(plan) + estimate
    out, client = _run(meta, d, max_calls=budget)
    assert out.error == "", out.error
    sent = len(meta.phases)                                      # start + куски + finish
    assert cr.upload_calls(f.stat().st_size) == sent + 1         # + 1 запас на сверку
    assert estimate >= sent + 1
    assert client.calls <= budget


@pytest.mark.parametrize("writing", [False, True])
def test_check_plan_and_publish_give_the_same_verdict(small, tmp_path, writing):
    d, f = _big_draft(tmp_path)
    plan = cr.plan_from_draft(d)
    tight = cr.upload_calls(f.stat().st_size) + cr.estimate_create_calls(plan) - 1
    meta = ChunkMeta()
    creator, _client = _creator(meta, writes=writing, max_calls=tight)
    with pytest.raises(cr.PlanError) as err:
        creator._prepare(plan, writing=writing, out=cr.Outcome(campaign_name=plan.name))
    assert "Large videos need about" in str(err.value)
    assert meta.posts == []


def test_check_plan_warns_about_a_large_video_with_the_call_count(small, tmp_path):
    d, f = _big_draft(tmp_path)
    plan = cr.plan_from_draft(d)
    chunks = cr.upload_calls(f.stat().st_size)
    budget = cr.estimate_create_calls(plan) + cr.estimate_upload_calls(plan)
    creator, _client = _creator(ChunkMeta(), writes=False, max_calls=budget)
    out = cr.Outcome(campaign_name=plan.name)
    creator._prepare(plan, writing=False, out=out)
    n = -(-f.stat().st_size // cr.VIDEO_CHUNK_MAX)
    assert [w for w in out.warnings if w.startswith("Large video")] == [
        f"Large video: about {chunks} Meta calls to upload in {n} chunks of 1 MB, "
        f"{chunks + cr.estimate_create_calls(plan)} of {budget} calls planned for this run "
        "(upper bound; ACL compresses large videos before upload, "
        "so the real upload is usually smaller)."]


def test_mutation_dropping_chunks_from_the_budget_is_caught(tmp_path, monkeypatch):
    plan = _plan(tmp_path, [1000], monkeypatch)
    good = publish_api.create_call_budget(plan)[0]
    monkeypatch.setattr(cr, "estimate_upload_calls", lambda *a, **k: 0)
    assert publish_api.create_call_budget(plan)[0] < good


# ---- ACL-2621: the estimate counts the size that will actually be uploaded ----

def test_a_2_5_gb_compressible_video_passes_and_is_counted_compressed(tmp_path, monkeypatch):
    from src.fb_ads_rpa import creative_compress as cc
    plan = _plan(tmp_path, [2500], monkeypatch)
    assert cr.upload_calls(2500 * MiB) > 300                    # the original alone would be refused
    monkeypatch.setattr(cc, "ffmpeg_exe", lambda: "ffmpeg")
    limit = int(cc.DEFAULT_LIMIT_MB * MiB)
    assert cr.planned_upload_size(plan.ad_sets[0].ads[0].path, 2500 * MiB) == limit
    assert cr.estimate_upload_calls(plan) == 0                  # 45 MB goes in one request
    budget, refusal = publish_api.create_call_budget(plan)
    assert refusal == "" and budget <= CEILING


def test_an_existing_cached_copy_is_counted_by_its_own_size(tmp_path, monkeypatch):
    from src.fb_ads_rpa import creative_compress as cc
    plan = _plan(tmp_path, [2500], monkeypatch)
    path = plan.ad_sets[0].ads[0].path
    copy = tmp_path / "cached.mp4"
    copy.write_bytes(b"x")
    monkeypatch.setattr(cc, "cache_path", lambda src, target: copy)
    real = cr._disk_size
    monkeypatch.setattr(cr, "_disk_size", lambda p: 30 * MiB if p == str(copy) else real(p))
    assert cr.planned_upload_size(path, 2500 * MiB) == 30 * MiB


def test_the_compression_threshold_follows_the_env(tmp_path, monkeypatch):
    from src.fb_ads_rpa import creative_compress as cc
    monkeypatch.setattr(cc, "ffmpeg_exe", lambda: "ffmpeg")
    monkeypatch.setenv("ACL_VIDEO_COMPRESS_ABOVE_MB", "100")
    assert cr.planned_upload_size(str(tmp_path / "x.mp4"), 2500 * MiB) == 100 * MiB
    assert cr.planned_upload_size(str(tmp_path / "x.mp4"), 90 * MiB) == 90 * MiB


def test_a_2_5_gb_video_without_a_compressor_is_refused_with_honest_wording(tmp_path, monkeypatch):
    plan = _plan(tmp_path, [2500], monkeypatch)                 # autouse fixture: no ffmpeg
    assert cr.planned_upload_size(plan.ad_sets[0].ads[0].path, 2500 * MiB) == 2500 * MiB
    budget, refusal = publish_api.create_call_budget(plan)
    assert budget == CEILING
    assert "more than ACL allows in one run (" in refusal and "not Meta's" in refusal
    assert "compress" in refusal and "shorter video" in refusal and "split the campaign" in refusal
    assert "Meta refused" not in refusal


def _check_plan(plan, monkeypatch):
    budget, refusal = publish_api.create_call_budget(plan)
    creator, client = _creator(ChunkMeta(), writes=False, max_calls=budget)
    creator.size_of = lambda p: 2500 * MiB
    creator.read_file = lambda p: b"x" * 10
    out = cr.Outcome(campaign_name=plan.name)
    return refusal, creator, out


def test_check_plan_prepare_accepts_a_2_5_gb_compressible_video(tmp_path, monkeypatch):
    from src.fb_ads_rpa import creative_compress as cc
    plan = _plan(tmp_path, [2500], monkeypatch)
    monkeypatch.setattr(cc, "ffmpeg_exe", lambda: "ffmpeg")
    refusal, creator, out = _check_plan(plan, monkeypatch)
    assert refusal == ""
    creator._prepare(plan, writing=False, out=out)              # no PlanError


def test_check_plan_prepare_refuses_a_2_5_gb_video_without_a_compressor(tmp_path, monkeypatch):
    plan = _plan(tmp_path, [2500], monkeypatch)                 # autouse fixture: no ffmpeg
    _, creator, out = _check_plan(plan, monkeypatch)
    with pytest.raises(cr.PlanError):
        creator._prepare(plan, writing=False, out=out)
