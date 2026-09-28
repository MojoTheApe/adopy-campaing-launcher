"""Allow an ACL auto-update only from the promoted release channel.

The launcher fetches ``origin/main`` but does not trust "a newer commit" as
synonymous with "a release".  The candidate must be a fast-forward, must be
the exact target of an annotated ``acl-release-*`` tag, and the tag annotation
must link to a GitHub Actions run that the GitHub API confirms: a run of
``.github/workflows/ci.yml`` in the release repository, on ``main``, for
exactly the tagged commit, finished with ``success``.  Branch protection and
the pilot ring remain operational controls; this is the machine-side last line
of defence against rolling an unreviewed commit to every buyer.

The text of the tag is never evidence by itself (audit S06): anyone who can
push a tag can also type "success" and a CI-looking link.  No token, no
network or an API error means "not verified" and the update is refused.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts._console import use_utf8  # noqa: E402

use_utf8()

STABLE_REF = "origin/main"

# Выпуски приходят только из этого репозитория и только от полного прогона CI
# на main — те же условия, что scripts/acl_publish_release.py проверяет перед
# публикацией упакованной сборки.
RELEASE_REPO = "MojoTheApe/adopy-campaing-launcher"
CI_WORKFLOW_PATH = ".github/workflows/ci.yml"
CI_BRANCH = "main"
CI_EVENTS = frozenset({"push", "workflow_dispatch"})
API = "https://api.github.com"
API_TIMEOUT_S = 20

# Ссылку на прогон берём только из этого репозитория: ссылка на чужой — не
# доказательство, её даже не спрашиваем у API.
_RUN_LINK = re.compile(
    r"https://github\.com/" + re.escape(RELEASE_REPO) + r"/actions/runs/(\d+)\b",
    re.IGNORECASE,
)
_ANY_RUN_LINK = re.compile(r"github\.com/[^\s/]+/[^\s/]+/actions/runs/\d+", re.IGNORECASE)
_SHA = re.compile(r"[0-9a-f]{40}")


class CiUnverified(RuntimeError):
    """The CI result could not be confirmed; the caller must refuse the update."""


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


# ── проверка прогона CI через GitHub API ────────────────────────────────

def _git_credential_token() -> str:
    """Токен, которым этот клон и так ходит в GitHub, — без окон и вопросов.

    Репозиторий приватный: раз `git fetch` прошёл, у git есть учётка для
    github.com (обычно Git Credential Manager). Спрашиваем её молча: если
    учётки нет, менеджер учёток не должен открывать окно входа посреди запуска
    дешборда — пусть лучше обновление не пройдёт.
    """
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}
    request = f"protocol=https\nhost=github.com\npath={RELEASE_REPO}.git\n\n"
    try:
        result = subprocess.run(
            ["git", "credential", "fill"],
            cwd=str(_ROOT),
            input=request,
            capture_output=True,
            text=True,
            timeout=20,
            env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    for line in result.stdout.splitlines():
        if line.startswith("password="):
            return line[len("password="):].strip()
    return ""


def github_token() -> str:
    """Токен для чтения Actions API, или "" — тогда проверка не пройдена."""
    explicit = (os.environ.get("ACL_UPDATE_GATE_TOKEN") or "").strip()
    if explicit:
        return explicit
    stored = _git_credential_token()
    if stored:
        return stored
    for name in ("GH_TOKEN", "GITHUB_TOKEN"):
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return ""


def fetch_run(run_id: str, token: str | None = None) -> dict[str, Any]:
    """The Actions run object from the GitHub API. Raises CiUnverified on any doubt."""
    if not re.fullmatch(r"\d{1,20}", run_id or ""):
        raise CiUnverified("has a malformed run id")
    token = github_token() if token is None else token
    if not token:
        raise CiUnverified("could not be verified: no GitHub credential on this machine")
    request = urllib.request.Request(
        f"{API}/repos/{RELEASE_REPO}/actions/runs/{run_id}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "acl-update-gate",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=API_TIMEOUT_S) as response:
            data = json.loads(response.read(1 << 20))
    except urllib.error.HTTPError as exc:
        raise CiUnverified(f"could not be verified: GitHub answered HTTP {exc.code}") from None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        # Только имя исключения: в сообщение не должно попасть ничего из запроса.
        raise CiUnverified(f"could not be verified: {type(exc).__name__}") from None
    if not isinstance(data, dict):
        raise CiUnverified("could not be verified: unexpected API response")
    return data


def ci_run_problem(run: dict[str, Any], run_id: str, sha: str) -> str:
    """Why this run does not prove the release, or "" when it does."""
    repository = run.get("repository") if isinstance(run.get("repository"), dict) else {}
    checks = [
        (str(run.get("id", "")) == str(run_id), "is not the run the tag links to"),
        (str(repository.get("full_name", "")).lower() == RELEASE_REPO.lower(),
         f"does not belong to {RELEASE_REPO}"),
        (run.get("path") == CI_WORKFLOW_PATH, f"is not the {CI_WORKFLOW_PATH} workflow"),
        (bool(_SHA.fullmatch(sha)) and run.get("head_sha") == sha, "was not run on the tagged commit"),
        (run.get("head_branch") == CI_BRANCH, f"did not run on {CI_BRANCH}"),
        (run.get("event") in CI_EVENTS, "was not a push/dispatch run of the branch"),
        (run.get("status") == "completed", "has not completed"),
        (run.get("conclusion") == "success", "did not succeed"),
    ]
    for ok, why in checks:
        if not ok:
            return why
    return ""


def verify_ci_run(
    run_id: str,
    sha: str,
    *,
    fetch_run: Callable[[str], dict[str, Any]] | None = None,
) -> tuple[bool, str]:
    """(True, "") only if the GitHub API confirms the run; any error is a refusal."""
    getter = fetch_run or globals()["fetch_run"]
    try:
        run = getter(run_id)
    except CiUnverified as exc:
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001 — любая неожиданность = «не проверено»
        return False, f"could not be verified: {type(exc).__name__}"
    if not isinstance(run, dict):
        return False, "could not be verified: unexpected API response"
    problem = ci_run_problem(run, run_id, sha)
    return (False, problem) if problem else (True, "")


def validate_candidate(
    candidate: str,
    *,
    enrolling: bool = False,
    fetch_run: Callable[[str], dict[str, Any]] | None = None,
) -> tuple[bool, str]:
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
        return False, "enrollment requires the installed commit to equal origin/main"

    if candidate_sha != head_sha:
        ancestor = _run("merge-base", "--is-ancestor", head_sha, candidate_sha)
        if ancestor.returncode != 0:
            return False, "candidate is not a fast-forward from the installed version"

    tags = _out("tag", "--points-at", candidate_sha, "--list", "acl-release-*").splitlines()
    if not tags:
        return False, "origin/main is not the exact target of an ACL release tag"

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
        if not object_match or object_match.group(1) != candidate_sha:
            problems.append(f"{tag} does not annotate the candidate commit")
            continue
        # Слово «success» в тексте тега больше ничего не значит: результат
        # прогона спрашиваем у GitHub API.
        run_ids = list(dict.fromkeys(_RUN_LINK.findall(annotation)))
        if not run_ids:
            where = "another repository's" if _ANY_RUN_LINK.search(annotation) else "no"
            problems.append(f"{tag} links {where} CI run, not a CI run of {RELEASE_REPO}")
            continue
        for run_id in run_ids:
            ok, why = verify_ci_run(run_id, candidate_sha, fetch_run=fetch_run)
            if ok:
                return True, f"{tag} ({candidate_sha[:12]}, CI run {run_id} verified)"
            problems.append(f"{tag}: CI run {run_id} {why}")

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
