"""Generate auditable version metadata embedded in a Windows bundle."""
from __future__ import annotations

import argparse
import json
import platform
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]
VERSION_PATTERN = re.compile(r"[0-9A-Za-z][0-9A-Za-z._-]{0,63}\Z")


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
    if not VERSION_PATTERN.fullmatch(version):
        raise ValueError("version must contain only letters, digits, dot, dash, and underscore")
    baseline = json.loads((SCRIPT_DIR / "baseline.json").read_text(encoding="utf-8"))
    source_sha = _git_sha()
    return {
        "schema": 1,
        "product": "Adopy Campaign Launcher",
        "version": version,
        "channel": "windows-preview",
        "core_baseline_sha": source_sha if baseline.get("runtime_policy") == "integrated" else baseline["baseline_sha"],
        "integration_base_sha": baseline["baseline_sha"],
        "runtime_source_sha": source_sha,
        "installer_source_sha": source_sha,
        "built_at_utc": built_at_utc
        or datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "python_version": platform.python_version(),
        "pyinstaller_version": _pyinstaller_version(),
    }


def build_current_payload(build: dict[str, object]) -> dict[str, object]:
    version = str(build["version"])
    return {
        "schema": 1,
        "version": version,
        "source_sha": build["installer_source_sha"],
        "core_baseline_sha": build["core_baseline_sha"],
        "relative_path": f"versions\\{version}",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--current-output", type=Path)
    args = parser.parse_args()

    payload = build_payload(args.version)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote build manifest: {args.output}")
    if args.current_output:
        args.current_output.parent.mkdir(parents=True, exist_ok=True)
        args.current_output.write_text(
            json.dumps(build_current_payload(payload), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"Wrote current version pointer: {args.current_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
