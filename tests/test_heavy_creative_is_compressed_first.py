# -*- coding: utf-8 -*-
"""Тяжёлый ролик сжимается перед отправкой, и об этом говорят вслух (acl-1189).

Замер 15.09 на живом кабинете: ролик 247 МБ уходит в Meta порциями по 2.8 МБ
раз в минуту — около полутора часов, при бюджете шага в девять минут. Тот же
ролик в 24 МБ едет обычной дорогой за минуту.

Кодировщика в этих проверках нет: запускать ffmpeg в наборе значило бы минуты
на прогон и зависимость от машины. Вместо него подставляется двойник — поэтому
проверяется то, что действительно можно сломать правкой: решение сжимать,
арифметика битрейта, ключ кэша и честность отказов.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import creative_compress as cc  # noqa: E402


def _file(tmp_path: Path, name: str, mb: float) -> str:
    p = tmp_path / name
    p.write_bytes(b"\0" * int(mb * 1048576))
    return str(p)


@pytest.fixture
def cache(tmp_path, monkeypatch):
    d = tmp_path / "cache"
    monkeypatch.setattr(cc, "cache_dir", lambda: d)
    monkeypatch.setattr(cc, "ffmpeg_exe", lambda: "ffmpeg.exe")
    return d


# ── арифметика ───────────────────────────────────────────────────────────

def test_the_bitrate_follows_the_length_not_a_constant():
    """25 МБ на 2:50 — это 1.2 Мбит/с; на ролике вдвое длиннее та же цифра
    дала бы вдвое худшую картинку, поэтому битрейт считается от длительности."""
    short = cc.video_bitrate_kbps(170.8, 25.0)
    long = cc.video_bitrate_kbps(341.6, 25.0)
    assert short > long
    assert 1000 < short < 1200, short


def test_a_target_that_would_ruin_the_picture_is_refused():
    """Час видео в 25 МБ — это каша. Лучше отдать исходник и дать обычному
    пути отказать со своей диагностикой, чем молча отправить мусор."""
    assert cc.video_bitrate_kbps(3600, 25.0) == 0


def test_nonsense_input_does_not_produce_a_bitrate():
    assert cc.video_bitrate_kbps(0, 25.0) == 0
    assert cc.video_bitrate_kbps(170, 0) == 0


# ── решение сжимать ──────────────────────────────────────────────────────

def test_a_light_file_is_left_alone(tmp_path, cache):
    src = _file(tmp_path, "small.mp4", 3)
    out = cc.ensure_uploadable(src, run=lambda *a, **k: (_ for _ in ()).throw(AssertionError("не звать")))
    assert out.path == src
    assert out.compressed is False


def test_a_heavy_file_is_compressed_and_the_original_survives(tmp_path, cache, monkeypatch):
    src = _file(tmp_path, "heavy.mp4", 120)
    monkeypatch.setattr(cc, "duration_s", lambda *a, **k: 170.8)

    def fake_compress(s, dst, kbps, **kw):
        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        Path(dst).write_bytes(b"\0" * int(24 * 1048576))
        return True

    monkeypatch.setattr(cc, "compress", fake_compress)
    out = cc.ensure_uploadable(src)

    assert out.compressed is True
    assert out.path != src, "отправлять должны копию"
    assert os.path.getsize(src) == int(120 * 1048576), "оригинал обязан остаться нетронутым"
    assert 23 < out.sent_mb < 25
    assert "MB" in out.note and "→" in out.note


def test_the_second_ad_takes_the_cached_copy(tmp_path, cache, monkeypatch):
    """Три объявления кампании и шесть кампаний круга берут один файл.
    Кодировать его заново на каждое — это минуты, которых у нас нет."""
    src = _file(tmp_path, "heavy.mp4", 120)
    monkeypatch.setattr(cc, "duration_s", lambda *a, **k: 170.8)
    calls: list[str] = []

    def fake_compress(s, dst, kbps, **kw):
        calls.append(dst)
        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        Path(dst).write_bytes(b"\0" * int(24 * 1048576))
        return True

    monkeypatch.setattr(cc, "compress", fake_compress)
    first = cc.ensure_uploadable(src)
    second = cc.ensure_uploadable(src)

    assert len(calls) == 1, "второй раз кодировать нечего"
    assert second.path == first.path
    assert second.compressed is True


def test_a_changed_source_gets_a_new_key(tmp_path, cache):
    """Ролик подменили, имя оставили — старая копия не должна всплыть."""
    src = _file(tmp_path, "heavy.mp4", 120)
    before = cc.cache_path(src, 25.0)
    os.utime(src, (0, 0))
    after = cc.cache_path(src, 25.0)
    assert before != after


# ── честность отказов ────────────────────────────────────────────────────

def test_without_an_encoder_the_original_goes_as_it_is(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "ffmpeg_exe", lambda: "")
    src = _file(tmp_path, "heavy.mp4", 120)
    out = cc.ensure_uploadable(src)
    assert out.path == src
    assert out.compressed is False
    assert "ffmpeg" in out.reason


def test_a_failed_encode_is_not_a_failed_step(tmp_path, cache, monkeypatch):
    """Отказ сжатия отдаёт исходник: у обычного пути есть диагностика, у нас
    только догадка — пусть отказывает он."""
    src = _file(tmp_path, "heavy.mp4", 120)
    monkeypatch.setattr(cc, "duration_s", lambda *a, **k: 170.8)
    monkeypatch.setattr(cc, "compress", lambda *a, **k: False)
    out = cc.ensure_uploadable(src)
    assert out.path == src
    assert out.compressed is False
    assert out.reason


def test_an_unreadable_duration_does_not_compress_blindly(tmp_path, cache, monkeypatch):
    """Не знаем длительность — не знаем битрейт. Гадать нельзя."""
    src = _file(tmp_path, "heavy.mp4", 120)
    monkeypatch.setattr(cc, "duration_s", lambda *a, **k: 0.0)
    monkeypatch.setattr(cc, "compress", lambda *a, **k: (_ for _ in ()).throw(AssertionError("не звать")))
    out = cc.ensure_uploadable(src)
    assert out.compressed is False


def test_the_duration_is_read_from_ffmpeg_output():
    text = "  Duration: 00:02:50.81, start: 0.000000, bitrate: 12138 kb/s"
    got = cc.duration_s("x.mp4", run=lambda *a, **k: (1, text), exe="ffmpeg.exe")
    assert abs(got - 170.81) < 0.01


def test_compression_keeps_resolution_and_length(tmp_path, cache, monkeypatch):
    """Менять разрешение или длительность значило бы менять то, что собрал
    оператор. Трогаем только битрейт — проверяем это по строке запуска."""
    src = _file(tmp_path, "heavy.mp4", 120)
    seen: dict = {}

    def fake_run(args, timeout):
        seen["args"] = args
        return 0, ""

    monkeypatch.setattr(cc, "ffmpeg_exe", lambda: "ffmpeg.exe")
    cc.compress(src, str(tmp_path / "out.mp4"), 1050, run=fake_run)
    args = " ".join(seen.get("args") or [])
    assert "-b:v 1050k" in args
    for forbidden in ("-s ", "-vf scale", "-t ", "-ss "):
        assert forbidden not in args, f"кодирование не должно менять {forbidden!r}"
