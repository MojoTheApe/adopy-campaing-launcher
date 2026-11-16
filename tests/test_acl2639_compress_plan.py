"""ACL-2639: the fast upload is OFF by default; large videos are planned and budgeted chunked at Meta's real 5 MB windows,
and the ones that would not fit the run are planned (and, before the chunked upload, made) at the size of a compressed copy.
"""
from __future__ import annotations

import hashlib
import sys
from dataclasses import replace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tests.test_acl2370_chunk_budget as B  # noqa: E402
from src.fb_api import creator as cr  # noqa: E402
from src.web import publish_api  # noqa: E402
from tests.test_acl1761_api_create import ACT  # noqa: E402
from tests.test_acl1891_video_chunks import MB, _big_draft, _run  # noqa: E402
from tests.test_acl2629_worker_upload import ServedMeta, worker_small  # noqa: E402,F401
from tests.test_acl2639_resumable_upload import _Prep  # noqa: E402


# ───────────────────────── the flag ─────────────────────────

@pytest.mark.parametrize("raw,on", [("", False), ("0", False), ("false", False), ("off", False), ("garbage", False),
                                    ("1", True), ("true", True), ("YES", True), ("on", True)])
def test_the_fast_upload_is_on_only_when_explicitly_set(raw, on):
    assert cr.resumable_upload_enabled({cr.RESUMABLE_UPLOAD_FLAG: raw}) is on
    assert cr.resumable_upload_enabled({}) is False


def test_by_default_a_large_video_goes_straight_to_chunks_without_touching_the_file_api(worker_small, tmp_path, monkeypatch):
    monkeypatch.delenv("ACL_VIDEO_RESUMABLE_UPLOAD", raising=False)
    d, f = _big_draft(tmp_path)
    meta = ServedMeta(meta_chunk=MB)
    out, _ = _run(meta, d, max_calls=300)
    assert out.error == "", out.error
    assert not any("resumable" in w or "fast upload" in w for w in out.warnings)
    sess = next(iter(meta.sessions.values()))
    assert sess["done"] and sess["sha"].hexdigest() == hashlib.sha256(f.read_bytes()).hexdigest()


# ───────────────────────── the plan and the budget ─────────────────────────

def _budget_parts(plan):
    need = cr.estimate_create_calls(plan)
    reads = cr.estimate_targeting_reads(plan)
    hard_max = publish_api.DEFAULT_CREATE_MAX_CALLS + publish_api._tr_cap.JOB_READ_ABS_CAP
    return need, reads, hard_max


def test_several_medium_videos_with_stories_files_fit_one_run_uncompressed(tmp_path, monkeypatch):
    monkeypatch.delenv("ACL_VIDEO_RESUMABLE_UPLOAD", raising=False)
    monkeypatch.delenv("ACL_VIDEO_WORKER_UPLOAD", raising=False)
    # 3 ads, each 160 MB (main) + 70 MB (Stories / Reels): ~200 chunked calls at 5 MB windows
    plan = B._plan(tmp_path, [160, 160, 160], monkeypatch, vertical={0: 70, 1: 70, 2: 70})
    budget, refusal = publish_api.create_call_budget(plan)
    need, reads, hard_max = _budget_parts(plan)
    assert refusal == "", refusal
    chunks = cr.estimate_upload_calls(plan, spare=hard_max - need - reads)
    assert chunks > 150                                                         # a lot of calls ...
    assert not any(q for _s, q in cr.plan_upload_sizes(plan, spare=hard_max - need - reads).values())   # ... that all fit
    assert budget >= need + reads + chunks - 1 or budget == hard_max


