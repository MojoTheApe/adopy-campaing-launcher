"""Windows entry point for the frozen ACL pilot bundle.

This wrapper deliberately lives outside the pilot runtime. It gives the
packaged application two diagnostics and moves relative runtime output away
from the read-only installation directory before handing control to serve.py.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _bundle_root() -> Path:
    """Return PyInstaller's data directory, or this file's directory in dev."""
    frozen_root = getattr(sys, "_MEIPASS", None)
    return Path(frozen_root) if frozen_root else Path(__file__).resolve().parent


def _manifest_path() -> Path:
    explicit = os.environ.get("ACL_BUILD_MANIFEST", "").strip()
    return Path(explicit) if explicit else _bundle_root() / "acl-build-manifest.json"


def _print_version() -> int:
    path = _manifest_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"ACL version metadata is unavailable: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print(f"ACL {payload.get('version', 'unknown')}")
    print(f"pilot core: {payload.get('core_baseline_sha', 'unknown')}")
    print(f"installer source: {payload.get('installer_source_sha', 'unknown')}")
    return 0


def _runtime_workdir() -> Path:
    """Return a per-user writable directory for relative logs and output."""
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    base = Path(local_app_data) if local_app_data else Path.home() / ".local" / "share"
    workdir = base / "ACL" / "workspace"
    workdir.mkdir(parents=True, exist_ok=True)
    return workdir


def main() -> int:
    if "--acl-version" in sys.argv[1:]:
        return _print_version()

    if "--smoke-check" in sys.argv[1:]:
        from scripts.acl_smoke import main as smoke_main

        return smoke_main()

    # Import first because serve.py locates bundled resources relative to its
    # own file and briefly switches there. Then restore a writable CWD so all
    # legacy relative output paths land in per-user storage.
    from serve import main as serve_main

    runtime_workdir = _runtime_workdir()
    os.chdir(runtime_workdir)
    os.environ.setdefault("ACL_RUNTIME_DIR", str(runtime_workdir))
    serve_main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
