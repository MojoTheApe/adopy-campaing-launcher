"""Локальная SQLite владельца: скачанные пачки журнала и отчёты по ним.

Только стандартная библиотека. Повторная загрузка той же пачки ничего не
меняет: `events.event_id` уникален, `segments.key` помнит скачанное. Тот же
`event_id` с другим содержимым считается конфликтом, остаётся первый.
"""
from __future__ import annotations

import gzip
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

_SCHEMA = """
CREATE TABLE IF NOT EXISTS segments (key TEXT PRIMARY KEY, fetched_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY, type TEXT, ts TEXT, machine_id TEXT, machine TEXT, manager TEXT,
    flow_version TEXT, channel TEXT, job_id TEXT, account TEXT, campaign TEXT,
    stream_id TEXT, seq INTEGER, segment_key TEXT, payload TEXT
);
CREATE INDEX IF NOT EXISTS events_job ON events(job_id);
CREATE INDEX IF NOT EXISTS events_account ON events(account);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS events_type ON events(type);
CREATE INDEX IF NOT EXISTS events_stream ON events(stream_id);
"""

_COLUMNS = ("event_id", "type", "ts", "machine_id", "machine", "manager", "flow_version", "channel",
            "job_id", "account", "campaign", "stream_id", "seq")

# Подозрение на токен в payload: сам токен потом не выводится, только event_id.
_SECRET_RE = re.compile(r"EAA[A-Za-z0-9]{20,}|access_token=", re.I)