def test_videos_that_would_not_fit_one_run_are_budgeted_at_their_compressed_size(tmp_path, monkeypatch):
    monkeypatch.delenv("ACL_VIDEO_RESUMABLE_UPLOAD", raising=False)
    monkeypatch.delenv("ACL_VIDEO_WORKER_UPLOAD", raising=False)
    plan = B._plan(tmp_path, [900, 900, 900], monkeypatch, vertical={0: 400, 1: 400, 2: 400})
    need, reads, hard_max = _budget_parts(plan)
    assert cr.estimate_upload_calls(plan) > hard_max                           # the originals would never fit
    budget, refusal = publish_api.create_call_budget(plan)
    assert refusal == "", refusal                                              # ... but the compressed plan does
    planned = cr.plan_upload_sizes(plan, spare=hard_max - need - reads)
    assert any(q for _s, q in planned.values())
    assert all(size <= cr.COMPRESS_FALLBACK_CAP_MB * MB for size, _ in planned.values())
    assert need + reads < budget <= hard_max
    monkeypatch.setenv("ACL_VIDEO_COMPRESS_FALLBACK", "0")                     # without the fallback: the old refusal
    assert publish_api.create_call_budget(plan)[1] != ""


def test_videos_that_fit_chunked_are_never_planned_compressed(tmp_path, monkeypatch):
    monkeypatch.delenv("ACL_VIDEO_RESUMABLE_UPLOAD", raising=False)
    monkeypatch.delenv("ACL_VIDEO_WORKER_UPLOAD", raising=False)
    plan = B._plan(tmp_path, [70, 70], monkeypatch, vertical={0: 30, 1: 30})
    planned = cr.plan_upload_sizes(plan, spare=400)
    assert not any(squeezed for _s, squeezed in planned.values())
    assert sorted(size for size, _ in planned.values()) == sorted([70 * MB, 70 * MB, 30 * MB, 30 * MB])


def test_the_plan_never_compresses_when_the_fast_upload_is_on_or_the_fallback_is_off(tmp_path, monkeypatch):
    monkeypatch.setenv("ACL_VIDEO_COMPRESS_OVER_MB", "100000")   # ACL-2644: the size threshold is not what this test is about
    monkeypatch.delenv("ACL_VIDEO_WORKER_UPLOAD", raising=False)
    plan = B._plan(tmp_path, [2000, 2000], monkeypatch)
    monkeypatch.delenv("ACL_VIDEO_RESUMABLE_UPLOAD", raising=False)
    monkeypatch.setenv("ACL_VIDEO_COMPRESS_FALLBACK", "0")
    assert not any(q for _s, q in cr.plan_upload_sizes(plan, spare=100).values())
    monkeypatch.delenv("ACL_VIDEO_COMPRESS_FALLBACK")
    monkeypatch.setenv("ACL_VIDEO_RESUMABLE_UPLOAD", "1")
    assert not any(q for _s, q in cr.plan_upload_sizes(plan, spare=100).values())
    monkeypatch.delenv("ACL_VIDEO_RESUMABLE_UPLOAD")
    assert all(q for _s, q in cr.plan_upload_sizes(plan, spare=100).values())      # now they shrink


def test_without_a_known_spare_the_originals_are_planned(tmp_path, monkeypatch):
    monkeypatch.setenv("ACL_VIDEO_COMPRESS_OVER_MB", "100000")   # ACL-2644: above the threshold they are planned compressed
    monkeypatch.delenv("ACL_VIDEO_RESUMABLE_UPLOAD", raising=False)
    monkeypatch.delenv("ACL_VIDEO_WORKER_UPLOAD", raising=False)
    plan = B._plan(tmp_path, [2000], monkeypatch)
    assert cr.plan_upload_sizes(plan) == {p: (2000 * MB, False) for p in cr.video_files_of(plan)}


def test_a_compressed_plan_always_counts_a_chunked_upload(tmp_path, monkeypatch):
    monkeypatch.delenv("ACL_VIDEO_RESUMABLE_UPLOAD", raising=False)
    monkeypatch.delenv("ACL_VIDEO_WORKER_UPLOAD", raising=False)
    plan = B._plan(tmp_path, [900], monkeypatch)
    calls = cr.estimate_upload_calls(plan, spare=60)
    assert calls >= cr.CALLS_UPLOAD_FIXED + 1 and calls <= 60                          # squeezed, never "free"


# ───────────────────────── Check plan names the compression ─────────────────────────

