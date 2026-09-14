"""Дописать строку в журнал так, чтобы её не потеряли.

Почему модуль вообще нужен. Журналы ACL — `rpa_telemetry.jsonl`,
`turn_off_log.jsonl` и соседние — писались одинаково: `open(path, "a")` и одна
строка за раз, без синхронизации. Формат выбран правильно, и обоснование в
`run_telemetry` сформулировано дословно так: «append-only, never rewritten, so
concurrent runs and crashed runs can't lose or truncate history».

Для POSIX это верно: там `O_APPEND` атомарен, ядро само ставит запись в конец.
**На Windows — нет.** Библиотека C эмулирует дописывание как «перейти в конец,
затем записать», и два писателя, дошедшие до конца одновременно, пишут по
одному смещению. Один затирает другого.

Замер 15.09, двенадцать писателей (`tests/test_journal_keeps_every_line.py`
воспроизводит его):

    потоки одного процесса, как было   целых 590 из 720 — потеряно 130
    отдельные процессы, как было       целых 331 из 480 — потеряно 149

Потерянная запись не портится и не оставляет следа: читатель её не увидит и не
узнает, что она была. Для журнала открытий и закрытий (ACL-117) это прямо
подрывает смысл — наутро нельзя отличить «действия не было» от «запись
пропала».

Как чинится. Два замка, потому что случая два:

* **внутри процесса** — обычный `threading.Lock` на путь. Дешборд обслуживает
  запросы в нескольких потоках, и это самый частый случай;
* **между процессами** — замок операционной системы на отдельном файле
  `<путь>.lock`. Отдельном, а не на самом журнале: запирать файл, в который
  сам же дописываешь, значит спорить с собственным режимом открытия.

Отдельный файл-замок ещё и переживает смену формата журнала: он не участвует в
чтении и его можно удалить в любой момент.

Чего здесь намеренно нет — `fsync`. Он стоит миллисекунды на каждой записи, а
защищает от потери питания, а не от соперничества. Журнал прогонов пишется
десятки раз за кампанию, и такая цена не оправдана.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

__all__ = ["append_line", "JournalWriteError"]


class JournalWriteError(RuntimeError):
    """Запись в журнал не удалась.

    Отдельный тип, потому что прежде оба места записи глушили любую ошибку
    через `except Exception: pass`. Нехватка места, отсутствие прав и занятый
    файл выглядели одинаково — никак. Вызывающий волен и дальше не падать
    из-за журнала, но теперь это будет его решением, а не умолчанием.
    """


_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(path: Path) -> threading.Lock:
    key = str(path.resolve() if path.is_absolute() else path)
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = _locks[key] = threading.Lock()
        return lock


if os.name == "nt":
    import msvcrt

    def _grab(fh) -> None:
        # LK_LOCK ждёт освобождения, но сдаётся через десять попыток по
        # секунде. Для строки журнала этого с запасом: держат замок на время
        # одной записи, а не на время прогона.
        msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)

    def _release(fh) -> None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _grab(fh) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)

    def _release(fh) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def append_line(path: str | os.PathLike[str], line: str) -> None:
    """Дописать одну строку, не потеряв её при одновременной записи.

    `line` дополняется переводом строки, если его нет: журнал построчный, и
    строка без перевода склеится со следующей — та же потеря, только видимая.
    """
    p = Path(path)
    if not line.endswith("\n"):
        line += "\n"
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with _lock_for(p):
            with open(str(p) + ".lock", "a+b") as guard:
                guard.seek(0)
                _grab(guard)
                try:
                    with p.open("a", encoding="utf-8") as fh:
                        fh.write(line)
                finally:
                    _release(guard)
    except OSError as exc:
        raise JournalWriteError(f"{p}: {exc}") from exc