def open_db(path: Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def _payload_text(event: dict) -> str:
    return json.dumps(event.get("payload") or {}, ensure_ascii=False, sort_keys=True)


def ingest_segment(conn: sqlite3.Connection, key: str, body: bytes) -> dict:
    """Разобрать одну пачку (gzip ndjson) и добавить новые события."""
    result: dict[str, Any] = {"new": 0, "duplicates": 0, "conflicts": 0, "secrets": []}
    if conn.execute("SELECT 1 FROM segments WHERE key=?", (key,)).fetchone():
        return result
    try:
        raw = gzip.decompress(body)
    except (OSError, EOFError) as exc:
        result["error"] = f"unreadable segment: {type(exc).__name__}"
        return result
    for line in raw.splitlines():
        try:
            event = json.loads(line)
            event_id = str(event["event_id"])
        except Exception:
            continue   # повреждённая строка пропускается, остальные читаются
        payload = _payload_text(event)
        values = [event.get(c) for c in _COLUMNS]
        values[0] = event_id
        cur = conn.execute(
            f"INSERT OR IGNORE INTO events ({', '.join(_COLUMNS)}, segment_key, payload) "
            f"VALUES ({', '.join('?' * len(_COLUMNS))}, ?, ?)", (*values, key, payload))
        if cur.rowcount:
            result["new"] += 1
            if _SECRET_RE.search(payload):
                result["secrets"].append(event_id)
            continue
        old = conn.execute("SELECT type, payload FROM events WHERE event_id=?", (event_id,)).fetchone()
        if old and old["payload"] == payload and old["type"] == event.get("type"):
            result["duplicates"] += 1
        else:
            result["conflicts"] += 1
    conn.execute("INSERT INTO segments (key, fetched_at) VALUES (?, ?)",
                 (key, datetime.now(timezone.utc).isoformat()))
    conn.commit()
    return result


def pull_new(conn: sqlite3.Connection, list_page: Callable[[str | None], tuple[list[str], str | None]],
             fetch: Callable[[str], bytes]) -> dict:
    """Скачать объекты, которых ещё нет. Сбой сети оставляет базу целой: пачка пишется целиком или нет."""
    total: dict[str, Any] = {"segments": 0, "new": 0, "duplicates": 0, "conflicts": 0, "secrets": [],
                             "errors": 0}
    known = {r["key"] for r in conn.execute("SELECT key FROM segments")}
    cursor: str | None = None
    while True:
        keys, cursor = list_page(cursor)
        for key in keys:
            if key in known:
                continue
            res = ingest_segment(conn, key, fetch(key))
            if res.get("error"):
                total["errors"] += 1
                continue
            total["segments"] += 1
            for name in ("new", "duplicates", "conflicts"):
                total[name] += res[name]
            total["secrets"].extend(res["secrets"])
        if not cursor:
            return total


def _rows(conn: sqlite3.Connection, sql: str, args: Iterable[Any] = ()) -> list[dict]:
    out = []
    for r in conn.execute(sql, tuple(args)):
        d = dict(r)
        if "payload" in d:
            d["payload"] = json.loads(d["payload"] or "{}")
        out.append(d)
    return out


def job_history(conn: sqlite3.Connection, job_id: str) -> list[dict]:
    """Все события одного задания по времени."""
    return _rows(conn, "SELECT * FROM events WHERE job_id=? ORDER BY ts, seq", (job_id,))


def runs(conn: sqlite3.Connection, *, days: int = 7, account: str | None = None,
         manager: str | None = None) -> list[dict]:
    """Итоги кампаний за период: по строке на `publish.result`, длительность — из события задания."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    sql = ("SELECT * FROM events WHERE ts >= ? AND job_id IS NOT NULL AND type IN "
           "('job.created','job.finished','publish.result','publish.stats')")
    args: list[Any] = [cutoff]
    if account:
        sql += " AND account=?"
        args.append(account)
    if manager:
        sql += " AND manager=?"
        args.append(manager)
    jobs: dict[str, dict] = {}
    for e in _rows(conn, sql + " ORDER BY ts, seq", args):
        j = jobs.setdefault(e["job_id"], {"results": [], "stats": {}, "created": None, "finished": None})
        if e["type"] == "publish.result":
            j["results"].append(e)
        elif e["type"] == "publish.stats":
            j["stats"][e["campaign"]] = e
        elif e["type"] == "job.finished":
            j["finished"] = e
        else:
            j["created"] = j["created"] or e
    out = []
    for job_id, j in jobs.items():
        fin = (j["finished"] or {}).get("payload", {})
        for e in j["results"] or [j["created"] or j["finished"]]:
            if e is None:
                continue
            is_result = e["type"] == "publish.result"
            p = e["payload"] if is_result else {}
            stats = j["stats"].get(e["campaign"], {}).get("payload", {})
            out.append({
                "ts": e["ts"], "job_id": job_id, "manager": e["manager"], "account": e["account"],
                "campaign": e["campaign"] if is_result else None,
                "publishable": p.get("publishable") if is_result else None,
                "success": p.get("success") if is_result else None,
                "error": p.get("error") if is_result else None,
                "status": fin.get("status"),
                "duration_s": stats.get("duration_s", fin.get("duration_s")),
                "stop_kind": stats.get("stop_kind") or None,
            })
    return sorted(out, key=lambda r: r["ts"], reverse=True)


def machines(conn: sqlite3.Connection) -> list[dict]:
    """Машины: версия, последний heartbeat, размер ящика, отброшенное, дыры по `seq`."""
    gaps: dict[str, int] = {}
    for r in conn.execute("SELECT machine_id, stream_id, MAX(seq) AS top, COUNT(DISTINCT seq) AS n "
                          "FROM events WHERE stream_id IS NOT NULL GROUP BY machine_id, stream_id"):
        gaps[r["machine_id"]] = gaps.get(r["machine_id"], 0) + max(0, (r["top"] or 0) - r["n"])
    out = []
    for m in conn.execute("SELECT machine_id, COUNT(*) AS events, MAX(ts) AS last_event_at "
                          "FROM events GROUP BY machine_id ORDER BY machine_id").fetchall():
        latest = conn.execute("SELECT machine, manager, flow_version FROM events WHERE machine_id=? "
                              "ORDER BY ts DESC, seq DESC LIMIT 1", (m["machine_id"],)).fetchone()
        beat = conn.execute("SELECT ts, payload FROM events WHERE machine_id=? AND type='heartbeat' "
                            "ORDER BY ts DESC, seq DESC LIMIT 1", (m["machine_id"],)).fetchone()
        bp = json.loads(beat["payload"]) if beat else {}
        out.append({
            "machine_id": m["machine_id"], "machine": latest["machine"], "manager": latest["manager"],
            "flow_version": latest["flow_version"], "events": m["events"],
            "last_event_at": m["last_event_at"], "last_heartbeat_at": beat["ts"] if beat else None,
            "outbox_bytes": bp.get("outbox_bytes"), "dropped": bp.get("dropped"),
            "gaps": gaps.get(m["machine_id"], 0),
        })
    return out
