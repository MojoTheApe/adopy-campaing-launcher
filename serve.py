#!/usr/bin/env python3
"""Launch the morning-report web UI on localhost.

    python serve.py            # default 127.0.0.1:8000
    python serve.py --port 9000

Reads FB_STATS_EXCEL + GSheet config from .env, same as run_morning.py.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_root = Path(__file__).resolve().parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))
os.chdir(_root)

try:
    from dotenv import load_dotenv
    load_dotenv(_root / ".env")
except ImportError:
    pass

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the fb_agents morning-report web UI on localhost."
    )
    parser.add_argument("--host", default="127.0.0.1",
                        help="Bind address (default: 127.0.0.1, single-user)")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true",
                        help="Auto-reload on code change (dev)")
    args = parser.parse_args()

    print(f"[serve] http://{args.host}:{args.port}/", file=sys.stderr)
    uvicorn.run(
        "src.web.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )


if __name__ == "__main__":
    main()