def _check_plan(tmp_path, monkeypatch, *, extra_calls):
    monkeypatch.delenv("ACL_VIDEO_RESUMABLE_UPLOAD", raising=False)
    monkeypatch.setattr(cr, "VIDEO_SINGLE_MAX_BYTES", MB)                       # (the Check plan reads it as a large file)
    d, f = _big_draft(tmp_path)
    plan = cr.plan_from_draft(d)
    creator, client = B._creator(ServedMeta(meta_chunk=MB), writes=False,
                                 max_calls=cr.estimate_create_calls(plan) + extra_calls)
    out = cr.Outcome(campaign_name=plan.name)
    creator._prepare(plan, writing=False, out=out)
    return out


def test_check_plan_says_which_video_will_be_compressed_to_what(worker_small, tmp_path, monkeypatch):
    out = _check_plan(tmp_path, monkeypatch, extra_calls=16)
    notes = [w for w in out.warnings if w.startswith("Ad 1 video (")]
    assert notes and "will be compressed to about" in notes[0] and "before upload" in notes[0], out.warnings
    assert "resolution and aspect ratio stay the same" in notes[0]


def test_check_plan_has_no_compress_note_when_everything_fits(worker_small, tmp_path, monkeypatch):
    out = _check_plan(tmp_path, monkeypatch, extra_calls=200)
    assert not [w for w in out.warnings if "will be compressed" in w]
    assert any(w.startswith("Large video: about") for w in out.warnings)


# ───────────────────────── a run with several large videos ─────────────────────────

def _three_videos(tmp_path):
    d, f = _big_draft(tmp_path)
    ad = d.ad_sets[0].ads[0]
    for i in (2, 3):
        extra = Path(str(ad.creative_path) + f".{i}.mp4")
        extra.write_bytes(hashlib.sha256(b"video%d" % i).digest() * 120_000)           # ~3.6 MB, different bytes
        d.ad_sets[0].ads.append(replace(ad, creative_id=str(1000 + i), creative_filename=extra.name,
                                        creative_path=str(extra), copy_name=f"Text{i}", ad_title=f"Head {i}",
                                        ad_body=f"Body {i}"))
    return d, f


def test_a_campaign_with_several_large_videos_compresses_what_does_not_fit_and_completes(worker_small, tmp_path, monkeypatch):
    monkeypatch.delenv("ACL_VIDEO_RESUMABLE_UPLOAD", raising=False)
    d, f = _three_videos(tmp_path)
    plan = cr.plan_from_draft(d)
    budget = cr.estimate_create_calls(plan) + 40                    # the three originals (~13 calls each) do not fit
    made = []

    def compress(path, limit_mb, target_mb):
        out_file = tmp_path / f"small{len(made)}.mp4"
        out_file.write_bytes(hashlib.sha256(path.encode()).digest() * 20_000)          # ~0.6 MB
        made.append((path, limit_mb))
        return _Prep(str(out_file), 3.6, 0.6)

    meta = ServedMeta(meta_chunk=MB)
    out, client = _run(meta, d, max_calls=budget, compress_to_fit=compress)
    assert out.error == "", out.error
    assert len(out.created["ads"]) == 3 and len(out.created["videos"]) == 3
    assert len(made) >= 1 and client.calls <= client.max_calls
    assert sum(1 for s in meta.sessions.values() if s["done"]) == 3
    assert any(w.startswith("Ad ") and "video compressed from" in w for w in out.warnings)


def test_without_the_fallback_the_same_campaign_stops_cleanly_before_any_chunk(worker_small, tmp_path, monkeypatch):
    monkeypatch.delenv("ACL_VIDEO_RESUMABLE_UPLOAD", raising=False)
    monkeypatch.setenv("ACL_VIDEO_COMPRESS_FALLBACK", "0")
    d, f = _three_videos(tmp_path)
    plan = cr.plan_from_draft(d)
    meta = ServedMeta(meta_chunk=MB)
    out, client = _run(meta, d, max_calls=cr.estimate_create_calls(plan) + 40)
    assert out.stop_kind in ("video_error", "plan") and client.calls <= client.max_calls


# ───────────────────────── ffmpeg gate, ETA, cache, reasons, shared helper ─────────────────────────

