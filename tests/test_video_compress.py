"""Issue #1000: сжатие тяжёлого видео перед загрузкой через Graph API в профиле.

ffmpeg не запускается: кодирование — поддельный runner, который пишет «сжатый» файл.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_api import video_compress as vc  # noqa: E402

MB = 1024 * 1024
BIG = b"x" * (70 * MB)
SMALL = b"y" * (10 * MB)


class Env:
    def __init__(self, tmp_path, *, exe="ffmpeg", seconds=120.0, out_size=30 * MB, code=0, ticks=0):
        self.cache = tmp_path / "cache"
        self.exe, self.seconds, self.out_size, self.code, self.ticks = exe, seconds, out_size, code, ticks
        self.runs: list[list[str]] = []
        self.steps: list[str] = []
        self.warns: list[str] = []

    def runner(self, args, tick):
        self.runs.append(args)
        for _ in range(self.ticks):
            tick()
        if self.code == 0:
            Path(args[-1]).write_bytes(b"z" * self.out_size)
        return self.code

    def go(self, data=BIG, **kw):
        return vc.maybe_compress("clip.mp4", data, 3, self.steps.append, warn=self.warns.append,
                                 runner=self.runner, ffmpeg=lambda: self.exe,
                                 duration=lambda p, e: self.seconds, cache_dir=lambda: self.cache, **kw)


def test_small_file_is_untouched(tmp_path):
    env = Env(tmp_path)
    assert env.go(SMALL) is SMALL
    assert not env.runs and not env.steps


def test_threshold_boundary_and_env(tmp_path, monkeypatch):
    env = Env(tmp_path)
    data = b"a" * (60 * MB)
    assert env.go(data) is data                      # ровно порог — не сжимаем
    assert env.go(data + b"a") != data + b"a"        # чуть выше — сжимаем
    monkeypatch.setenv(vc.ENV_THRESHOLD, "100")
    env2 = Env(tmp_path / "e2")
    assert env2.go(BIG) is BIG
    monkeypatch.setenv(vc.ENV_THRESHOLD, "junk")
    assert vc.threshold_mb() == vc.DEFAULT_THRESHOLD_MB


def test_compresses_with_expected_ffmpeg_args_and_steps(tmp_path):
    env = Env(tmp_path, ticks=2)
    got = env.go()
    assert len(got) == 30 * MB
    args = env.runs[0]
    assert args[args.index("-c:v") + 1] == "libx264" and args[args.index("-c:a") + 1] == "aac"
    assert "+faststart" in args and "-vf" not in args and "-s" not in args   # разрешение не трогаем
    kbps = int(args[args.index("-b:v") + 1][:-1])
    assert kbps * 1000 * 120 / 8 / MB <= 50
    assert env.steps[0] == "ad 3: compressing video 70 MB → 45 MB"
    assert env.steps[1].startswith("ad 3: compressing video 70 MB → 45 MB (")
    assert env.steps[-1] == "ad 3: video compressed 70 MB → 30.0 MB"
    assert not env.warns


def test_cache_by_sha256_avoids_second_encode(tmp_path):
    env = Env(tmp_path)
    first = env.go()
    assert (env.cache / f"{hashlib.sha256(BIG).hexdigest()}.mp4").is_file()
    second = env.go()
    assert second == first and len(env.runs) == 1
    assert env.steps[-1].endswith("(from cache)")


def test_no_ffmpeg_keeps_original_and_warns(tmp_path):
    env = Env(tmp_path, exe="")
    assert env.go() is BIG
    assert env.warns == ["ad 3: video is 70 MB and ffmpeg was not found, so it cannot be compressed; "
                         "the upload may hang the Ads Manager tab"]
    assert not env.runs


def test_encoder_failure_and_bigger_output_fall_back(tmp_path):
    assert Env(tmp_path / "a", code=1).go() is BIG
    env = Env(tmp_path / "b", out_size=80 * MB)
    assert env.go() is BIG and "compression failed" in env.warns[0]
    assert not list((env.cache).glob("*.mp4"))        # битый результат в кэш не попадает


def test_too_long_for_target_sends_original(tmp_path):
    env = Env(tmp_path, seconds=100000.0)
    assert env.go() is BIG and not env.runs and "cannot be compressed" in env.warns[0]


def test_unknown_duration_sends_original(tmp_path):
    env = Env(tmp_path, seconds=0.0)
    assert env.go() is BIG and not env.runs


def test_step_text_format():
    assert vc.step_text(2, 247.4, 45) == "ad 2: compressing video 247 MB → 45 MB"
    assert vc.step_text(2, 247.4, 45, 20.2) == "ad 2: compressing video 247 MB → 45 MB (20 s)"


def test_creator_compresses_before_the_size_limit(tmp_path):
    from tests.test_acl1761_api_create import FakeMeta, _draft, _run
    from src.fb_api import creator as cr

    calls = []

    def fake(path, data, index, step, *, warn):
        calls.append((Path(path).name, index))
        step(f"ad {index}: compressing video 1 MB → 1 MB")
        return b"small"

    meta = FakeMeta()
    out, _ = _run(meta, _draft(tmp_path, video=True, ads=3), compress_video=fake)
    assert calls == [("1002_clip.mp4", 3)]
    assert any("compressing video" in s for s in out.steps)
    assert cr.VIDEO_MAX_BYTES < 247 * MB                # без сжатия такой ролик отклонялся бы лимитом
