"""ACL-2000: the Graph API path compresses heavy videos first and survives a slow page.

Covers: a big video is compressed with the shared compressor before upload (fake ffmpeg), the cached copy
is reused on repeat (same bytes, same title), a missing ffmpeg falls back to the original with a warning,
a page timeout on one chunk is retried from the same offset in the same session with backoff, and the
job stops with the old message after the last attempt. Never a second upload session.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import creative_compress as cc  # noqa: E402
from src.fb_ads_rpa import run_telemetry  # noqa: E402
from src.fb_api import creator as cr  # noqa: E402
from src.fb_api import graph as g  # noqa: E402
from tests.test_acl1761_api_create import ACT  # noqa: E402
from tests.test_acl1891_video_chunks import MB, ChunkMeta, _big_draft, small  # noqa: E402,F401

TIMEOUT_REPLY = {"error": "no_file", "why": "Runtime.evaluate: the browser did not answer within 35 s"}
ENCODED = hashlib.sha256(b"encoded").digest() * 100_000      # 3.2 MB, deterministic


class TimeoutMeta(ChunkMeta):
    """ChunkMeta whose page does not answer for the next `timeouts` transfer calls (nothing reaches Meta)."""

    def __init__(self, timeouts=0, **kw):
        super().__init__(**kw)
        self.timeouts = timeouts

    def write(self, path, params, files=None, filename="", **k):
        if path.endswith("/advideos") and params.get("upload_phase") == "transfer" and self.timeouts > 0:
            self.timeouts -= 1
            self.phases.append(("transfer", int(params["start_offset"])))
            return dict(TIMEOUT_REPLY)
        return super().write(path, params, files, filename, **k)


class FakeFfmpeg:
    def __init__(self):
        self.encodes = 0

    def __call__(self, args, timeout):
        if "-pass" not in args:
            return 0, "  Duration: 00:02:50.00, start: 0.0, bitrate: 11900 kb/s"
        if args[args.index("-pass") + 1] == "2":
            self.encodes += 1
            Path(args[-1]).write_bytes(ENCODED)
        return 0, ""


@pytest.fixture
def ffmpeg(monkeypatch, tmp_path):
    fake = FakeFfmpeg()
    monkeypatch.setattr(cc, "ffmpeg_exe", lambda: "ffmpeg-fake")
    monkeypatch.setattr(cc, "cache_dir", lambda: tmp_path / "cache")
    fake.compress_video = lambda p: cc.ensure_uploadable(p, run=fake)
    return fake


@pytest.fixture
def beats(monkeypatch):
    calls = []
    monkeypatch.setattr(run_telemetry, "beat", lambda: calls.append(1))
    return calls


def _creator(meta, sleeps=None, **kw):
    client = g.GraphClient(meta, allow_writes=True, sleep=lambda s: None, min_interval_s=0.0, max_calls=300)
    facts = cr.preflight_account(client, ACT)
    sleeps = sleeps if sleeps is not None else []
    return cr.CampaignCreator(client, facts, cr.TargetingResolver(client), sleep=sleeps.append, **kw), client


def _run(meta, draft, sleeps=None, **kw):
    creator, client = _creator(meta, sleeps, **kw)
    return creator.run(cr.plan_from_draft(draft)), client


def _waits(out):
    import re
    return [int(m.group(1)) for st in out.steps for m in [re.search(r"retrying the same chunk in (\d+)s", st)] if m]


def _heavy(tmp_path):
    return _big_draft(tmp_path, size=46 * MB)


def test_a_heavy_video_is_compressed_before_the_chunked_upload(small, ffmpeg, tmp_path):
    d, f = _heavy(tmp_path)
    meta = ChunkMeta()
    out, _ = _run(meta, d, compress_video=ffmpeg.compress_video)
    assert out.error == "", out.error
    sess = next(iter(meta.sessions.values()))
    assert sess["size"] == len(ENCODED) and sess["sha"].hexdigest() == hashlib.sha256(ENCODED).hexdigest()
    assert any("video compressed 46 MB -> 3.1 MB" in s for s in out.steps)
    assert ffmpeg.encodes == 1 and not [w for w in out.warnings if "compress" in w]


def test_a_repeat_reuses_the_cached_copy_and_the_uploaded_video(small, ffmpeg, tmp_path):
    d, f = _heavy(tmp_path)
    meta = ChunkMeta()
    first, _ = _run(meta, d, compress_video=ffmpeg.compress_video)
    second, client = _run(meta, d, compress_video=ffmpeg.compress_video)
    assert first.error == "" and second.error == ""
    assert ffmpeg.encodes == 1                                   # cached copy, not encoded again
    assert second.created == {} and client.writes == 0          # video already in the ad account
    assert len(meta.sessions) == 1
    other = ChunkMeta()                                          # a fresh account gets the same bytes
    third, _ = _run(other, d, compress_video=ffmpeg.compress_video)
    assert third.error == "" and ffmpeg.encodes == 1
    assert next(iter(other.sessions.values()))["sha"].hexdigest() == hashlib.sha256(ENCODED).hexdigest()


def test_without_ffmpeg_the_original_goes_up_with_a_warning(small, monkeypatch, tmp_path):
    monkeypatch.setattr(cc, "ffmpeg_exe", lambda: "")
    monkeypatch.setattr(cc, "cache_dir", lambda: tmp_path / "cache")
    d, f = _heavy(tmp_path)
    meta = ChunkMeta()
    out, _ = _run(meta, d)
    assert out.error == "", out.error
    assert next(iter(meta.sessions.values()))["size"] == 46 * MB
    assert any("ffmpeg not found" in w and "not compressed" in w for w in out.warnings)


def test_a_crashing_compressor_falls_back_to_the_original(small, tmp_path):
    d, f = _heavy(tmp_path)

    def boom(path):
        raise RuntimeError("encoder exploded")

    meta = ChunkMeta()
    out, _ = _run(meta, d, compress_video=boom)
    assert out.error == "", out.error
    assert next(iter(meta.sessions.values()))["size"] == 46 * MB
    assert any("compression failed" in w for w in out.warnings)


def test_a_dry_run_does_not_encode(small, ffmpeg, tmp_path):
    d, f = _heavy(tmp_path)
    creator, _ = _creator(ChunkMeta(), compress_video=ffmpeg.compress_video)
    creator._prepare(cr.plan_from_draft(d), cr.Outcome(campaign_name="x"), writing=False)
    assert ffmpeg.encodes == 0


def test_a_slow_page_chunk_is_retried_from_the_same_offset_in_the_same_session(small, ffmpeg, beats, tmp_path):
    d, f = _heavy(tmp_path)
    meta = TimeoutMeta(timeouts=1)
    sleeps: list[float] = []
    out, client = _run(meta, d, sleeps, compress_video=ffmpeg.compress_video)
    assert out.error == "", out.error
    offsets = [o for p, o in meta.phases if p == "transfer"]
    assert offsets[:3] == [0, 0, MB]                             # the same offset again, then on
    assert len(meta.sessions) == 1 and [p for p, _ in meta.phases].count("start") == 1
    assert next(iter(meta.sessions.values()))["sha"].hexdigest() == hashlib.sha256(ENCODED).hexdigest()
    assert _waits(out) == [10] and client.stopped is None
    assert any("retrying the same chunk in 10s (attempt 1/3)" in s for s in out.steps)
    assert len(beats) >= 4                                       # the silence watcher sees activity


def test_three_retries_back_off_10_20_40_and_then_succeed(small, ffmpeg, tmp_path):
    d, f = _heavy(tmp_path)
    meta = TimeoutMeta(timeouts=3)
    sleeps: list[float] = []
    out, _ = _run(meta, d, sleeps, compress_video=ffmpeg.compress_video)
    assert out.error == "", out.error
    assert _waits(out) == [10, 20, 40] and len(meta.sessions) == 1


def test_after_the_last_attempt_the_job_stops_with_a_clear_message(small, ffmpeg, tmp_path):
    d, f = _heavy(tmp_path)
    meta = TimeoutMeta(timeouts=99)
    sleeps: list[float] = []
    out, _ = _run(meta, d, sleeps, compress_video=ffmpeg.compress_video)
    assert out.stop_kind == "page_failed"
    assert "did not answer within 35 s" in out.error
    assert _waits(out) == [10, 20, 40]
    assert len(meta.sessions) == 1 and "finish" not in [p for p, _ in meta.phases]
    assert [p for p, _ in meta.phases].count("transfer") == 4


def test_a_login_stop_is_never_resumed():
    client = g.GraphClient(ChunkMeta(), allow_writes=True, sleep=lambda s: None, min_interval_s=0.0)
    exc = g.PageFailed("the page refused the write (no_token)")
    client.stopped = exc
    assert client.resume_after_unsent(exc) is False and client.stopped is exc
