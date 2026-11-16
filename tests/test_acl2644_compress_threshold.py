"""ACL-2644: every video above the compress threshold (default 500 MB, `ACL_VIDEO_COMPRESS_OVER_MB`) is compressed before
upload; a video at or below it goes up unmodified. The plan counts the compressed size; without ffmpeg a video above the
threshold is refused before any write."""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tests.test_acl2370_chunk_budget as B  # noqa: E402
from src.fb_api import creator as cr  # noqa: E402
from tests.test_acl1891_video_chunks import MB, _big_draft, _run  # noqa: E402
from tests.test_acl2629_worker_upload import ServedMeta, worker_small  # noqa: E402,F401
from tests.test_acl2639_resumable_upload import _Prep  # noqa: E402


@pytest.fixture
def ffmpeg_present(monkeypatch):
    from src.fb_ads_rpa import creative_compress as cc
    monkeypatch.setattr(cc, "ffmpeg_exe", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr(cc, "duration_s", lambda path, **kw: 0.0)


@pytest.fixture
def ffmpeg_missing(monkeypatch):
    from src.fb_ads_rpa import creative_compress as cc
    monkeypatch.setattr(cc, "ffmpeg_exe", lambda: "")


def _fake_compressor(tmp_path, calls):
    def fn(path, limit_mb, target_mb):
        calls.append((path, limit_mb, target_mb))
        out = tmp_path / "squeezed.mp4"
        out.write_bytes(hashlib.sha256(b"squeezed").digest() * 20_000)          # ~0.6 MB
        return _Prep(str(out), 3.0, out.stat().st_size / MB)
    return fn


# ───────────────────────── the setting ─────────────────────────

def test_the_threshold_defaults_to_500_mb():
    assert cr.video_compress_over_mb({}) == 500.0
    assert cr.COMPRESS_OVER_FLAG == "ACL_VIDEO_COMPRESS_OVER_MB"


@pytest.mark.parametrize("raw,expected", [("250", 250.0), (" 1000 ", 1000.0), ("2.5", 2.5),
                                          ("", 500.0), ("garbage", 500.0), ("0", 500.0), ("-5", 500.0)])
def test_the_setting_overrides_the_threshold(raw, expected):
    assert cr.video_compress_over_mb({cr.COMPRESS_OVER_FLAG: raw}) == expected


def test_the_setting_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("ACL_VIDEO_COMPRESS_OVER_MB", "250")
    assert cr.video_compress_over_mb() == 250.0 and cr.video_compress_over_bytes() == 250 * MB
    monkeypatch.delenv("ACL_VIDEO_COMPRESS_OVER_MB")
    assert cr.video_compress_over_mb() == 500.0


def test_the_setting_moves_the_line_in_the_plan(tmp_path, monkeypatch, ffmpeg_present):
    monkeypatch.delenv("ACL_VIDEO_WORKER_UPLOAD", raising=False)
    monkeypatch.delenv("ACL_VIDEO_COMPRESS_OVER_MB", raising=False)
    plan = B._plan(tmp_path, [300], monkeypatch)
    (path,) = cr.video_files_of(plan)
    assert cr.plan_upload_sizes(plan)[path] == (300 * MB, False)               # 300 MB is below the default 500
    monkeypatch.setenv("ACL_VIDEO_COMPRESS_OVER_MB", "250")
    assert cr.plan_upload_sizes(plan)[path] == (250 * MB, True)                # ... and above an override of 250


# ───────────────────────── below / at / above, in a real run ─────────────────────────

@pytest.mark.parametrize("delta,squeezed", [(-1, False), (0, False), (1, True)])
def test_a_video_is_compressed_only_when_above_the_threshold(worker_small, tmp_path, monkeypatch, ffmpeg_present,
                                                             delta, squeezed):
    monkeypatch.setenv("ACL_VIDEO_COMPRESS_OVER_MB", "3")                      # 3 MB: small files stand in for 500 MB
    d, f = _big_draft(tmp_path, size=3 * MB + delta)
    original_sha = hashlib.sha256(f.read_bytes()).hexdigest()
    calls: list = []
    meta = ServedMeta(meta_chunk=MB)
    out, _client = _run(meta, d, max_calls=300, compress_to_fit=_fake_compressor(tmp_path, calls))
    assert out.error == "", out.error
    sent = next(iter(meta.sessions.values()))
    assert sent["done"]
    if squeezed:
        assert len(calls) == 1 and calls[0][1] == 3.0 and calls[0][2] == pytest.approx(3.0 * 0.95)   # at most the threshold
        assert sent["sha"].hexdigest() != original_sha
        assert any("compressed from" in w and "above 3 MB" in w for w in out.warnings), out.warnings
    else:
        assert calls == []
        assert sent["sha"].hexdigest() == original_sha                          # the very same bytes
        assert not any("compressed from" in w for w in out.warnings)


def _failing(calls, reason="compression failed (test)"):
    def failing(path, limit_mb, target_mb):
        calls.append(path)
        return _Prep(path, 3.0, 3.0, False, reason)
    return failing


def test_a_failed_compression_that_fits_the_budget_sends_the_original_with_a_warning(worker_small, tmp_path, monkeypatch,
                                                                                    ffmpeg_present):
    monkeypatch.setenv("ACL_VIDEO_COMPRESS_OVER_MB", "3")
    d, f = _big_draft(tmp_path, size=3 * MB + 1)
    calls: list = []
    meta = ServedMeta(meta_chunk=MB)
    out, _ = _run(meta, d, max_calls=300, compress_to_fit=_failing(calls))
    assert out.error == "", out.error
    assert len(calls) == 1                                                      # no second encode by the budget fallback
    warn = [w for w in out.warnings if "could not be compressed under 3 MB" in w]
    assert warn and "compression failed (test)" in warn[0] and "original is uploaded" in warn[0], out.warnings
    assert next(iter(meta.sessions.values()))["sha"].hexdigest() == hashlib.sha256(f.read_bytes()).hexdigest()


def test_a_failed_compression_that_does_not_fit_the_budget_stops_cleanly_with_the_reason(worker_small, tmp_path,
                                                                                         monkeypatch, ffmpeg_present):
    monkeypatch.setenv("ACL_VIDEO_COMPRESS_OVER_MB", "3")
    d, _f = _big_draft(tmp_path, size=3 * MB + 1)
    monkeypatch.setattr(cr.CampaignCreator, "_chunk_spare", lambda self, plan: 0)   # the run has no calls to spare
    calls: list = []
    meta = ServedMeta(meta_chunk=MB)
    out, _ = _run(meta, d, max_calls=300, compress_to_fit=_failing(calls, "compression would not help (test)"))
    assert out.stop_kind == "video_error", (out.stop_kind, out.error)
    assert "compression would not help (test)" in out.error and "Nothing was uploaded" in out.error
    assert not meta.sessions and len(calls) == 1


def test_a_copy_above_the_threshold_is_not_accepted(worker_small, tmp_path, monkeypatch, ffmpeg_present):
    monkeypatch.setenv("ACL_VIDEO_COMPRESS_OVER_MB", "3")
    d, f = _big_draft(tmp_path, size=4 * MB)

    def too_big(path, limit_mb, target_mb):
        out = tmp_path / "bigcopy.mp4"
        out.write_bytes(hashlib.sha256(b"x").digest() * (int(3.5 * MB) // 32))      # smaller than the original, above 3 MB
        return _Prep(str(out), 4.0, 3.5)

    meta = ServedMeta(meta_chunk=MB)
    out, _ = _run(meta, d, max_calls=300, compress_to_fit=too_big)
    assert out.error == "", out.error
    assert next(iter(meta.sessions.values()))["sha"].hexdigest() == hashlib.sha256(f.read_bytes()).hexdigest()
    assert any("could not be compressed under 3 MB" in w and "above the limit" in w for w in out.warnings), out.warnings


def test_a_copy_made_for_the_threshold_is_never_encoded_again_by_the_budget_fallback(worker_small, tmp_path, monkeypatch,
                                                                                    ffmpeg_present):
    monkeypatch.setenv("ACL_VIDEO_COMPRESS_OVER_MB", "3")
    d, f = _big_draft(tmp_path, size=4 * MB)
    plan = cr.plan_from_draft(d)
    calls: list = []
    creator, _ = B._creator(ServedMeta(meta_chunk=MB), writes=False, max_calls=300)
    creator.worker_path = True
    creator.compress_to_fit = _fake_compressor(tmp_path, calls)
    out = cr.Outcome(campaign_name=plan.name)
    big = cr.LargeFile(str(f), 4 * MB, creator.read_range)
    copy = creator._compress_over_threshold(out, plan, big, "ad 1")
    assert copy.path != big.path and len(calls) == 1
    monkeypatch.setattr(cr.CampaignCreator, "_chunk_spare", lambda self, plan: 1)   # tight: the fallback would encode again
    assert creator._fit_for_chunks(out, plan, copy, "ad 1") is copy and len(calls) == 1


def test_the_plans_smaller_target_is_encoded_once_straight_from_the_original(worker_small, tmp_path, monkeypatch,
                                                                              ffmpeg_present):
    monkeypatch.setenv("ACL_VIDEO_COMPRESS_OVER_MB", "3")
    d, f = _big_draft(tmp_path, size=4 * MB)
    plan = cr.plan_from_draft(d)
    calls: list = []
    creator, _ = B._creator(ServedMeta(meta_chunk=MB), writes=False, max_calls=300)
    creator.worker_path = True
    creator.compress_to_fit = _fake_compressor(tmp_path, calls)
    creator._video_plan = {str(f): (int(2 * MB), True)}                          # the budget plan chose 2 MB
    creator._compress_over_threshold(cr.Outcome(campaign_name=plan.name), plan,
                                     cr.LargeFile(str(f), 4 * MB, creator.read_range), "ad 1")
    assert len(calls) == 1 and calls[0][0] == str(f) and calls[0][1] == pytest.approx(2.0)
    assert calls[0][2] == pytest.approx(2.0 * 0.95)


# ───────────────────────── no ffmpeg: refused before any write ─────────────────────────

def test_without_ffmpeg_a_video_above_the_threshold_is_refused_before_any_write(worker_small, tmp_path, monkeypatch,
                                                                                ffmpeg_missing):
    monkeypatch.setenv("ACL_VIDEO_COMPRESS_OVER_MB", "3")
    d, _f = _big_draft(tmp_path, size=3 * MB + 1)
    calls: list = []
    meta = ServedMeta(meta_chunk=MB)
    out, client = _run(meta, d, max_calls=300, compress_to_fit=_fake_compressor(tmp_path, calls))
    assert out.error and "ffmpeg" in out.error and "Nothing was created" in out.error, out.error
    assert client.writes == 0 and calls == [] and not meta.sessions


def test_the_refusal_is_a_plan_error_with_the_creative_key(worker_small, tmp_path, monkeypatch, ffmpeg_missing):
    monkeypatch.setenv("ACL_VIDEO_COMPRESS_OVER_MB", "3")
    d, _f = _big_draft(tmp_path, size=3 * MB + 1)
    plan = cr.plan_from_draft(d)
    creator, _ = B._creator(ServedMeta(meta_chunk=MB), writes=False, max_calls=300)
    with pytest.raises(cr.PlanError) as err:
        creator._prepare(plan, writing=False, out=cr.Outcome(campaign_name=plan.name))
    assert "above 3 MB" in str(err.value) and "ffmpeg" in str(err.value)
    assert getattr(err.value, "key", "") == "creative_file"


@pytest.mark.parametrize("delta", [-1, 0])
def test_without_ffmpeg_a_video_up_to_the_threshold_still_goes_up(worker_small, tmp_path, monkeypatch, ffmpeg_missing, delta):
    monkeypatch.setenv("ACL_VIDEO_COMPRESS_OVER_MB", "3")
    d, f = _big_draft(tmp_path, size=3 * MB + delta)
    meta = ServedMeta(meta_chunk=MB)
    out, _ = _run(meta, d, max_calls=300)
    assert out.error == "", out.error
    assert next(iter(meta.sessions.values()))["sha"].hexdigest() == hashlib.sha256(f.read_bytes()).hexdigest()


# ───────────────────────── the plan counts the compressed size ─────────────────────────

def test_the_plan_counts_the_compressed_size_of_a_video_above_the_threshold(tmp_path, monkeypatch, ffmpeg_present):
    monkeypatch.delenv("ACL_VIDEO_WORKER_UPLOAD", raising=False)
    monkeypatch.delenv("ACL_VIDEO_RESUMABLE_UPLOAD", raising=False)
    monkeypatch.delenv("ACL_VIDEO_COMPRESS_OVER_MB", raising=False)
    plan = B._plan(tmp_path, [900, 400], monkeypatch)
    big, ok = cr.video_files_of(plan)
    planned = cr.plan_upload_sizes(plan)                                        # no spare known: only the threshold acts
    assert planned[big] == (500 * MB, True) and planned[ok] == (400 * MB, False)
    chunks = cr.video_upload_calls(500 * MB, True) + cr.video_upload_calls(400 * MB)
    assert cr.estimate_upload_calls(plan) >= chunks
    assert cr.estimate_upload_calls(plan) < cr.upload_calls(900 * MB, True, chunked=True) + chunks


def test_without_ffmpeg_the_plan_keeps_the_original_size(tmp_path, monkeypatch, ffmpeg_missing):
    monkeypatch.delenv("ACL_VIDEO_WORKER_UPLOAD", raising=False)
    plan = B._plan(tmp_path, [900], monkeypatch)
    (path,) = cr.video_files_of(plan)
    assert cr.plan_upload_sizes(plan)[path] == (900 * MB, False)


def test_check_plan_names_the_video_it_will_compress_and_its_planned_size(worker_small, tmp_path, monkeypatch, ffmpeg_present):
    monkeypatch.setenv("ACL_VIDEO_COMPRESS_OVER_MB", "3")
    monkeypatch.setattr(cr, "VIDEO_SINGLE_MAX_BYTES", MB)
    d, _f = _big_draft(tmp_path, size=3 * MB + 500_000)
    plan = cr.plan_from_draft(d)
    creator, _ = B._creator(ServedMeta(meta_chunk=MB), writes=False, max_calls=cr.estimate_create_calls(plan) + 200)
    out = cr.Outcome(campaign_name=plan.name)
    creator._prepare(plan, writing=False, out=out)
    notes = [w for w in out.warnings if w.startswith("Ad 1 video (")]
    assert notes and "will be compressed to about 3 MB before upload" in notes[0], out.warnings
    assert "resolution and aspect ratio stay the same" in notes[0]


# ───────────────────────── too long to compress; the 4 GB rule ─────────────────────────

def test_a_video_too_long_to_compress_is_planned_at_its_original_size_with_a_warning(tmp_path, monkeypatch, ffmpeg_present):
    from src.fb_ads_rpa import creative_compress as cc
    monkeypatch.setattr(cc, "duration_s", lambda path, **kw: 20000.0)            # ~5.5 h: 475 MB would be under 400 kbps
    monkeypatch.delenv("ACL_VIDEO_WORKER_UPLOAD", raising=False)
    monkeypatch.delenv("ACL_VIDEO_COMPRESS_OVER_MB", raising=False)
    plan = B._plan(tmp_path, [900], monkeypatch)
    (path,) = cr.video_files_of(plan)
    assert cr.video_cannot_compress(path, 900 * MB) and not cr.video_cannot_compress(path, 400 * MB)
    assert cr.plan_upload_sizes(plan)[path] == (900 * MB, False)                 # budgeted as the original
    assert cr.estimate_upload_calls(plan) >= cr.upload_calls(900 * MB, True, chunked=True)
    creator, _ = B._creator(ServedMeta(meta_chunk=MB), writes=False, max_calls=300)
    out = cr.Outcome(campaign_name=plan.name)
    creator._require_compressor_for_big_videos(plan, out)
    assert any(w == "Ad 1 video (900 MB) is too long to compress under 500 MB; the original will be uploaded."
               for w in out.warnings), out.warnings


def test_an_unknown_duration_is_not_a_prediction(tmp_path, monkeypatch, ffmpeg_present):
    plan = B._plan(tmp_path, [900], monkeypatch)                                  # ffmpeg_present: duration 0
    (path,) = cr.video_files_of(plan)
    assert not cr.video_cannot_compress(path, 900 * MB)


@pytest.mark.parametrize("writing", [False, True])
def test_check_plan_and_publish_refuse_an_original_above_4_gb(worker_small, tmp_path, monkeypatch, ffmpeg_present, writing):
    monkeypatch.delenv("ACL_VIDEO_COMPRESS_OVER_MB", raising=False)
    plan = B._plan(tmp_path, [5000], monkeypatch)                                 # 5000 MB: it would be planned at 500 MB
    creator, _ = B._creator(ServedMeta(meta_chunk=MB), writes=writing, max_calls=300)
    with pytest.raises(cr.PlanError) as err:
        creator._prepare(plan, writing=writing, out=cr.Outcome(campaign_name=plan.name))
    assert "larger than" in str(err.value) and "creative_file" == getattr(err.value, "key", "")
