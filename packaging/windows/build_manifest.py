"""Generate auditable version metadata embedded in a Windows bundle."""
from __future__ import annotations

import argparse
import json
import platform
import subprocess
from datetime import datetime, timezone
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]


def _git_sha() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _pyinstaller_version() -> str:
    try:
        import PyInstaller

        return str(PyInstaller.__version__)
    except Exception:
        return "unavailable"


def build_payload(version: str, built_at_utc: str | None = None) -> dict[str, object]:
    baseline = json.loads((SCRIPT_DIR / "baseline.json").read_text(encoding="utf-8"))
    return {
        "schema": 1,
        "product": "ACL Campaign Launcher",
        "version": version,
        "channel": "windows-packaging-spike",
        "core_baseline_sha": baseline["baseline_sha"],
        "installer_source_sha": _git_sha(),
        "built_at_utc": built_at_utc
        or datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "python_version": platform.python_version(),
        "pyinstaller_version": _pyinstaller_version(),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(build_payload(args.version), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote build manifest: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
