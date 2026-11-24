"""Owner only: copy the local action log (event_log.sqlite) into the telemetry hub.

  python scripts/acl_hub_backfill.py                 # default database
  python scripts/acl_hub_backfill.py --db X --size 500

Needs ACL_HUB_URL and ACL_HUB_OWNER_KEY. Safe to repeat: the hub skips events it already has.
Never prints the key.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Iterator

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts._console import use_utf8  # noqa: E402

use_utf8()

try:
    from config.paths import get_local_config_dir, load_env

    load_env()
except Exception:
    get_local_config_dir = None  # type: ignore[assignment]

from src.acl_event_log import shipper  # noqa: E402

_COLUMNS = ("event_id", "stream_id", "seq", "type", "ts", "machine_id", "machine", "manager",
            "flow_version", "channel", "job_id", "account", "campaign")
#: Распакованная пачка не больше 4 МБ: хаб принимает до 16 МБ, а publish.result бывает по 45 КБ.
MAX_BYTES = 4 * 1024 * 1024

Post = Callable[[str, bytes, dict], "tuple[int, dict]"]


def iter_batches(db_path: Path, size: int = 500, max_bytes: int = MAX_BYTES) -> Iterator[tuple[str, bytes]]:
    """Пачки (batch_id, gzip ndjson) в форме событий клиента, по порядку rowid."""
    conn = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        lines: list[bytes] = []
        first = last = 0
        total = 0
        for row in conn.execute(f"SELECT rowid AS rid, {', '.join(_COLUMNS)}, payload FROM events ORDER BY rowid"):
            event = {c: row[c] for c in _COLUMNS}
            event["payload"] = json.loads(row["payload"] or "{}")
            line = json.dumps(event, ensure_ascii=False).encode("utf-8")
            if lines and (len(lines) >= size or total + len(line) + 1 > max_bytes):
                yield f"backfill-{first}-{last}", gzip.compress(b"\n".join(lines) + b"\n")
                lines, total = [], 0
            if not lines:
                first = row["rid"]
            lines.append(line)
            total += len(line) + 1
            last = row["rid"]
        if lines:
            yield f"backfill-{first}-{last}", gzip.compress(b"\n".join(lines) + b"\n")
    finally:
        conn.close()


def _http_post(url: str, body: bytes, headers: dict) -> tuple[int, dict]:
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={**headers, "User-Agent": shipper.USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except Exception:
            return exc.code, {}


def default_db_path() -> Path:
    return Path(get_local_config_dir()) / "event_log.sqlite"


def main(argv: list[str] | None = None, *, post: Post | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--size", type=int, default=500, help="events per batch")
    args = parser.parse_args(argv)
    base = (os.environ.get("ACL_HUB_URL") or "").strip().rstrip("/")
    key = (os.environ.get("ACL_HUB_OWNER_KEY") or "").strip()
    if not base or not key:
        print("Set ACL_HUB_URL and ACL_HUB_OWNER_KEY (see deploy/acl-hub/README.md).")
        return 1
    post = post or _http_post
    totals = {"batches": 0, "accepted": 0, "duplicates": 0, "rejected": 0}
    for batch_id, body in iter_batches(args.db or default_db_path(), size=max(1, args.size)):
        headers = {"Authorization": f"Bearer {key}", "Content-Encoding": "gzip",
                   "Content-Type": "application/x-ndjson", "X-ACL-Batch": batch_id}
        try:
            status, data = post(f"{base}/v1/backfill", body, headers)
        except (urllib.error.URLError, OSError) as exc:
            print(f"Network error at {batch_id}: {type(exc).__name__}. Run again to continue.")
            return 2
        if status != 200:
            print(f"Hub answered {status} at {batch_id}: {data.get('error', '')}. Run again to continue.")
            return 2
        totals["batches"] += 1
        for name in ("accepted", "duplicates", "rejected"):
            totals[name] += int(data.get(name) or 0)
    print("batches={batches} accepted={accepted} duplicates={duplicates} rejected={rejected}".format(**totals))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
