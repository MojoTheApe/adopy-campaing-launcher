"""Allow an ACL auto-update only from the promoted release channel.

The launcher fetches ``origin/main`` but does not trust "a newer commit" as
synonymous with "a release".  The candidate must be a fast-forward, must be
the exact target of an annotated ``acl-release-*`` tag, and the tag annotation
must link to a GitHub Actions run of the release repository.  The GitHub API
is asked about that run: ``.github/workflows/ci.yml``, on ``main``, for
exactly the tagged commit, finished with ``success``.  Branch protection and
the pilot ring remain operational controls; this is the machine-side last line
of defence against rolling an unreviewed commit to every buyer.

The text of the tag is not evidence when the API can answer (audit S06).
Three outcomes, staged like the release signatures (ACL-1357):

* **verified** — the API confirms the run: update.
* **rejected** — a definite negative: the run is readable and does not prove
  the release, or it does not exist while the same token can read the
  repository (a made-up link): refuse in every mode.
* **unverified** — nothing to check with: no token, 401/403, network error, or
  a 404 where the repository itself is not visible to the token.  In warn
  mode (today) the previous check of the tag text decides and the event goes
  to the log and Sentry (``kind=update_trust``, ``gate=unverified``); in
  enforce mode it is a refusal.  An attacker controls neither the buyer's
  token nor the network, so cannot choose this path for a real check.

Enforcement only tightens: ``GATE_ENFORCE = True`` here or
``ACL_UPDATE_GATE=enforce`` on the machine.
"""
from __future__ import annotations

import argparse
import json
import logging
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

log = logging.getLogger("acl.update_gate")

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

# Этап выкатки: False — «проверить нечем» решает текст тега + телеметрия;
# True — такой выпуск не ставится. Переменная машины может только ужесточить.
# Менять вместе с $AclGateEnforce в Install ACL.bat (тест сверяет оба).
GATE_ENFORCE = False
GATE_MODE_ENV = "ACL_UPDATE_GATE"

VERIFIED = "verified"
REJECTED = "rejected"
UNVERIFIED = "unverified"
# По этой метке дешборд понимает, что выпуск пропущен без проверки CI.
UNVERIFIED_NOTE = "CI not verified"

# Ссылку на прогон берём только из этого репозитория: ссылка на чужой — не
# доказательство, её даже не спрашиваем у API.
_RUN_LINK = re.compile(
    r"https://github\.com/" + re.escape(RELEASE_REPO) + r"/actions/runs/(\d+)\b",
    re.IGNORECASE,
)
_ANY_RUN_LINK = re.compile(r"github\.com/[^\s/]+/[^\s/]+/actions/runs/\d+", re.IGNORECASE)
_SHA = re.compile(r"[0-9a-f]{40}")


class CiUnverified(RuntimeError):
    """Nothing to check the CI result with (token, network, visibility)."""


class CiRejected(RuntimeError):
    """The API answered, and the answer does not prove the release."""


def gate_mode() -> str:
    """"enforce" or "warn". Every source can only make it stricter."""
    if GATE_ENFORCE:
        return "enforce"
    if (os.environ.get(GATE_MODE_ENV) or "").strip().lower() == "enforce":
        return "enforce"
    return "warn"


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


# ── токен ───────────────────────────────────────────────────────────────

def _no_prompt_env() -> dict[str, str]:
    """Окружение git, в котором никакой вопрос пользователю невозможен.

    GIT_TERMINAL_PROMPT=0 закрывает только консоль: askpass-программа
    (GIT_ASKPASS, core.askPass, SSH_ASKPASS) всё равно открыла бы окно. Её
    убираем из окружения, а core.askPass обнуляем в командной строке.
    """
    env = {k: v for k, v in os.environ.items() if k.upper() not in {"GIT_ASKPASS", "SSH_ASKPASS"}}
    env.update({"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"})
    return env


def _git_credential_token(cwd: Path | None = None) -> str:
    """Токен, которым этот клон и так ходит в GitHub, — без окон и вопросов.

    Репозиторий приватный: раз `git fetch` по HTTPS прошёл, у git есть учётка
    для github.com (обычно Git Credential Manager). Если её нет (SSH-remote,
    несколько аккаунтов в GCM), окна входа не будет — будет «проверить нечем».
    """
    request = f"protocol=https\nhost=github.com\npath={RELEASE_REPO}.git\n\n"
    try:
        result = subprocess.run(
            ["git", "-c", "core.askPass=", "credential", "fill"],
            cwd=str(cwd or _ROOT),
            input=request,
            capture_output=True,
            text=True,
            timeout=20,
            env=_no_prompt_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    for line in result.stdout.splitlines():
        if line.startswith("password="):
            return line[len("password="):].strip()
    return ""


def _machine_env_token() -> str:
    """ACL_UPDATE_GATE_TOKEN из машинного .env (%LOCALAPPDATA%\\ACL\\.env).

    Лаунчер запускает гейт отдельным процессом, который .env сам не читает;
    это путь «разморозить» машину, где git-учётки нет (docs/RELEASE_TRUST.md).
    """
    try:
        from dotenv import dotenv_values

        from config.paths import get_env_files
    except Exception:  # noqa: BLE001 — без .env просто нет этого источника
        return ""
    for path in get_env_files():
        try:
            value = dotenv_values(path).get("ACL_UPDATE_GATE_TOKEN") or ""
        except Exception:  # noqa: BLE001
            continue
        if value.strip():
            return value.strip()
    return ""


def github_token() -> str:
    """Токен для чтения Actions API, или "" — тогда «проверить нечем»."""
    explicit = (os.environ.get("ACL_UPDATE_GATE_TOKEN") or "").strip() or _machine_env_token()
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


# ── GitHub API ──────────────────────────────────────────────────────────

def _api_get(path: str, token: str) -> Any:
    request = urllib.request.Request(
        f"{API}{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "acl-update-gate",
        },
    )
    with urllib.request.urlopen(request, timeout=API_TIMEOUT_S) as response:
        return json.loads(response.read(1 << 20))


