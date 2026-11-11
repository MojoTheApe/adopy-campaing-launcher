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
                       f"chunks, {rest} for the rest), more than one run allows ({CEILING}). "
                       "Use a smaller or shorter video, or split the campaign. Nothing was created.")


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
        "(upper bound; the file may be compressed before upload)."]


def test_mutation_dropping_chunks_from_the_budget_is_caught(tmp_path, monkeypatch):
    plan = _plan(tmp_path, [1000], monkeypatch)
    good = publish_api.create_call_budget(plan)[0]
    monkeypatch.setattr(cr, "estimate_upload_calls", lambda *a, **k: 0)
    assert publish_api.create_call_budget(plan)[0] < good
