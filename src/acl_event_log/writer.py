"""Запись событий в локальный ящик: один процесс — один файл-поток.

Никаких межпроцессных блокировок: каждый процесс пишет только в свой файл
`<stream_id>.ndjson`, а `seq` — счётчик в памяти под `threading.Lock`.
Перезапуск процесса — новый поток. `emit()` не бросает исключений и не ходит в
сеть: сбой журнала не должен ни задержать, ни уронить публикацию.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.acl_event_log.clean import clean_value

SCHEMA_VERSION = 1
# Предел неотправленного: выше мягкого отбрасываются `raw.*`, выше жёсткого — всё.
SOFT_CAP_BYTES = 200 * 1024 ** 2
HARD_CAP_BYTES = 400 * 1024 ** 2
_SIZE_REFRESH_S = 60

_lock = threading.Lock()
_stream: dict[str, Any] = {"id": None, "seq": 0, "fh": None, "dir": None}
_override: Path | None = None
_forced = False  # тест включил журнал сам
_dropped = 0
_ident: dict[str, Any] | None = None
_size: dict[str, float] = {"at": 0.0, "bytes": 0}


def enabled() -> bool:
    if (os.environ.get("ACL_EVENT_LOG") or "").strip() == "0":
        return False
    # Тесты не пишут в настоящий ящик, пока сами не включили журнал фикстурой.
    if os.environ.get("PYTEST_CURRENT_TEST") and not _forced:
        return False
    return True


def outbox_dir() -> Path:
    if _override is not None:
        return _override
    from config.paths import get_local_config_dir
    return Path(get_local_config_dir()) / "event_outbox"


def _reset_stream() -> None:
    fh = _stream.get("fh")
    if fh is not None:
        try:
            fh.close()
        except Exception:
            pass
    _stream.update(id=None, seq=0, fh=None, dir=None)
    _size.update(at=0.0, bytes=0)


def use_outbox_for_tests(path: Path | None) -> None:
    """Включить журнал в тесте и направить его в `path`; `None` возвращает как было."""
    global _override, _forced, _dropped
    with _lock:
        _reset_stream()
        _override = Path(path) if path is not None else None
        _forced = path is not None
        _dropped = 0


def stream_state() -> dict:
    with _lock:
        return {_stream["id"]: _stream["seq"]} if _stream["id"] else {}


def take_dropped() -> int:
    global _dropped
    with _lock:
        n, _dropped = _dropped, 0
        return n


def _identity() -> dict:
    """Идентичность машины читается один раз на процесс, каждое поле в своём try."""
    global _ident
    if _ident is not None:
        return _ident
    ident: dict[str, Any] = {"machine_id": "", "machine": "", "manager": "", "flow_version": ""}
    try:
        from src.fb_ads_rpa import telemetry_sync
        label, mid = telemetry_sync.machine_identity()
        ident["machine"], ident["machine_id"] = label, mid
    except Exception:
        pass
    try:
        from src.acl_sentry import manager_label
        ident["manager"] = manager_label()
    except Exception:
        pass
    try:
        from src.fb_ads_rpa import telemetry_sync
        ident["flow_version"] = telemetry_sync.flow_version()
    except Exception:
        pass
    _ident = ident
    return ident


def _outbox_bytes(directory: Path) -> int:
    """Грубая оценка неотправленного: размеры файлов минус подтверждённые смещения."""
    total = 0
    for f in directory.glob("*.ndjson"):
        try:
            size = f.stat().st_size
            acked = 0
            ack = f.with_suffix(".acked")
            if ack.is_file():
                acked = int(json.loads(ack.read_text("utf-8")).get("offset", 0))
            total += max(0, size - acked)
        except Exception:
            continue
    return total


def _pending_bytes(directory: Path) -> int:
    now = time.monotonic()
    if now - _size["at"] > _SIZE_REFRESH_S:
        _size["bytes"] = _outbox_bytes(directory)
        _size["at"] = now
    return int(_size["bytes"])


def _tz_minutes() -> int:
    off = datetime.now().astimezone().utcoffset()
    return int(off.total_seconds() // 60) if off else 0


def emit(event_type: str, payload: dict | None = None, *, job_id=None, account=None, campaign=None) -> None:
    global _dropped
    try:
        if not enabled():
            return
        if job_id is None:
            try:
                from src.fb_ads_rpa import run_telemetry
                job_id = run_telemetry.current_job_id() or None
            except Exception:
                job_id = None
        body = clean_value(payload or {})
        ident = _identity()
        ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        with _lock:
            directory = outbox_dir()
            pending = _pending_bytes(directory) if directory.is_dir() else 0
            if pending >= HARD_CAP_BYTES or (pending >= SOFT_CAP_BYTES and event_type.startswith("raw.")):
                _dropped += 1
                return
            if _stream["fh"] is None or _stream["dir"] != str(directory):
                directory.mkdir(parents=True, exist_ok=True)
                _reset_stream()
                _stream["id"] = uuid.uuid4().hex
                _stream["dir"] = str(directory)
                _stream["fh"] = open(directory / f"{_stream['id']}.ndjson", "ab")
            seq = _stream["seq"] + 1
            line = {
                "event_id": f"{_stream['id']}:{seq}", "stream_id": _stream["id"], "seq": seq,
                "schema_version": SCHEMA_VERSION, "type": event_type, "ts": ts,
                "tz_machine": _tz_minutes(), **ident,
                "channel": (os.environ.get("ACL_EVENT_LOG_CHANNEL") or "prod").strip() or "prod",
                "job_id": job_id, "account": account, "campaign": campaign, "payload": body,
            }
            data = (json.dumps(line, ensure_ascii=False, default=str) + "\n").encode("utf-8")
            _stream["fh"].write(data)
            _stream["fh"].flush()
            _stream["seq"] = seq
            _size["bytes"] += len(data)
    except Exception:
        pass
