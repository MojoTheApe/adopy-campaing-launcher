# -*- coding: utf-8 -*-
"""Тяжёлый ролик сжимается перед отправкой, а не идёт особой дорогой (acl-1189).

Замер 15.09 на живом кабинете. Ролик 247 МБ, 1080×1080, 11.9 Мбит/с. Передача
файла браузеру проходит нормально — тяжёлый путь через CDP отрабатывает, — но
дальше байты уходят в Meta **порциями по 2.8 МБ раз в минуту**: во время
всплеска канал выдаёт 0.54 МБ/с, между всплесками ноль. Это около полутора
часов на файл, а шаг загрузки ждёт девять минут в самый долгий раз. Он не
сдаётся рано — он не может дождаться.

Тот же ролик, пересжатый, — 24.4 МБ, те же 1080×1080 и те же 2:50. Такой вес
уходит обычным путём, подтверждённым 103 успешными загрузками в истории.

Почему это стоит делать в коде, а не просьбой к байеру: в папке креативов 4882
файла, медиана 0.8 МБ, тяжелее порога — 117 (2%), самый большой 1.5 ГБ. Два
процента, но каждый означает несобранную кампанию, и человек узнаёт об этом
через полчаса ожидания.

Три правила, без которых починка была бы хуже болезни:

* **Оригинал неприкосновенен.** Сжатое живёт в машинном кэше, рядом с
  настройками, а не в папке креативов оператора. Его файлы — его.
* **Сжатие не скрывается.** Оператор собирал кампанию из конкретного файла;
  если уехала копия, об этом говорит телеметрия и экран.
* **Кодируем один раз.** Три объявления кампании и шесть кампаний круга берут
  один и тот же файл. Ключ кэша — путь, время изменения и размер: изменился
  исходник — изменится и ключ.

Чего здесь нет: смены разрешения и длительности. Только битрейт — всё
остальное меняет то, что собрал оператор.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from config import paths

from src.fb_ads_rpa import job_cancel

#: Выше этого веса обычный путь загрузки перестаёт работать. То же число, что
#: и `meta_dom._PW_FILE_LIMIT_MB`, и не случайно: именно там кончается дорога,
#: которую Playwright проходит сам.
DEFAULT_LIMIT_MB = 45.0

#: Куда целимся. Не «чуть ниже порога»: запас нужен, потому что двухпроходное
#: кодирование попадает в размер с точностью процентов, а не байтов.
DEFAULT_TARGET_MB = 25.0

#: Звук трогаем в последнюю очередь — на нём не экономят, разница в мегабайтах
#: копеечная, а слышно сразу.
AUDIO_KBPS = 128

#: Ниже этого битрейта картинка разваливается заметнее, чем стоит экономия.
#: Лучше отправить файл потяжелее и дать обычному пути попробовать, чем молча
#: выдать кашу вместо ролика.
MIN_VIDEO_KBPS = 400

_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d\d):(\d\d(?:\.\d+)?)")


@dataclass
class Prepared:
    """Что в итоге отправлять и что об этом сказать человеку."""

    path: str
    original_mb: float
    sent_mb: float
    compressed: bool = False
    seconds: float = 0.0
    reason: str = ""

    @property
    def note(self) -> str:
        if not self.compressed:
            return self.reason or ""
        # Shown to the operator on the job card — English, like every other
        # dashboard message.
        timing = f" ({self.reason})" if self.reason else f" in {self.seconds:.0f}s"
        return (f"compressed before upload: {self.original_mb:.0f} MB → "
                f"{self.sent_mb:.1f} MB{timing}")


# The publishing worker owns this callback; concurrent jobs/CLI calls do not
# inherit another job's notices. Always reset it, including on cancellation.
_notice_sink: ContextVar[Callable[[str], None] | None] = ContextVar("creative_notice", default=None)


@contextmanager
def observe_preparation(callback: Callable[[str], None]):
    token = _notice_sink.set(callback)
    try:
        yield
    finally:
        _notice_sink.reset(token)


def report_notice(text: str) -> None:
    """Строка для человека в панели задания (ACL-1388: робот сам закрыл окно
    профиля). Тот же канал, что у сжатия креативов; вне задания — ничего."""
    callback = _notice_sink.get()
    if callback and text:
        callback(text)


def report_preparation(original_path: str, prepared: Prepared) -> None:
    callback = _notice_sink.get()
    if callback and prepared.note:
        callback(f"{Path(original_path).name}: {prepared.note}")


def size_mb(path: str | Path) -> float:
    try:
        return os.path.getsize(path) / 1048576
    except OSError:
        return 0.0


def ffmpeg_exe() -> str:
    """Путь к кодировщику. Пусто — значит сжимать нечем.

    Сначала тот, что приехал вместе с зависимостями: у байера системного
    ffmpeg не будет, а требовать его установки — это ровно та ручная возня,
    из-за которой установка у людей и ломается.
    """
    try:
        import imageio_ffmpeg

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and os.path.isfile(exe):
            return exe
    except Exception:
        pass
    return shutil.which("ffmpeg") or ""


def _run(args: list[str], timeout: float) -> tuple[int, str]:
    """Запустить ffmpeg и дождаться; Stop задания его обрывает (ACL-1614).

    Ждём опросом, а не одним вызовом: сжатие идёт минуты, и всё это время Stop
    должен быть слышен. На Stop завершаем СВОЙ дочерний процесс ffmpeg (не
    Multilogin) и бросаем `JobCancelled`. Пока ждём — heartbeat: сжатие не
    пишет строк телеметрии, и это не зависание (`step_stuck`).
    """
    proc = subprocess.Popen(
        args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
        errors="replace", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    deadline = time.monotonic() + timeout
    while True:
        try:
            out, err = proc.communicate(timeout=0.5)
            break
        except subprocess.TimeoutExpired:
            pass
        if job_cancel.requested():
            proc.kill()
            proc.communicate()
            job_cancel.check()
        if time.monotonic() >= deadline:
            proc.kill()
            proc.communicate()
            raise subprocess.TimeoutExpired(args, timeout)
        job_cancel.heartbeat()
    return proc.returncode, (err or "") + (out or "")


def duration_s(path: str, *, run=_run, exe: str = "") -> float:
    """Длительность ролика в секундах. Ноль — не смогли определить.

    Читаем из вывода самого ffmpeg: `imageio-ffmpeg` привозит только его,
    отдельного ffprobe там нет, а тащить второй бинарник ради одной строки
    незачем.
    """
    exe = exe or ffmpeg_exe()
    if not exe:
        return 0.0
    try:
        _, text = run([exe, "-hide_banner", "-i", path], 60)
    except Exception:
        return 0.0
    m = _DURATION_RE.search(text or "")
    if not m:
        return 0.0
    h, mnt, sec = m.groups()
    return int(h) * 3600 + int(mnt) * 60 + float(sec)


def video_bitrate_kbps(duration: float, target_mb: float,
                       audio_kbps: int = AUDIO_KBPS) -> int:
    """Какой битрейт даст нужный размер на этой длительности.

    Считается от длительности, а не берётся постоянным, и это важно: 25 МБ на
    2:50 — это 1.2 Мбит/с, а на ролике вдвое длиннее та же цифра дала бы вдвое
    худшую картинку. Ноль означает «так не выйдет» — см. `MIN_VIDEO_KBPS`.
    """
    if duration <= 0 or target_mb <= 0:
        return 0
    total_kbps = (target_mb * 1048576 * 8) / duration / 1000
    video = int(total_kbps - audio_kbps)
    return video if video >= MIN_VIDEO_KBPS else 0


def cache_dir() -> Path:
    return Path(paths.get_local_config_dir()) / "creative_cache"


def cache_path(src: str, target_mb: float) -> Path:
    """Имя производного файла — от исходника, его времени и размера.

    Поменяли ролик, оставив имя, — поменяется и ключ, и старая копия просто
    перестанет находиться. Это дешевле, чем сверять содержимое.
    """
    try:
        stat = os.stat(src)
        stamp = f"{stat.st_mtime_ns}:{stat.st_size}"
    except OSError:
        stamp = "?"
    # v2 excludes incomplete files left by the former direct-to-cache writer.
    # Always use an MP4 container for the H.264/AAC copy, including AVI/WebM inputs.
    key = hashlib.sha1(f"v2|{os.path.abspath(src)}|{stamp}|{target_mb}".encode("utf-8")).hexdigest()[:16]
    return cache_dir() / f"{Path(src).stem}.{key}.mp4"


def cached_copy_name(src: str, *, limit_mb: float = DEFAULT_LIMIT_MB,
                     target_mb: float = DEFAULT_TARGET_MB) -> str:
    """Имя сжатой копии `src` в кэше, если она уже есть; иначе пусто.

    ACL-1637: в Meta уезжает именно эта копия, и в медиатеке она подписана
    ЕЁ именем (`<имя>.<ключ>.mp4`), а не именем исходника. Объявление 2+ искало
    плитку по имени исходника и «не находило копии». Здесь ничего не сжимается
    и ffmpeg не нужен: только проверка, что копия лежит в кэше.
    """
    original = size_mb(src)
    if original <= limit_mb:
        return ""
    dst = cache_path(src, target_mb)
    if dst.is_file() and 0 < size_mb(dst) <= min(limit_mb, original):
        return dst.name
    return ""


def compress(src: str, dst: str, video_kbps: int, *, run=_run,
             exe: str = "", timeout: float = 3600) -> bool:
    """Два прохода x264 в заданный битрейт. Разрешение и длительность не трогаем.

    Два прохода, а не постоянное качество (CRF): нам нужен предсказуемый
    размер, а CRF даёт предсказуемое качество и непредсказуемый вес — то есть
    ровно не ту гарантию, которая здесь нужна.
    """
    exe = exe or ffmpeg_exe()
    if not exe:
        return False
    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        log = str(Path(tmp) / "pass")
        common = [exe, "-y", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", src,
                  "-c:v", "libx264", "-b:v", f"{video_kbps}k", "-preset", "medium",
                  "-passlogfile", log]
        try:
            code, _ = run(common + ["-pass", "1", "-an", "-f", "null", os.devnull], timeout)
            if code != 0:
                return False
            code, _ = run(
                common + ["-pass", "2", "-c:a", "aac", "-b:a", f"{AUDIO_KBPS}k",
                          "-movflags", "+faststart", dst],
                timeout,
            )
        except Exception:
            return False
    return code == 0 and os.path.isfile(dst) and os.path.getsize(dst) > 0


def ensure_uploadable(path: str, *, limit_mb: float = DEFAULT_LIMIT_MB,
                      target_mb: float = DEFAULT_TARGET_MB,
                      run=_run) -> Prepared:
    """Вернуть файл, который стоит отправлять, и рассказ о том, что с ним сделали.

    Отказ сжатия — не отказ шага: отдаём исходник и даём обычному пути
    попробовать. Он, скорее всего, не справится, но решать это его отказом
    честнее, чем нашим: у него есть диагностика, а у нас догадка.
    """
    original = size_mb(path)
    if original <= limit_mb:
        return Prepared(path=path, original_mb=original, sent_mb=original)

    exe = ffmpeg_exe()
    if not exe:
        return Prepared(path=path, original_mb=original, sent_mb=original,
                        reason="cannot compress: ffmpeg not found")

    dst = cache_path(path, target_mb)
    if dst.is_file() and 0 < size_mb(dst) <= min(limit_mb, original):
        got = size_mb(dst)
        return Prepared(path=str(dst), original_mb=original, sent_mb=got,
                        compressed=True, reason="from cache")

    seconds_long = duration_s(path, run=run, exe=exe)
    kbps = video_bitrate_kbps(seconds_long, target_mb)
    if not kbps:
        return Prepared(path=path, original_mb=original, sent_mb=original,
                        reason=f"compression would not help: it needs under {MIN_VIDEO_KBPS} kbps")

    started = time.monotonic()
    # Never expose a half-written file under the reusable cache name. A
    # separate directory per attempt also isolates concurrent encoders.
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="encode-", dir=dst.parent) as tmp:
            pending = Path(tmp) / "creative.mp4"
            ok = compress(path, str(pending), kbps, run=run, exe=exe)
            job_cancel.check()    # ACL-1614: Stop во время сжатия — дальше не идём
            got = size_mb(pending)
            if not ok or not (0 < got <= min(limit_mb, original)):
                raise ValueError("encoded copy is incomplete or too large")
            if cache_path(path, target_mb) != dst:
                raise ValueError("source changed during encoding")
            os.replace(pending, dst)
    except Exception:
        return Prepared(path=path, original_mb=original, sent_mb=original,
                        seconds=time.monotonic() - started,
                        reason="compression failed — sending the original")
    return Prepared(path=str(dst), original_mb=original, sent_mb=got,
                    compressed=True, seconds=time.monotonic() - started)
