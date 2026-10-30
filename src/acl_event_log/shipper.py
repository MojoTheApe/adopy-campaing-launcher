"""Фоновая отправка ящика в Cloudflare Worker.

Отправка идёт только из потока `acl-event-log-shipper`: задание её не ждёт.
Подтверждение (`<stream_id>.acked`) пишется только после ответа `200`, поэтому
сбой сети, падение процесса или повтор пачки ничего не теряют; дубли снимает
Worker (ключ объекта = номер пачки) и скрипт владельца (`event_id`).
Только стандартная библиотека. Ошибки отправки менеджеру не показываются.
"""
from __future__ import annotations

import gzip
import json
import os
import random
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from src.acl_event_log import writer

#: Адрес Worker. Пусто, пока владелец не развернул Worker; можно задать `ACL_EVENT_LOG_URL`.
DEFAULT_URL = ""

BATCH_MAX_BYTES = 512 * 1024
CYCLE_S = 900
JITTER = 0.2
FIRST_DELAY_S = (60, 300)
MAX_BACKOFF_S = 3600
FOREIGN_TTL_S = 86400
LOCK_TTL_S = 3600
#: Недописанная строка чужого потока считается потерянной, если файл не менялся так долго.
PARTIAL_LOST_S = 600

Post = Callable[[str, bytes, dict], int]

_thread: threading.Thread | None = None
_wake = threading.Event()
_start_lock = threading.Lock()


def ingest_url() -> str:
    return ((os.environ.get("ACL_EVENT_LOG_URL") or "").strip() or DEFAULT_URL).rstrip("/")


def library_token() -> str:
    return (os.environ.get("ACL_LIBRARY_TOKEN") or "").strip()


def configured() -> bool:
    return bool(ingest_url() and library_token())


def _http_post(url: str, body: bytes, headers: dict) -> int:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return int(resp.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)


def _read_acked(path: Path) -> dict:
    try:
        data = json.loads(path.read_text("utf-8"))
        return {"offset": int(data.get("offset", 0)), "last_seq": int(data.get("last_seq", 0))}
    except Exception:
        return {"offset": 0, "last_seq": 0}


def _write_acked(path: Path, offset: int, last_seq: int) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"offset": offset, "last_seq": last_seq}), encoding="utf-8")
    os.replace(tmp, path)


def _take_lock(directory: Path) -> Path | None:
    lock = directory / ".shipper.lock"
    try:
        if lock.exists():
            if time.time() - lock.stat().st_mtime < LOCK_TTL_S:
                return None
            lock.unlink()   # забытый замок упавшего процесса
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return lock
    except OSError:
        return None


def _last_ship_at(directory: Path) -> str:
    try:
        return (directory / ".last_ship").read_text("utf-8").strip()
    except Exception:
        return ""


def status() -> dict:
    """Для `acl_doctor`: настроена ли отправка, размер ящика, когда уходило в последний раз."""
    directory = writer.outbox_dir()
    size = 0
    try:
        if directory.is_dir():
            size = writer._outbox_bytes(directory)
    except Exception:
        pass
    return {"configured": configured(), "outbox_bytes": size, "last_ship_at": _last_ship_at(directory)}


def _machine_id() -> str:
    mid = str(writer._identity().get("machine_id") or "")
    return mid if mid and all(c.isalnum() or c in "_-" for c in mid) and len(mid) <= 64 else "unknown"


def _split_lines(data: bytes, offset: int) -> tuple[list[tuple[bytes, int, int]], bool]:
    """Полные строки от `offset`: (строка, смещение после неё, seq). Второе — есть ли недописанный хвост."""
    out: list[tuple[bytes, int, int]] = []
    pos = 0
    while True:
        nl = data.find(b"\n", pos)
        if nl < 0:
            break
        line = data[pos:nl + 1]
        pos = nl + 1
        try:
            seq = int(json.loads(line)["seq"])
        except Exception:
            seq = 0   # битая строка уйдёт как есть: разбор на стороне владельца пропустит её
        out.append((line, offset + pos, seq))
    return out, pos < len(data)


