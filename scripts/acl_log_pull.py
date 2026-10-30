"""Owner only: download new batches of the shared action log into a local SQLite.

  python scripts/acl_log_pull.py            # fetch what is new
  python scripts/acl_log_pull.py --db X     # use another database file

Needs ACL_EVENT_LOG_URL (or the built-in address) and ACL_EVENT_LOG_READ_KEY.
Safe to repeat: a batch already downloaded is skipped, and an interrupted run
continues where it stopped. Never prints token values - only the ids of events
that look like they contain one.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Windows consoles are not UTF-8 by default; see scripts/_console.py.
from scripts._console import use_utf8  # noqa: E402

use_utf8()

try:
    from config.paths import get_local_config_dir, load_env

    load_env()
except Exception:
    get_local_config_dir = None  # type: ignore[assignment]

from src.acl_event_log import shipper  # noqa: E402
from src.acl_event_log.store import open_db, pull_new  # noqa: E402


def default_db_path() -> Path:
    return Path(get_local_config_dir()) / "event_log.sqlite"


def _get(url: str, key: str) -> bytes:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", type=Path, default=None, help="SQLite file (default: machine config dir)")
    args = parser.parse_args(argv)

    base = shipper.ingest_url()
    key = (os.environ.get("ACL_EVENT_LOG_READ_KEY") or "").strip()
    if not base or not key:
        print("The log address or the read key is not set.\n"
              "Set ACL_EVENT_LOG_URL and ACL_EVENT_LOG_READ_KEY (see deploy/event-log/README.md).")
        return 1

    def list_page(cursor):
        query = {"cursor": cursor} if cursor else {}
        data = json.loads(_get(f"{base}/v1/list?{urllib.parse.urlencode(query)}", key))
        return data["keys"], data.get("cursor")

    def fetch(obj_key):
        return _get(f"{base}/v1/object?{urllib.parse.urlencode({'key': obj_key})}", key)

    db_path = args.db or default_db_path()
    conn = open_db(db_path)
    try:
        total = pull_new(conn, list_page, fetch)
    except (urllib.error.URLError, OSError) as exc:
        print(f"Network error, the database is intact - run again to continue: {type(exc).__name__}")
        return 2
    finally:
        conn.close()
    print(f"Database: {db_path}")
    print(f"Batches: {total['segments']}   new events: {total['new']}   duplicates: {total['duplicates']}"
          f"   conflicts: {total['conflicts']}   unreadable: {total['errors']}")
    if total["secrets"]:
        print("WARNING: these events look like they contain a token (ids only):")
        for event_id in total["secrets"]:
            print("  " + event_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
