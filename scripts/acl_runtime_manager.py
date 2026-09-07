"""Manage immutable per-release Python runtimes and safe update recovery.

The bootstrap ``.venv`` is never modified by automatic updates.  Each promoted
commit receives its own runtime under the machine-local ACL directory.  Code
is activated only after dependency installation and smoke checks, while a
pending marker lets the next launcher invocation recover an update that never
reached a healthy server.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from config.paths import get_local_config_dir  # noqa: E402
from scripts._console import use_utf8  # noqa: E402

use_utf8()

_SHA = re.compile(r"^[0-9a-fA-F]{40}$")
_RUNTIME_METADATA = "runtime.json"
_QUARANTINE = "quarantined.json"
_PENDING = "update_pending.json"


def _sha(value: str) -> str:
    value = (value or "").strip().lower()
    if not _SHA.fullmatch(value):
        raise ValueError("release SHA must contain exactly 40 hexadecimal characters")
    return value


def _config_dir() -> Path:
    return Path(get_local_config_dir()).resolve()


def runtime_dir(sha: str) -> Path:
    return _config_dir() / "runtimes" / _sha(sha)


def runtime_python(sha: str) -> Path:
    # ACL's supported fleet launcher is Windows. Keeping this explicit also
    # prevents a staged POSIX test runtime from being mistaken for production.
    return runtime_dir(sha) / "Scripts" / "python.exe"


def pending_path() -> Path:
    return _config_dir() / _PENDING


def _atomic_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Короткое служебное имя, а не `.{имя}.{32 hex}.tmp`: у Windows предел
    # пути 260 символов, и каталог рантайма — `.{sha40}.staging-{uuid32}` —
    # съедает 82 из них. Длинное имя упирало запись в FileNotFoundError, и
    # ровно на этом сборка Windows была красной. Уникальности восьми
    # шестнадцатеричных знаков внутри одного каталога довольно.
    temp = path.with_name(f".{uuid.uuid4().hex[:8]}.tmp")
    try:
        with open(temp, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _read_json(path: Path) -> dict[str, object] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else None
    except (OSError, ValueError):
        return None


def _requirements_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _runtime_metadata(sha: str) -> dict[str, object] | None:
    return _read_json(runtime_dir(sha) / _RUNTIME_METADATA)


def is_active(sha: str) -> bool:
    sha = _sha(sha)
    metadata = _runtime_metadata(sha)
    return bool(
        metadata
        and metadata.get("sha") == sha
        and metadata.get("state") == "active"
        and runtime_python(sha).is_file()
        and not (runtime_dir(sha) / _QUARANTINE).exists()
    )


def is_quarantined(sha: str) -> bool:
    return (runtime_dir(_sha(sha)) / _QUARANTINE).is_file()


def prepare_runtime(sha: str, requirements: str | Path) -> Path:
    """Create a dependency runtime without touching the working one."""
    sha = _sha(sha)
    requirements = Path(requirements).resolve()
    if not requirements.is_file():
        raise RuntimeError(f"requirements file is missing: {requirements}")
    target = runtime_dir(sha)
    python = runtime_python(sha)
    digest = _requirements_digest(requirements)
    metadata = _runtime_metadata(sha)

    if is_quarantined(sha):
        raise RuntimeError(f"release {sha[:12]} is quarantined on this machine")
    if target.exists():
        if (
            metadata
            and metadata.get("sha") == sha
            and metadata.get("requirements_sha256") == digest
            and metadata.get("state") in {"prepared", "active"}
            and python.is_file()
        ):
            return python
        raise RuntimeError(
            f"runtime directory exists but is incomplete or mismatched: {target}"
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.parent / f".{sha}.staging-{uuid.uuid4().hex}"
    try:
        base_python = str(getattr(sys, "_base_executable", None) or sys.executable)
        subprocess.run(
            [base_python, "-m", "venv", str(staging)],
            cwd=str(_ROOT),
            check=True,
            timeout=180,
        )
        staged_python = staging / "Scripts" / "python.exe"
        if not staged_python.is_file():
            raise RuntimeError("new Windows runtime did not create Scripts/python.exe")
        subprocess.run(
            [
                str(staged_python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "-q",
                "-r",
                str(requirements),
            ],
            cwd=str(_ROOT),
            check=True,
            timeout=1200,
        )
        _atomic_json(
            staging / _RUNTIME_METADATA,
            {
                "schema": 1,
                "sha": sha,
                "state": "prepared",
                "requirements_sha256": digest,
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        os.replace(staging, target)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return runtime_python(sha)


def activate_runtime(sha: str) -> Path:
    sha = _sha(sha)
    target = runtime_dir(sha)
    metadata = _runtime_metadata(sha)
    if (
        not metadata
        or metadata.get("sha") != sha
        or metadata.get("state") not in {"prepared", "active"}
        or not runtime_python(sha).is_file()
        or is_quarantined(sha)
    ):
        raise RuntimeError(f"runtime {sha[:12]} is not prepared for activation")
    metadata["state"] = "active"
    metadata["activated_at"] = datetime.now(timezone.utc).isoformat()
    _atomic_json(target / _RUNTIME_METADATA, metadata)
    return runtime_python(sha)


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )


def _git_out(*args: str) -> str:
    result = _git(*args)
    return result.stdout.strip() if result.returncode == 0 else ""


def _head() -> str:
    return _git_out("rev-parse", "HEAD").lower()


def _tracked_clean() -> bool:
    return not _git_out("status", "--porcelain", "--untracked-files=no")


def _is_ancestor(previous: str, candidate: str) -> bool:
    return _git("merge-base", "--is-ancestor", previous, candidate).returncode == 0


def mark_pending(previous: str, candidate: str) -> None:
    previous, candidate = _sha(previous), _sha(candidate)
    if previous == candidate:
        raise RuntimeError("pending update requires two different commits")
    if _head() != previous:
        raise RuntimeError("installed HEAD changed before pending update was recorded")
    if not _tracked_clean():
        raise RuntimeError("tracked local changes prevent a recoverable update")
    if not _is_ancestor(previous, candidate):
        raise RuntimeError("candidate is not a fast-forward from the installed release")
    # git cat-file -e intentionally has no output when successful, so its
    # return code is the only trustworthy existence check.
    if _git("cat-file", "-e", f"{candidate}^{{commit}}").returncode != 0:
        raise RuntimeError("candidate commit is unavailable")
    _atomic_json(
        pending_path(),
        {
            "schema": 1,
            "previous": previous,
            "candidate": candidate,
            "created_at": datetime.now(timezone.utc).isoformat(),
        },
    )


def _load_pending() -> tuple[str, str] | None:
    path = pending_path()
    if not path.exists():
        return None
    payload = _read_json(path)
    if not payload:
        raise RuntimeError(f"pending update marker is unreadable: {path}")
    try:
        return _sha(str(payload["previous"])), _sha(str(payload["candidate"]))
    except (KeyError, ValueError) as exc:
        raise RuntimeError(f"pending update marker is invalid: {path}") from exc


def _clear_pending() -> None:
    pending_path().unlink(missing_ok=True)


def _quarantine(sha: str, reason: str) -> None:
    target = runtime_dir(sha)
    target.mkdir(parents=True, exist_ok=True)
    _atomic_json(
        target / _QUARANTINE,
        {
            "schema": 1,
            "sha": _sha(sha),
            "reason": reason,
            "quarantined_at": datetime.now(timezone.utc).isoformat(),
        },
    )


def recover_pending() -> tuple[bool, str]:
    """Recover an unconfirmed update. Return (did_reset_code, message)."""
    pending = _load_pending()
    if not pending:
        return False, "no pending update"
    previous, candidate = pending
    current = _head()
    if current == previous:
        _clear_pending()
        return False, "pending marker cleared before code activation"
    if current != candidate:
        raise RuntimeError(
            "installed HEAD matches neither side of the pending update; manual review required"
        )
    if not _tracked_clean():
        raise RuntimeError("tracked local changes prevent safe release rollback")
    if not _is_ancestor(previous, candidate):
        raise RuntimeError("pending rollback ancestry is invalid")
    reset = _git("reset", "--keep", previous)
    if reset.returncode != 0 or _head() != previous:
        raise RuntimeError("git reset --keep could not restore the previous release")
    _quarantine(candidate, "candidate never reached confirmed ACL health")
    _clear_pending()
    return True, f"restored previous release {previous[:12]}; candidate quarantined"


def confirm_running() -> str:
    pending = _load_pending()
    if not pending:
        return "no pending update"
    _previous, candidate = pending
    if _head() != candidate:
        raise RuntimeError("cannot confirm a release that is not the installed HEAD")
    if not is_active(candidate):
        raise RuntimeError("cannot confirm a release without its active versioned runtime")
    _clear_pending()
    return f"confirmed healthy release {candidate[:12]}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)

    check = commands.add_parser("check-active")
    check.add_argument("--sha", required=True)

    prepare = commands.add_parser("prepare")
    prepare.add_argument("--sha", required=True)
    prepare.add_argument("--requirements", required=True)

    activate = commands.add_parser("activate")
    activate.add_argument("--sha", required=True)

    pending = commands.add_parser("mark-pending")
    pending.add_argument("--previous", required=True)
    pending.add_argument("--candidate", required=True)

    commands.add_parser("recover-pending")
    commands.add_parser("confirm-running")

    args = parser.parse_args(argv)
    try:
        if args.command == "check-active":
            return 0 if is_active(args.sha) else 1
        if args.command == "prepare":
            print(f"runtime prepared: {prepare_runtime(args.sha, args.requirements)}")
            return 0
        if args.command == "activate":
            print(f"runtime active: {activate_runtime(args.sha)}")
            return 0
        if args.command == "mark-pending":
            mark_pending(args.previous, args.candidate)
            print(f"pending update recorded: {args.previous[:12]} -> {args.candidate[:12]}")
            return 0
        if args.command == "recover-pending":
            changed, message = recover_pending()
            print(message)
            return 10 if changed else 0
        if args.command == "confirm-running":
            print(confirm_running())
            return 0
    except Exception as exc:
        print(f"runtime manager BLOCKED: {exc}")
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