def _send_stream(path: Path, own: bool, post: Post, url: str, headers: dict, result: dict) -> str:
    """Отправить неподтверждённое одного файла. Вернуть "" или причину остановки цикла."""
    acked_path = path.with_suffix(".acked")
    acked = _read_acked(acked_path)
    with path.open("rb") as fh:
        fh.seek(acked["offset"])
        data = fh.read()
    lines, partial = _split_lines(data, acked["offset"])
    give_up_at = None
    if partial and not own:
        try:
            stale = time.time() - path.stat().st_mtime > PARTIAL_LOST_S
        except OSError:
            stale = False
        if stale:   # процесс упал посреди строки: хвост потерян, но после целых строк
            give_up_at = acked["offset"] + len(data)
    stream_id = path.stem
    limit = None
    i = 0
    while i < len(lines):
        batch, size = [], 0
        for item in lines[i:]:
            if batch and (size + len(item[0]) > BATCH_MAX_BYTES or (limit and len(batch) >= limit)):
                break
            batch.append(item)
            size += len(item[0])
        seqs = [s for _, _, s in batch if s]
        h = {**headers,
             "X-ACL-Batch": f"{stream_id}-{seqs[0] if seqs else 0}-{seqs[-1] if seqs else 0}",
             "X-ACL-Count": str(len(batch))}
        code = post(url, gzip.compress(b"".join(b for b, _, _ in batch)), h)
        if code == 200:
            _write_acked(acked_path, batch[-1][1], max([acked["last_seq"], *seqs]))
            acked["last_seq"] = max([acked["last_seq"], *seqs])
            result["sent"] += len(batch)
            result["batches"] += 1
            i += len(batch)
        elif code in (401, 403):
            return "unauthorized"
        elif code == 413:
            if len(batch) > 1:
                limit = max(1, len(batch) // 2)
            else:
                _write_acked(acked_path, batch[0][1], acked["last_seq"])   # одна строка не проходит: пропустить
                i += 1
        else:
            return "retry"
    if give_up_at is not None:
        _write_acked(acked_path, give_up_at, acked["last_seq"])
    return ""


def _clean_foreign(directory: Path, own_ids: set[str]) -> None:
    for f in directory.glob("*.ndjson"):
        if f.stem in own_ids:
            continue
        try:
            acked = _read_acked(f.with_suffix(".acked"))
            if acked["offset"] >= f.stat().st_size and time.time() - f.stat().st_mtime > FOREIGN_TTL_S:
                f.unlink()
                f.with_suffix(".acked").unlink(missing_ok=True)
        except OSError:
            continue


def ship_once(*, post: Post | None = None) -> dict:
    """Один цикл отправки. `post(url, body, headers) -> int` подменяется в тестах."""
    result = {"sent": 0, "batches": 0, "status": "ok"}
    if not configured():
        result["status"] = "not_configured"
        return result
    directory = writer.outbox_dir()
    lock = _take_lock(directory) if directory.is_dir() else None
    if lock is None:
        result["status"] = "busy"
        return result
    try:
        post = post or _http_post
        writer.emit("heartbeat", {
            "streams": writer.stream_state(), "outbox_bytes": writer._outbox_bytes(directory),
            "dropped": writer.take_dropped(), "last_ship_at": _last_ship_at(directory),
        })
        url = ingest_url() + "/v1/batch"
        headers = {"Authorization": f"Bearer {library_token()}", "Content-Encoding": "gzip",
                   "Content-Type": "application/x-ndjson", "X-ACL-Machine": _machine_id()}
        own_ids = set(writer.stream_state())
        for path in sorted(directory.glob("*.ndjson")):
            stop = _send_stream(path, path.stem in own_ids, post, url, headers, result)
            if stop:
                result["status"] = stop
                break
        if result["batches"]:
            (directory / ".last_ship").write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")
        if result["status"] == "ok":
            _clean_foreign(directory, own_ids)
    except Exception:
        result["status"] = "retry"
    finally:
        try:
            lock.unlink()
        except OSError:
            pass
    return result


def _delay(failures: int) -> float:
    base = min(MAX_BACKOFF_S, CYCLE_S * (2 ** failures)) if failures else CYCLE_S
    return base * random.uniform(1 - JITTER, 1 + JITTER)


def _loop() -> None:
    _wake.wait(random.uniform(*FIRST_DELAY_S))
    failures = 0
    while True:
        _wake.clear()
        try:
            status_ = ship_once()["status"]
        except Exception:
            status_ = "retry"
        failures = failures + 1 if status_ == "retry" else 0
        _wake.wait(_delay(failures))


def start_shipper() -> None:
    """Запустить фоновый поток один раз за процесс; без журнала или под pytest — нет."""
    global _thread
    if not writer.enabled():
        return
    with _start_lock:
        if _thread is not None and _thread.is_alive():
            return
        _thread = threading.Thread(target=_loop, name="acl-event-log-shipper", daemon=True)
        _thread.start()


def wake_shipper() -> None:
    """Разбудить поток после окончания задания; сама отправка идёт в потоке."""
    _wake.set()
