#!/usr/bin/env python3
"""Launch the morning-report web UI on localhost.

    python serve.py                 # http://127.0.0.1:9090 + opens browser
    python serve.py --port 8765
    python serve.py --no-browser      # terminal only, no auto-open

Reads FB_STATS_EXCEL + GSheet config from .env, same as run_morning.py.
"""
from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

_root = Path(__file__).resolve().parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))
os.chdir(_root)

# ACL-1680: every launch path (exe, start_dashboard.bat, a Claude-started shell)
# must use the same real %LOCALAPPDATA%\ACL, not the MSIX package copy. Pin it
# before anything reads settings; also merges a package copy and logs the folder.
try:
    from config.config_unify import prepare_config_dir

    prepare_config_dir()
except Exception as _exc:  # a convenience step must never stop the dashboard
    print(f"[serve] settings folder check skipped: {_exc}", file=sys.stderr)

try:
    # Machine-local settings first, checkout .env second — see
    # config.paths.get_env_files. Keeps a buyer's configuration outside the
    # folder that updates overwrite.
    from config.paths import load_env
    load_env()
except Exception:
    pass

# The engine a fresh install runs. recorded_flow is the one every verified run
# used, including the 43/43 run that produced live spend (tag
# rpa-v96-spend-confirmed-2026-08-24).
#
# It used to default to the recipe player WITH a development stop gate at
# s12_conversion_location — a gate from the days when the goal was to reach
# Conversion location cleanly three times. On this machine the gate was
# invisible because .env sets FB_RPA_ENGINE=recorded_flow, but .env.example
# ships that line empty: a buyer's fresh install would have stopped after
# conversion location, building no audience, ads, creatives, copy or
# destination, while the operator believed they had asked for a full campaign.
_engine = (os.environ.get("FB_RPA_ENGINE") or "").strip().lower() or "recorded_flow"
os.environ["FB_RPA_ENGINE"] = _engine

_backend = (os.environ.get("FB_PUBLISH_BACKEND") or "rpa").strip().lower()
if _backend != "rpa":
    raise SystemExit(
        "[serve] Refusing to start: FB_PUBLISH_BACKEND must be 'rpa'. "
        "The legacy live Meta API backend is disabled."
    )

# Experimental engines remain available to developers from the terminal, but
# a campaign-manager launcher must not silently switch ten machines away from
# the only end-to-end flow with production evidence.
_allow_experimental = (os.environ.get("ACL_ALLOW_EXPERIMENTAL_RPA") or "").strip().lower()
if _engine != "recorded_flow" and _allow_experimental not in {"1", "true", "yes"}:
    raise SystemExit(
        f"[serve] Refusing to start unsupported RPA engine '{_engine}'. "
        "Set FB_RPA_ENGINE=recorded_flow."
    )

# The stop gate belongs to the recipe player and applies only when someone
# deliberately selects that engine. It must never narrow the default path.
if _engine != "recorded_flow":
    os.environ.setdefault("FB_RPA_RECIPE_PLAYER", "1")
    os.environ.setdefault("FB_RPA_RECIPE_STOP_AFTER", "s12_conversion_location")

from src.fb_ads_rpa.config import ensure_playwright_browsers_path

ensure_playwright_browsers_path()

# The version string says both what the flow calls itself and which commit is
# actually running — see telemetry_sync.flow_version. With ACL on ten machines,
# "v96" alone cannot tell two installs apart.
from src.fb_ads_rpa.telemetry_sync import flow_version

print(f"[serve] RPA flow version: {flow_version()}")
# Say which engine will actually run and whether anything truncates it. The old
# line printed "Recipe player: 1" even when the recorded flow was in charge,
# which is how a stop gate stayed invisible for weeks.
_stop = (os.environ.get("FB_RPA_RECIPE_STOP_AFTER") or "").strip()
print(f"[serve] RPA engine: {_engine or 'recipe_player (default)'}")
if _stop and _engine != "recorded_flow":
    print(f"[serve] WARNING: partial run — recipe stops after '{_stop}'")

import logging

import uvicorn

_DEFAULT_PORT = 9090


def _configure_logging() -> None:
    """Send ACL's own step log to this window.

    Every step the RPA takes is recorded with log.info(...) — which profile it
    opened, which field it filled, what it is waiting for. Those lines went
    nowhere: nothing in this process ever configured logging, so Python's
    last-resort handler printed WARNING and above and dropped the rest. A run
    started from the launcher was therefore silent, and the window showed
    nothing but the browser polling for job status.

    Found on 2026-09-03, watching a stalled run on another machine with no
    record of what it had been doing — the console had the answer and was
    throwing it away.

    Third-party loggers stay at WARNING: playwright and urllib3 at INFO bury
    the one line that matters.
    """
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")
    )
    root = logging.getLogger()
    if not any(isinstance(h, logging.StreamHandler) for h in root.handlers):
        root.addHandler(handler)
    root.setLevel(logging.WARNING)
    logging.getLogger("src").setLevel(logging.INFO)


def _pick_port(host: str, preferred: int, *, scan: int = 20) -> int:
    """Return the first bindable port in [preferred .. preferred+scan)."""
    for port in range(preferred, preferred + scan):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((host, port))
            except OSError:
                continue
            return port
    raise SystemExit(
        f"[serve] No free port found near {preferred}. "
        f"Try: python serve.py --port {preferred + scan}"
    )


def _schedule_browser_open(url: str, delay_s: float = 2.0) -> None:
    def _open() -> None:
        time.sleep(delay_s)
        webbrowser.open(url)

    threading.Thread(target=_open, daemon=True).start()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the fb_agents morning-report web UI on localhost.",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind address (default: 127.0.0.1, single-user)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=_DEFAULT_PORT,
        help=f"Preferred port (default: {_DEFAULT_PORT}; auto-picks next free)",
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="Auto-reload on code change (dev; can fail on Windows if port busy)",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not open the browser automatically",
    )
    args = parser.parse_args()
    if args.reload:
        os.environ.setdefault("ACL_AUTO_REFRESH", "0")  # dev reload never starts scheduled profiles by default

    _configure_logging()

    port = _pick_port(args.host, args.port)
    url = f"http://{args.host}:{port}/"

    print(f"[serve] Dashboard URL: {url}", file=sys.stderr)
    print("[serve] Press Ctrl+C to stop.", file=sys.stderr)

    if not args.no_browser:
        _schedule_browser_open(url)

    # ACL-1800: команды над кампаниями отвечают только на loopback — адрес привязки
    # запоминаем для приложения (процесс --reload наследует окружение).
    os.environ["ACL_BIND_HOST"] = args.host
    uvicorn.run(
        "src.web.app:app",
        host=args.host,
        port=port,
        reload=args.reload,
        log_level="info",
    )


if __name__ == "__main__":
    main()
