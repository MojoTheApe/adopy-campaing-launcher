# -*- coding: utf-8 -*-
"""Сжатие тяжёлого видео перед загрузкой через Graph API в профиле (Issue #1000).

18.10 на Doc121 ролик 247 МБ ушёл 31 порцией по 8 МБ: тяжёлая вкладка Ads
Manager не справилась — опросы прогресса отвалились по таймауту, загрузка
закончилась «result unknown», токен сессии в странице потерялся. Самое быстрое
лекарство — не слать столько: ролик заранее пересжимается в H.264 + AAC.

Правила:

* Порог ~60 МБ (`ACL_VIDEO_COMPRESS_ABOVE_MB`). Всё, что легче, едет как есть.
* Разрешение и пропорции не трогаем (Meta хочет 1080+ по короткой стороне —
  оригинал, если он крупнее, остаётся крупным). Меняется только битрейт.
* Битрейт считается от настоящей длительности под ~45 МБ, чтобы уложиться в 50.
* Результат кэшируется по sha256 оригинала: повтор публикации не кодирует заново.
* Нет ffmpeg — поведение прежнее, оператору честное предупреждение.
* Пока идёт кодирование, раз в ~10 с пишем шаг: точка на дешборде не оранжевеет.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

#: Выше этого веса видео сжимается.
DEFAULT_THRESHOLD_MB = 60.0
#: Куда целимся: запас до 50 МБ на неточность одного прохода.
DEFAULT_TARGET_MB = 45.0
#: Как часто писать шаг, пока кодировщик работает.
TICK_S = 10.0
#: Звук оставляем нормальным: разница в мегабайтах копеечная.
AUDIO_KBPS = 128
#: Ниже этого картинка разваливается — лучше отправить оригинал.
MIN_VIDEO_KBPS = 400
ENV_THRESHOLD = "ACL_VIDEO_COMPRESS_ABOVE_MB"

MB = 1024 * 1024

#: Запуск кодировщика: (args, tick) -> код возврата. `tick()` зовётся ~раз в TICK_S.
Runner = Callable[[list[str], Callable[[], None]], int]


def threshold_mb() -> float:
    try:
        value = float(os.environ.get(ENV_THRESHOLD, ""))
    except ValueError:
        return DEFAULT_THRESHOLD_MB
    return value if value > 0 else DEFAULT_THRESHOLD_MB


def step_text(index: int, original_mb: float, target_mb: float, elapsed_s: float | None = None) -> str:
    """Строка шага для оператора (английский, как все сообщения дешборда)."""
    text = f"ad {index}: compressing video {original_mb:.0f} MB → {target_mb:.0f} MB"
    return text if elapsed_s is None else f"{text} ({elapsed_s:.0f} s)"


def done_text(index: int, original_mb: float, got_mb: float, *, cached: bool = False) -> str:
    return (f"ad {index}: video compressed {original_mb:.0f} MB → {got_mb:.1f} MB"
            + (" (from cache)" if cached else ""))


def no_ffmpeg_text(index: int, original_mb: float) -> str:
    return (f"ad {index}: video is {original_mb:.0f} MB and ffmpeg was not found, so it cannot be "
            "compressed; the upload may hang the Ads Manager tab")


def _ffmpeg() -> str:
    from src.fb_ads_rpa import creative_compress
    return creative_compress.ffmpeg_exe()


def _duration(path: str, exe: str) -> float:
    from src.fb_ads_rpa import creative_compress
    return creative_compress.duration_s(path, exe=exe)


def _cache_dir() -> Path:
    from config import paths
    return Path(paths.get_local_config_dir()) / "video_cache"


def default_runner(args: list[str], tick: Callable[[], None]) -> int:
    """Запустить ffmpeg и ждать опросом; на каждом шаге опроса — `tick`."""
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        while True:
            try:
                return proc.wait(timeout=TICK_S)
            except subprocess.TimeoutExpired:
                tick()
    except BaseException:
        proc.kill()
        raise


@dataclass
class Compressed:
    data: bytes
    original_mb: float
    sent_mb: float
    cached: bool = False


def maybe_compress(path: str, data: bytes, index: int, step: Callable[[str], None], *,
                   warn: Callable[[str], None] | None = None,
                   threshold: float | None = None, target_mb: float = DEFAULT_TARGET_MB,
                   runner: Runner = default_runner,
                   ffmpeg: Callable[[], str] = _ffmpeg,
                   duration: Callable[[str, str], float] = _duration,
                   cache_dir: Callable[[], Path] = _cache_dir,
                   clock: Callable[[], float] = time.monotonic) -> bytes:
    """Вернуть байты для загрузки: сжатые, если ролик тяжелее порога и это удалось.

    Любой сбой сжатия — не отказ публикации: отдаём оригинал, как было раньше.
    """
    limit = threshold_mb() if threshold is None else threshold
    original_mb = len(data) / MB
    if original_mb <= limit:
        return data
    warn = warn or step

    exe = ffmpeg()
    if not exe:
        warn(no_ffmpeg_text(index, original_mb))
        return data

    try:
        digest = _sha256(path, data)
        folder = cache_dir()
        dst = folder / f"{digest}.mp4"
        if dst.is_file() and 0 < dst.stat().st_size < len(data):
            got = dst.read_bytes()
            step(done_text(index, original_mb, len(got) / MB, cached=True))
            return got

        seconds = duration(path, exe)
        kbps = _video_kbps(seconds, target_mb)
        if not kbps:
            warn(f"ad {index}: video is {original_mb:.0f} MB but cannot be compressed to "
                 f"{target_mb:.0f} MB without ruining the picture; sending the original")
            return data

        folder.mkdir(parents=True, exist_ok=True)
        started = clock()
        step(step_text(index, original_mb, target_mb))
        with tempfile.TemporaryDirectory(prefix="encode-", dir=folder) as tmp:
            pending = Path(tmp) / "video.mp4"
            args = [exe, "-y", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", path,
                    "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                    "-b:v", f"{kbps}k", "-maxrate", f"{int(kbps * 1.25)}k",
                    "-bufsize", f"{kbps * 2}k", "-c:a", "aac", "-b:a", f"{AUDIO_KBPS}k",
                    "-movflags", "+faststart", str(pending)]
            code = runner(args, lambda: step(step_text(index, original_mb, target_mb, clock() - started)))
            if code != 0 or not pending.is_file() or not 0 < pending.stat().st_size < len(data):
                raise ValueError("encoded copy is missing or not smaller")
            os.replace(pending, dst)
        got = dst.read_bytes()
    except Exception as exc:
        warn(f"ad {index}: video compression failed ({type(exc).__name__}); sending the original")
        return data
    step(done_text(index, original_mb, len(got) / MB))
    return got


def _sha256(path: str, data) -> str:
    """sha256 оригинала. Тяжёлый файл может прийти не байтами (объект с `len()`,
    например `LargeFile` в сборке с чанковой загрузкой) — тогда читаем файл с диска блоками."""
    if isinstance(data, (bytes, bytearray, memoryview)):
        return hashlib.sha256(data).hexdigest()
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(MB), b""):
            digest.update(block)
    return digest.hexdigest()


def _video_kbps(duration_s: float, target_mb: float) -> int:
    """Битрейт видео, который при этой длительности даёт `target_mb`; 0 — так не выйдет."""
    if duration_s <= 0 or target_mb <= 0:
        return 0
    total = target_mb * MB * 8 / duration_s / 1000
    video = int(total - AUDIO_KBPS)
    return video if video >= MIN_VIDEO_KBPS else 0
