"""Allow an ACL auto-update only from the promoted release channel.

The launcher fetches ``origin/master`` but does not trust "a newer commit" as
synonymous with "a release".  The candidate must be a fast-forward, must be
the exact target of an annotated ``acl-release-*`` tag, and the tag annotation
must record a successful GitHub Actions run.  Branch protection and the pilot
ring remain operational controls; this is the machine-side last line of
defence against accidentally rolling a feature branch to every buyer.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts._console import use_utf8  # noqa: E402

use_utf8()

STABLE_REF = "origin/master"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(_ROOT),
        capture_output=True,
        text=True,
        timeout=30,
    )


def _out(*args: str) -> str:
    result = _run(*args)
    return result.stdout.strip() if result.returncode == 0 else ""


def validate_candidate(candidate: str, *, enrolling: bool = False) -> tuple[bool, str]:
    if candidate != STABLE_REF:
        return False, f"update ref must be {STABLE_REF}, got {candidate!r}"

    candidate_sha = _out("rev-parse", f"{candidate}^{{commit}}")
    head_sha = _out("rev-parse", "HEAD")
    if not candidate_sha or not head_sha:
        return False, "could not resolve current or candidate commit"

    dirty = _out("status", "--porcelain", "--untracked-files=no")
    if dirty:
        return False, "tracked local changes present; update must not overwrite them"

    if enrolling and candidate_sha != head_sha:
        return False, "enrollment requires the installed commit to equal origin/master"

    if candidate_sha != head_sha:
        ancestor = _run("merge-base", "--is-ancestor", head_sha, candidate_sha)
        if ancestor.returncode != 0:
            return False, "candidate is not a fast-forward from the installed version"

    tags = _out("tag", "--points-at", candidate_sha, "--list", "acl-release-*").splitlines()
    if not tags:
        return False, "origin/master is not the exact target of an ACL release tag"

    problems: list[str] = []
    for tag in tags:
        tag = tag.strip()
        if not tag:
            continue
        if _out("cat-file", "-t", tag) != "tag":
            problems.append(f"{tag} is lightweight, not an annotated release tag")
            continue
        annotation = _out("cat-file", "-p", tag)
        object_match = re.search(r"^object ([0-9a-f]{40})$", annotation, re.MULTILINE)
        has_success = bool(re.search(r"\bsuccess\b", annotation, re.IGNORECASE))
        has_run = bool(re.search(r"github\.com/.+/actions/runs/\d+", annotation))
        if not object_match or object_match.group(1) != candidate_sha:
            problems.append(f"{tag} does not annotate the candidate commit")
            continue
        if not (has_success and has_run):
            problems.append(f"{tag} does not record a successful CI run URL")
            continue
        return True, f"{tag} ({candidate_sha[:12]})"

    return False, "; ".join(problems) or "no usable annotated release tag"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--candidate", default=STABLE_REF)
    parser.add_argument(
        "--enroll",
        action="store_true",
        help="also require this checkout to already equal the stable release",
    )
    args = parser.parse_args(argv)
    ok, detail = validate_candidate(args.candidate, enrolling=args.enroll)
    print(("update gate OK: " if ok else "update gate BLOCKED: ") + detail)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