def _repo_visible(token: str) -> bool:
    """Видит ли этот токен сам репозиторий — чтобы отличить выдумку от нехватки прав."""
    try:
        repo = _api_get(f"/repos/{RELEASE_REPO}", token)
    except (urllib.error.URLError, OSError, ValueError):
        return False
    return isinstance(repo, dict) and str(repo.get("full_name", "")).lower() == RELEASE_REPO.lower()


def fetch_run(run_id: str, token: str | None = None) -> dict[str, Any]:
    """The Actions run object. Raises CiRejected / CiUnverified otherwise."""
    if not re.fullmatch(r"\d{1,20}", run_id or ""):
        raise CiRejected("has a malformed run id")
    token = github_token() if token is None else token
    if not token:
        raise CiUnverified("could not be verified: no GitHub credential on this machine")
    try:
        data = _api_get(f"/repos/{RELEASE_REPO}/actions/runs/{run_id}", token)
    except urllib.error.HTTPError as exc:
        # 404 бывает и у выдуманного прогона, и у токена без доступа к Actions.
        # Различаем по тому, виден ли тем же токеном сам репозиторий.
        if exc.code == 404 and _repo_visible(token):
            raise CiRejected(f"does not exist in {RELEASE_REPO} (not a real run)") from None
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
) -> tuple[str, str]:
    """(VERIFIED | REJECTED | UNVERIFIED, why)."""
    getter = fetch_run or globals()["fetch_run"]
    try:
        run = getter(run_id)
    except CiRejected as exc:
        return REJECTED, str(exc)
    except CiUnverified as exc:
        return UNVERIFIED, str(exc)
    except Exception as exc:  # noqa: BLE001 — неожиданность = «проверить нечем»
        return UNVERIFIED, f"could not be verified: {type(exc).__name__}"
    if not isinstance(run, dict):
        return UNVERIFIED, "could not be verified: unexpected API response"
    problem = ci_run_problem(run, run_id, sha)
    return (REJECTED, problem) if problem else (VERIFIED, "")


# ── телеметрия ──────────────────────────────────────────────────────────

_reported: set[tuple[str, str]] = set()


def report_unverified(tag: str, reason: str, mode: str) -> None:
    """Log + Sentry (kind=update_trust, gate=unverified), once per process."""
    key = (tag, reason)
    if key in _reported:
        return
    _reported.add(key)
    log.warning("update gate: %s for %s (%s mode): %s", UNVERIFIED_NOTE, tag, mode, reason)
    try:
        from src import acl_sentry

        if not acl_sentry.init():
            return
        import sentry_sdk

        with sentry_sdk.new_scope() as scope:
            scope.fingerprint = ["acl-update-trust", "gate", UNVERIFIED, mode]
            scope.set_tag("kind", "update_trust")
            scope.set_tag("gate", UNVERIFIED)
            scope.set_tag("gate_mode", mode)
            scope.set_context("release", {"tag": tag, "reason": reason})
            sentry_sdk.capture_message(f"Update gate: {UNVERIFIED_NOTE} ({mode})", level="warning")
    except Exception:  # noqa: BLE001 — телеметрия не должна мешать гейту
        log.debug("update gate telemetry failed", exc_info=True)


def _flush_telemetry() -> None:
    try:
        import sentry_sdk

        sentry_sdk.flush(timeout=5)
    except Exception:  # noqa: BLE001
        pass


# ── гейт ────────────────────────────────────────────────────────────────

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

    mode = gate_mode()
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
        run_ids = list(dict.fromkeys(_RUN_LINK.findall(annotation)))
        if not run_ids:
            where = "another repository's" if _ANY_RUN_LINK.search(annotation) else "no"
            problems.append(f"{tag} links {where} CI run, not a CI run of {RELEASE_REPO}")
            continue
        rejected: list[str] = []
        unverified: list[str] = []
        for run_id in run_ids:
            verdict, why = verify_ci_run(run_id, candidate_sha, fetch_run=fetch_run)
            if verdict == VERIFIED:
                return True, f"{tag} ({candidate_sha[:12]}, CI run {run_id} verified)"
            (rejected if verdict == REJECTED else unverified).append(f"CI run {run_id} {why}")
        if rejected:
            # Определённое «нет» от API перевешивает любое «проверить нечем».
            problems.append(f"{tag}: " + "; ".join(rejected + unverified))
            continue
        reason = "; ".join(unverified)
        if mode == "enforce":
            problems.append(f"{tag}: {reason} (enforced)")
            continue
        # Этап warn: проверить нечем — решает прежняя проверка текста тега.
        if not re.search(r"\bsuccess\b", annotation, re.IGNORECASE):
            problems.append(f"{tag}: {reason}; the tag does not record success either")
            continue
        report_unverified(tag, reason, mode)
        return True, (f"{tag} ({candidate_sha[:12]}) - {UNVERIFIED_NOTE}: {reason}; "
                      "accepted on the release tag's record (warn mode)")

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
    # Лаунчер не читает машинный .env: берём оттуда DSN телеметрии и
    # переключатель этапа. Уже заданные переменные не перезаписываются.
    try:
        from config.paths import load_env

        load_env()
    except Exception:  # noqa: BLE001
        pass
    ok, detail = validate_candidate(args.candidate, enrolling=args.enroll)
    print(("update gate OK: " if ok else "update gate BLOCKED: ") + detail)
    _flush_telemetry()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
