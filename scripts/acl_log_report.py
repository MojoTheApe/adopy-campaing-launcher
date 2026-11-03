"""Owner only: read the local copy of the shared action log.

  python scripts/acl_log_report.py --runs [--days 7] [--account X] [--manager X]
  python scripts/acl_log_report.py --job <job_id>
  python scripts/acl_log_report.py --machines
  python scripts/acl_log_report.py --failures [--days 7]
  python scripts/acl_log_report.py --events [--days 1] [--type publish.result | raw.] [--machine X] [--limit 200]

Run scripts/acl_log_pull.py first to bring the copy up to date.
"""
from __future__ import annotations

import argparse
import json
import sys
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

from src.acl_event_log.store import events, failures, job_history, machines, open_db, runs  # noqa: E402


def _cell(value) -> str:
    return "" if value is None else str(value)


def print_table(rows: list[dict], columns: list[str]) -> None:
    if not rows:
        print("(no rows)")
        return
    widths = [max(len(c), *(len(_cell(r.get(c))) for r in rows)) for c in columns]
    print("  ".join(c.ljust(w) for c, w in zip(columns, widths)))
    for r in rows:
        print("  ".join(_cell(r.get(c)).ljust(w) for c, w in zip(columns, widths)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", type=Path, default=None, help="SQLite file (default: machine config dir)")
    what = parser.add_mutually_exclusive_group(required=True)
    what.add_argument("--runs", action="store_true", help="campaign results over the period")
    what.add_argument("--job", metavar="JOB_ID", help="full history of one job")
    what.add_argument("--machines", action="store_true", help="machines, versions, gaps")
    what.add_argument("--failures", action="store_true", help="failures grouped by fingerprint")
    what.add_argument("--events", action="store_true", help="latest raw events with filters")
    parser.add_argument("--days", type=int, default=None, help="period (default: 7, --events: 1)")
    parser.add_argument("--account")
    parser.add_argument("--manager")
    parser.add_argument("--type", help="--events: event type, or a prefix ending with a dot")
    parser.add_argument("--machine", help="--events: machine id or name")
    parser.add_argument("--limit", type=int, default=200, help="--events: max rows")
    args = parser.parse_args(argv)
    days = args.days if args.days is not None else (1 if args.events else 7)

    db_path = args.db or Path(get_local_config_dir()) / "event_log.sqlite"
    conn = open_db(db_path)
    try:
        if args.runs:
            print_table(runs(conn, days=days, account=args.account, manager=args.manager),
                        ["ts", "manager", "account", "campaign", "publishable", "status", "duration_s",
                         "stop_kind", "error"])
        elif args.job:
            for e in job_history(conn, args.job):
                print(f"{e['ts']}  {e['type']:<16} {json.dumps(e['payload'], ensure_ascii=False)[:300]}")
        elif args.failures:
            print_table(failures(conn, days=days), ["count", "machines", "versions", "accounts", "first", "last",
                                                    "stop_kind", "after", "fail_fp", "text"])
        elif args.events:
            for e in events(conn, days=days, type=args.type, machine=args.machine, limit=args.limit):
                print(f"{e['ts']}  {_cell(e['machine']):<12} {e['type']:<18} {_cell(e['account']):<10} "
                      f"{json.dumps(e['payload'], ensure_ascii=False)[:240]}")
        else:
            print_table(machines(conn), ["machine", "manager", "flow_version", "last_heartbeat_at",
                                         "outbox_bytes", "dropped", "gaps", "events", "ram_gb", "cores",
                                         "scale_pct", "on_battery"])
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
