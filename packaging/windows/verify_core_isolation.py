"""Verify runtime isolation or clean integrated build inputs.

Frozen mode preserves the original pilot boundary. Integrated mode packages the
current reviewed commit, requires the integration baseline as an ancestor, and
build/CI commands additionally require a completely clean source worktree.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST = Path(__file__).with_name("baseline.json")
_SHA = re.compile(r"^[0-9a-f]{40}$")


class IsolationError(RuntimeError):
    """The guard could not establish a trustworthy comparison."""


@dataclass(frozen=True)
class BaselineManifest:
    baseline_sha: str
    baseline_branch: str
    implementation_branch: str
    allowed_exact_paths: frozenset[str]
    allowed_path_prefixes: tuple[str, ...]
    runtime_policy: str = "frozen"


def _normalise_repo_path(raw: str) -> str:
    """Return a stable Git-style path and reject paths outside the repository."""

    value = raw.strip().replace("\\", "/")
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts:
        raise IsolationError(f"invalid repository path in baseline manifest: {raw!r}")
    return path.as_posix()


def load_manifest(path: Path = DEFAULT_MANIFEST) -> BaselineManifest:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise IsolationError(f"cannot read baseline manifest {path}: {exc}") from exc

    if not isinstance(payload, dict) or payload.get("schema") != 1:
        raise IsolationError("baseline manifest must be an object with schema=1")

    sha = str(payload.get("baseline_sha") or "").strip().lower()
    if not _SHA.fullmatch(sha):
        raise IsolationError("baseline_sha must contain exactly 40 hexadecimal characters")

    branch = str(payload.get("baseline_branch") or "").strip()
    implementation = str(payload.get("implementation_branch") or "").strip()
    if not branch or not implementation:
        raise IsolationError("baseline_branch and implementation_branch are required")

    exact_raw = payload.get("allowed_exact_paths")
    prefixes_raw = payload.get("allowed_path_prefixes")
    if not isinstance(exact_raw, list) or not isinstance(prefixes_raw, list):
        raise IsolationError("path allowlists must be JSON arrays")

    exact = frozenset(_normalise_repo_path(str(item)) for item in exact_raw)
    prefixes: list[str] = []
    for item in prefixes_raw:
        prefix = _normalise_repo_path(str(item))
        if not prefix.endswith("/"):
            prefix += "/"
        prefixes.append(prefix)

    if not exact and not prefixes:
        raise IsolationError("baseline manifest must allow at least one packaging path")

    policy = payload.get("runtime_policy", "frozen")
    if policy not in {"frozen", "integrated"}:
        raise IsolationError("invalid runtime_policy")
    return BaselineManifest(
        baseline_sha=sha,
        runtime_policy=policy,
        baseline_branch=branch,
        implementation_branch=implementation,
        allowed_exact_paths=exact,
        allowed_path_prefixes=tuple(prefixes),
    )


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise IsolationError(f"git {' '.join(args)} failed: {detail or result.returncode}")
    return result


def is_path_allowed(path: str, manifest: BaselineManifest) -> bool:
    normal = _normalise_repo_path(path)
    return normal in manifest.allowed_exact_paths or any(
        normal.startswith(prefix) for prefix in manifest.allowed_path_prefixes
    )


def changed_paths(root: Path, baseline_sha: str, *, include_untracked: bool) -> list[str]:
    tracked = _git(
        root,
        "diff",
        "--name-only",
        "--no-renames",
        "-z",
        f"{baseline_sha}..HEAD",
    ).stdout.split("\0")

    paths = {line.strip() for line in tracked if line.strip()}
    if include_untracked:
        # The baseline comparison covers committed branch work only. A local
        # guard must also catch staged and unstaged edits to already-tracked
        # runtime files before they become a commit.
        for args in (
            ("diff", "--name-only", "--no-renames", "-z"),
            ("diff", "--cached", "--name-only", "--no-renames", "-z"),
        ):
            working = _git(root, *args).stdout.split("\0")
            paths.update(line.strip() for line in working if line.strip())
        untracked = _git(root, "ls-files", "--others", "--exclude-standard", "-z").stdout.split("\0")
        paths.update(line.strip() for line in untracked if line.strip())
    return sorted(paths)


def verify_isolation(
    root: Path,
    manifest: BaselineManifest,
    *,
    include_untracked: bool = True,
) -> tuple[list[str], list[str]]:
    root = root.resolve()
    if not (root / ".git").exists():
        # Linked Git worktrees have a .git file rather than a directory.
        if not (root / ".git").is_file():
            raise IsolationError(f"not a Git worktree: {root}")

    _git(root, "cat-file", "-e", f"{manifest.baseline_sha}^{{commit}}")
    ancestor = _git(
        root,
        "merge-base",
        "--is-ancestor",
        manifest.baseline_sha,
        "HEAD",
        check=False,
    )
    if ancestor.returncode != 0:
        raise IsolationError(
            "installer HEAD does not descend from the frozen pilot baseline "
            f"{manifest.baseline_sha}"
        )

    changed = changed_paths(
        root,
        "HEAD" if manifest.runtime_policy == "integrated" else manifest.baseline_sha,
        include_untracked=include_untracked,
    )
    blocked = [path for path in changed if not is_path_allowed(path, manifest)]
    return changed, blocked


def _print_paths(label: str, paths: Iterable[str]) -> None:
    items = list(paths)
    print(f"{label}: {len(items)}")
    for path in items:
        print(f"  {path}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument(
        "--tracked-only",
        action="store_true",
        help="Ignore untracked files. CI normally has none; local checks should include them.",
    )
    parser.add_argument("--require-clean", action="store_true", help="Require every build input to match its recorded source commit")
    args = parser.parse_args(argv)

    try:
        manifest = load_manifest(args.manifest)
        if args.require_clean and _git(args.root, "status", "--porcelain", "--untracked-files=all").stdout:
            raise IsolationError("build inputs are dirty; commit reviewed changes before building")
        changed, blocked = verify_isolation(
            args.root,
            manifest,
            include_untracked=not args.tracked_only,
        )
    except IsolationError as exc:
        print(f"installer isolation ERROR: {exc}", file=sys.stderr)
        return 2

    print(f"ACL integration baseline: {manifest.baseline_sha}")
    _print_paths("Changes since baseline", changed)
    if blocked:
        print("\ninstaller isolation BLOCKED", file=sys.stderr)
        _print_paths("Runtime/non-packaging paths changed", blocked)
        print(
            "Only the paths listed in packaging/windows/baseline.json may change "
            "for uncommitted runtime edits; commit and review them before building.",
            file=sys.stderr,
        )
        return 1

    print("installer source guard OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
