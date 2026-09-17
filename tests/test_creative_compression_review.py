"""Cache failure/retry, real bundled encoding and publish notice regressions."""
import hashlib
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.fb_ads_rpa import creative_compress as cc
from src.fb_ads_rpa.draft import RpaPublishResult
from src.web import publish_jobs as pj


@pytest.fixture
def source(tmp_path, monkeypatch):
    src = tmp_path / 'heavy.mp4'
    with src.open('wb') as f:
        f.truncate(46 * 1048576)
    monkeypatch.setattr(cc, 'cache_dir', lambda: tmp_path / 'cache')
    monkeypatch.setattr(cc, 'ffmpeg_exe', lambda: 'synthetic-encoder')
    monkeypatch.setattr(cc, 'duration_s', lambda *a, **k: 170.8)
    return str(src)


@pytest.mark.parametrize('failure', ['return', 'timeout', 'oversize', 'source_changed'])
def test_failed_output_never_becomes_a_reusable_cache(source, monkeypatch, failure):
    calls = []
    final = cc.cache_path(source, 25.0)
    def encode(src, dst, *a, **kw):
        calls.append(dst)
        with open(dst, 'wb') as f:
            f.write(b'partial')
            if failure == 'oversize':
                f.truncate(46 * 1048576)
        assert not final.exists(), 'final name must not expose pending output'
        if failure == 'timeout':
            raise subprocess.TimeoutExpired('encoder', 1)
        if failure == 'source_changed':
            with open(src, 'ab') as f:
                f.write(b'changed')
        return failure != 'return'
    monkeypatch.setattr(cc, 'compress', encode)
    for _ in range(2):
        result = cc.ensure_uploadable(source)
        assert result.path == source and not result.compressed
    assert len(calls) == 2
    assert not final.exists()
    assert list(cc.cache_dir().iterdir()) == []


def test_legacy_partial_cache_is_not_reused(source, monkeypatch):
    stat = Path(source).stat()
    key = hashlib.sha1(f'{Path(source).absolute()}|{stat.st_mtime_ns}:{stat.st_size}|25.0'.encode()).hexdigest()[:16]
    old = cc.cache_dir() / f'heavy.{key}.mp4'
    old.parent.mkdir()
    old.write_bytes(b'partial')
    monkeypatch.setattr(cc, 'compress', lambda *a, **k: False)
    assert not cc.ensure_uploadable(source).compressed


@pytest.mark.parametrize('audio', [True, False])
def test_bundled_encoder_preserves_geometry_duration_and_source(tmp_path, monkeypatch, audio):
    import imageio_ffmpeg
    exe = imageio_ffmpeg.get_ffmpeg_exe()
    src = tmp_path / 'synthetic.avi'
    args = [exe, '-y', '-f', 'lavfi', '-i', 'testsrc2=size=160x96:rate=15:duration=2']
    if audio:
        args += ['-f', 'lavfi', '-i', 'sine=frequency=440:duration=2', '-c:a', 'pcm_s16le']
    args += ['-c:v', 'rawvideo', str(src)]
    code, log = cc._run(args, 30)
    assert code == 0, log
    before = hashlib.sha256(src.read_bytes()).digest()
    monkeypatch.setattr(cc, 'cache_dir', lambda: tmp_path / 'cache')
    result = cc.ensure_uploadable(str(src), limit_mb=.5, target_mb=.2)
    assert result.compressed, result
    assert 0 < result.sent_mb < .5
    assert Path(result.path).suffix == '.mp4'
    assert hashlib.sha256(src.read_bytes()).digest() == before
    _, metadata = cc._run([exe, '-hide_banner', '-i', result.path], 30)
    assert '160x96' in metadata
    assert abs(cc.duration_s(result.path) - 2) < .12
    assert ('Audio:' in metadata) == audio
    assert cc._run([exe, '-v', 'error', '-i', result.path, '-f', 'null', '-'], 30)[0] == 0
    monkeypatch.setattr(cc, 'compress', lambda *a, **k: pytest.fail('cache should be reused'))
    assert cc.ensure_uploadable(str(src), limit_mb=.5, target_mb=.2).path == result.path


@pytest.mark.parametrize('fail', [True, False])
def test_real_job_runner_reports_notices_during_run_without_leaking(monkeypatch, fail):
    monkeypatch.setattr(pj, 'use_rpa_publish', lambda: True)
    monkeypatch.setattr(pj, '_write_draft_status', lambda *a, **k: None)
    job = pj.create_job('Offline', 'Test', [{}])
    other = pj.create_job('Other', 'Test', [{}])
    prepared = cc.Prepared('/cache/copy.mp4', 247, 24, True, 2)
    def publish(draft):
        cc.report_preparation('/private/source.mp4', prepared)
        snap = pj.get_job(job)
        assert 'source.mp4' in snap['creative_notices'][0]
        assert '/private' not in snap['creative_notices'][0]
        snap['creative_notices'].clear()
        assert pj.get_job(job)['creative_notices']
        assert pj.get_job(other)['creative_notices'] == []
        if fail:
            raise RuntimeError('synthetic failure')
        return RpaPublishResult(campaign_name='Demo', success=True)
    publisher = SimpleNamespace(build_drafts=lambda *a, **k: [SimpleNamespace(errors=[])],
                                publish_draft=publish, close=lambda **k: None)
    monkeypatch.setattr(pj, 'RpaPublisher', lambda **k: publisher)
    try:
        pj._run_rpa(job, [{}], account_label='Offline', country='Test', activate=False)
        before = pj.get_job(job)['creative_notices']
        cc.report_preparation('later.mp4', prepared)
        assert pj.get_job(job)['creative_notices'] == before
        assert pj.get_job(job)['status'] == 'done'
        assert bool(pj.get_job(job)['errors']) == fail
    finally:
        with pj._jobs_lock:
            pj._jobs.pop(job)
            pj._jobs.pop(other)