@pytest.fixture
def ffmpeg_present(monkeypatch):
    from src.fb_ads_rpa import creative_compress as cc
    monkeypatch.setattr(cc, "ffmpeg_exe", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr(cc, "duration_s", lambda path, **kw: 0.0)


@pytest.fixture
def ffmpeg_missing(monkeypatch):
    from src.fb_ads_rpa import creative_compress as cc
    monkeypatch.setattr(cc, "ffmpeg_exe", lambda: "")


def test_compression_is_planned_only_when_ffmpeg_is_there(tmp_path, monkeypatch, ffmpeg_present):
    monkeypatch.delenv("ACL_VIDEO_RESUMABLE_UPLOAD", raising=False)
    monkeypatch.delenv("ACL_VIDEO_WORKER_UPLOAD", raising=False)
    plan = B._plan(tmp_path, [2000, 2000], monkeypatch)
    assert all(q for _s, q in cr.plan_upload_sizes(plan, spare=100).values())
    from src.fb_ads_rpa import creative_compress as cc
    monkeypatch.setattr(cc, "ffmpeg_exe", lambda: "")
    assert not any(q for _s, q in cr.plan_upload_sizes(plan, spare=100).values())      # originals: refused later, clearly


def test_without_ffmpeg_a_run_that_would_not_fit_is_refused_before_the_first_write(worker_small, tmp_path, monkeypatch,
                                                                                   ffmpeg_missing):
    with pytest.raises(cr.PlanError) as err:
        _check_plan(tmp_path, monkeypatch, extra_calls=8)
    assert "needs ffmpeg" in str(err.value) and "smaller file" in str(err.value) and "Nothing was created" in str(err.value)


def test_check_plan_shows_the_expected_compression_time(worker_small, tmp_path, monkeypatch, ffmpeg_present):
    from src.fb_ads_rpa import creative_compress as cc
    monkeypatch.setattr(cc, "duration_s", lambda path, **kw: 600.0)
    out = _check_plan(tmp_path, monkeypatch, extra_calls=16)
    note = next(w for w in out.warnings if w.startswith("Ad 1 video ("))
    assert "about 10 min, can take up to" in note


def test_the_compression_timeout_scales_with_the_duration():
    from src.fb_ads_rpa import creative_compress as cc
    assert cc.pass_timeout(0) == 3600 and cc.pass_timeout(60) == 3600
    assert cc.pass_timeout(3600) == 3 * 3600 + 300 and cc.pass_timeout(7200) > cc.pass_timeout(3600)
    assert cc.eta_text(0) == "" and cc.eta_text(600).startswith("about 10 min, can take up to ")


def test_the_encoder_gets_the_scaled_timeout(tmp_path, monkeypatch):
    from src.fb_ads_rpa import creative_compress as cc
    src = tmp_path / "in.mp4"
    src.write_bytes(bytes(60_000_000))
    monkeypatch.setattr(cc, "cache_dir", lambda: tmp_path / "cache")
    monkeypatch.setattr(cc, "ffmpeg_exe", lambda: "ffmpeg")
    seen = []

    def run(args, timeout, tick=None):
        seen.append(timeout)
        if "Duration" not in " ".join(args) and "-pass" in args and args[args.index("-pass") + 1] == "2":
            Path(args[-1]).write_bytes(b"y" * 100)
        return 0, "Duration: 00:10:00.00, start"

    prep = cc.ensure_uploadable(str(src), limit_mb=45.0, target_mb=40.0, run=run)
    assert prep.compressed and cc.pass_timeout(600) == 3600 and 3600 in seen                 # a 10 min video: the 3600 s floor per pass


def test_the_cache_drops_old_copies_and_keeps_the_total_under_the_cap(tmp_path, monkeypatch):
    import os
    import time
    from src.fb_ads_rpa import creative_compress as cc
    root = tmp_path / "cache"
    root.mkdir()
    monkeypatch.setattr(cc, "cache_dir", lambda: root)
    now = time.time()
    day = 86400

    def make(name, age_days, size=1000):
        f = root / name
        f.write_bytes(b"x" * size)
        os.utime(f, (now - age_days * day, now - age_days * day))
        return f

    old, fresh, keep_old = make("old.mp4", 10), make("fresh.mp4", 1), make("keep.mp4", 30)
    other = make("note.txt", 90)
    stale = root / "encode-abc"
    stale.mkdir()
    (stale / "creative.mp4").write_bytes(b"z")
    os.utime(stale, (now - 3 * day, now - 3 * day))
    removed = cc.cleanup_cache(keep=str(keep_old), now=now)
    assert not old.exists() and fresh.exists() and keep_old.exists() and other.exists() and not stale.exists()
    assert removed == 2
    # the total cap: the oldest go first, the one in use never
    a, b, c = make("a.mp4", 3, 400), make("b.mp4", 2, 400), make("c.mp4", 1, 400)
    cc.cleanup_cache(keep=str(c), max_total_gb=900 / 1024 ** 3, now=now)
    assert not a.exists() and c.exists()
    assert sum(f.stat().st_size for f in root.glob("*.mp4")) <= 900 + 1000                  # fresh/keep sizes included


def test_a_failed_compression_logs_a_short_scrubbed_reason(tmp_path, monkeypatch, caplog):
    import logging
    from src.fb_ads_rpa import creative_compress as cc
    src = tmp_path / "secret_name.mp4"
    src.write_bytes(bytes(60_000_000))
    monkeypatch.setattr(cc, "cache_dir", lambda: tmp_path / "cache")
    monkeypatch.setattr(cc, "ffmpeg_exe", lambda: "ffmpeg")

    def run(args, timeout, tick=None):
        if "-i" in args and "-pass" not in args:
            return 0, "Duration: 00:10:00.00"
        return 1, f"error {src}: Invalid data found when processing input\n/home/bob/x.mp4: Conversion failed!"

    caplog.set_level(logging.WARNING)
    prep = cc.ensure_uploadable(str(src), limit_mb=45.0, target_mb=40.0, run=run)
    assert not prep.compressed and prep.reason.startswith("compression failed (ffmpeg pass 1 failed (")
    assert "secret_name" not in prep.reason and "bob" not in prep.reason and len(prep.reason) < 220
    assert "video compression failed" in caplog.text and "secret_name" not in caplog.text


def test_the_stop_message_carries_the_reason_compression_was_not_possible(worker_small, tmp_path, monkeypatch):
    from tests.test_acl2639_resumable_upload import ResumableMeta, _fake_compressor
    monkeypatch.setenv("ACL_VIDEO_RESUMABLE_UPLOAD", "1")
    d, f = _big_draft(tmp_path)
    monkeypatch.setattr(cr.CampaignCreator, "_chunk_spare", lambda self, plan: 9)
    why = "compression would not help: it needs under 400 kbps"
    out, _ = _run(ResumableMeta(start="denied"), d, max_calls=300,
                  compress_to_fit=_fake_compressor(tmp_path, [], compressed=False, reason=why))
    assert out.stop_kind == "video_error" and f"compression was not possible: {why}" in out.error


def test_one_helper_counts_a_video_for_the_plan_the_budget_and_the_run(tmp_path, monkeypatch, ffmpeg_present):
    monkeypatch.delenv("ACL_VIDEO_RESUMABLE_UPLOAD", raising=False)
    monkeypatch.delenv("ACL_VIDEO_WORKER_UPLOAD", raising=False)
    assert cr.video_upload_calls(100 * MB) == cr.upload_calls(100 * MB)
    assert cr.video_upload_calls(1 * MB, True) == cr.CALLS_UPLOAD_FIXED + 1                 # a squeezed copy is never free
    assert cr.video_upload_calls(400 * MB, True) == cr.upload_calls(400 * MB, True, chunked=True)
    plan = B._plan(tmp_path, [100, 100], monkeypatch)
    sizes = cr.plan_upload_sizes(plan, spare=1000)
    chunks = sum(cr.video_upload_calls(s, q) for s, q in sizes.values())
    assert cr.estimate_upload_calls(plan, spare=1000) == chunks + max(cr.CHUNK_RETRY_SLACK_MIN, -(-chunks * 5 // 100))
