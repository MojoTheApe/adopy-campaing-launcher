"""Prove that this exact tree already passed a full CI run, or return nothing.

ACL-1565: a promotion (stage push -> PR stage->main -> main push) used to rerun
the full Windows suite and the installer on a tree that was already green.
Reuse is allowed only when it is provable:

- the tree comes from git (``<commit>^{tree}``), never from the API payload;
- the earlier run is a ``push`` or ``workflow_dispatch`` run of the same workflow
  file in this repository, so the commit it checked out is its ``head_sha``
  (a ``pull_request`` run tests a temporary merge commit whose tree cannot be
  proven later, so it never counts);
- the run concluded ``success`` and every required job of the full matrix ran
  and succeeded in its latest attempt (a run that itself reused, or was partial,
  skipped or cancelled, does not count);
- ACL-1582: a ci.yml run must have the current job layout (Linux and Windows
  test jobs). A run of the old layout (``test (py, shard)``, all on Windows) is
  refused even if green: it ran a different split of the suite.

Any doubt (API error, missing data, no matching run) returns ``None``: the
caller runs everything in full.
"""
from __future__ import annotations

import json
import re
import subprocess
import urllib.parse
import urllib.request

SHA = re.compile(r"[0-9a-f]{40}")
# owner/repo GitHub: без «..», пробелов и лишних сегментов пути.
REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_-][A-Za-z0-9_.-]*")
# События, у которых проверенный коммит — это head_sha запуска.
PROVABLE_EVENTS = {"push", "workflow_dispatch"}
# Сколько последних коммитов с тем же деревом проверяем: продвижение даёт 2-3.
MAX_CANDIDATES = 20

# ACL-1582: раскладка полного push-прогона ci.yml. Непомеченные тесты идут на
# Linux (обе версии Python × шарды), помеченные windows_only — одним заданием
# на Windows. Имена заданий заданы в ci.yml явно (`name:`), а тест
# (tests/test_acl_ci_reuse.py) сверяет этот список с матрицей workflow, чтобы
# новая версия Python или шард не сделали «полным» прогон без них.
CI_LINUX_PYTHONS = ("3.11", "3.13")
CI_LINUX_SHARDS = (1, 2, 3)
CI_WINDOWS_PYTHONS = ("3.13",)


def ci_linux_job(python, shard):
    return f"linux tests ({python}, {shard})"


def ci_windows_job(python):
    return f"windows tests ({python})"


# Задания раскладки до ACL-1582 (всё на Windows): такой прогон не доказательство.
LEGACY_CI_JOB = re.compile(r"test \(\d+\.\d+, \d+\)")

# Обязательные задания полного прогона для каждого workflow.
REQUIRED_JOBS = {
    "ci.yml": tuple(ci_linux_job(p, s) for p in CI_LINUX_PYTHONS for s in CI_LINUX_SHARDS)
    + tuple(ci_windows_job(p) for p in CI_WINDOWS_PYTHONS)
    + ("ACL verification",),
    "windows-installer.yml": ("Verify build inputs", "Build, install, repair, and uninstall",
                              "Installer verification"),
}


def _git(root, *args):
    return subprocess.check_output(["git", *args], cwd=root, text=True).strip()


def tree_of(root, rev):
    """Дерево коммита по git; None, если коммита нет локально."""
    try:
        value = _git(root, "rev-parse", "--verify", "--quiet", f"{rev}^{{tree}}")
    except (OSError, subprocess.SubprocessError):
        return None
    return value if SHA.fullmatch(value) else None


def same_tree_commits(root, tree, limit=MAX_CANDIDATES):
    """Коммиты всех веток/тегов (и HEAD), чьё дерево git равно ``tree``, новые первыми."""
    if not SHA.fullmatch(tree or ""):
        raise ValueError("Expected an exact tree sha")
    found = []
    for line in _git(root, "log", "--all", "--format=%H %T").splitlines():
        commit, _, commit_tree = line.partition(" ")
        if commit_tree == tree and SHA.fullmatch(commit) and commit not in found:
            found.append(commit)
            if len(found) >= limit:
                break
    return found


def latest_jobs(api, repo, run):
    """Задания последней попытки запуска по имени (только задания этого запуска)."""
    payload = api(f"repos/{repo}/actions/runs/{int(run['id'])}/jobs?filter=latest&per_page=100")
    jobs = {}
    for job in payload.get("jobs") or []:
        if job.get("run_id") != run["id"]:
            continue
        jobs.setdefault(job.get("name"), []).append(job)
    return jobs


def legacy_layout(jobs, workflow):
    """Прогон ci.yml в раскладке до ACL-1582 (есть задание ``test (py, shard)``)."""
    return workflow == "ci.yml" and any(LEGACY_CI_JOB.fullmatch(str(name)) for name in jobs)


def jobs_complete(jobs, workflow):
    """Полный набор текущей раскладки прошёл; старая раскладка — никогда."""
    if legacy_layout(jobs, workflow):
        return False
    for name in REQUIRED_JOBS[workflow]:
        rows = jobs.get(name) or []
        if not rows or any(r.get("status") != "completed" or r.get("conclusion") != "success" for r in rows):
            return False
    return True


def full_jobs_passed(api, repo, run, workflow):
    """Все обязательные задания последней попытки запуска реально прошли."""
    return jobs_complete(latest_jobs(api, repo, run), workflow)


def valid_run_url(url, repo, run_id=None):
    """Только ссылка вида https://github.com/<owner>/<repo>/actions/runs/<id> этого репозитория
    (и этого запуска, если id известен): она уходит в GITHUB_OUTPUT и в сводку."""
    if not isinstance(url, str) or not REPO.fullmatch(repo or ""):
        return False
    match = re.fullmatch(r"https://github\.com/" + re.escape(repo) + r"/actions/runs/(\d+)", url)
    return bool(match) and (run_id is None or match[1] == str(run_id))


def proven_run(api, repo, workflow, commit, exclude_run_id=None):
    """Успешный полный запуск ``workflow`` на ``commit`` или None."""
    if not SHA.fullmatch(commit or ""):
        return None
    query = urllib.parse.urlencode({"head_sha": commit, "status": "success", "per_page": 20})
    payload = api(f"repos/{repo}/actions/workflows/{workflow}/runs?{query}")
    for run in payload.get("workflow_runs") or []:
        # Фильтр API не принимаем на веру: каждое условие проверяем сами.
        if (not isinstance(run.get("id"), int) or str(run["id"]) == str(exclude_run_id or "")
                or run.get("head_sha") != commit
                or run.get("path") != f".github/workflows/{workflow}"
                or run.get("event") not in PROVABLE_EVENTS
                or run.get("status") != "completed" or run.get("conclusion") != "success"
                or (run.get("repository") or {}).get("full_name") != repo
                or (run.get("head_repository") or {}).get("full_name") != repo
                or not valid_run_url(run.get("html_url"), repo, run["id"])):
            continue
        if full_jobs_passed(api, repo, run, workflow):
            return run
    return None


def find_reuse(root, api, repo, workflow, tree, exclude_run_id=None):
    """Ссылка на полный зелёный прогон того же дерева или None.

    Исключения API/git не глотаются здесь: вызывающий решает «полный прогон»."""
    if workflow not in REQUIRED_JOBS or not REPO.fullmatch(repo or ""):
        return None
    for commit in same_tree_commits(root, tree):
        # Повторная проверка дерева кандидата отдельной командой git.
        if tree_of(root, commit) != tree:
            continue
        run = proven_run(api, repo, workflow, commit, exclude_run_id)
        if run:
            return {"run_id": run["id"], "run_url": run["html_url"], "commit": commit, "tree": tree,
                    "event": run["event"], "branch": run.get("head_branch")}
    return None


def github_api(token, base_url="https://api.github.com", timeout=20):
    """Клиент только для чтения с токеном задания (permissions: actions: read)."""
    def get(path):
        request = urllib.request.Request(base_url.rstrip("/") + "/" + path.lstrip("/"), headers={
            "Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "acl-ci-reuse"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    return get


def gh_api(path):
    """Тот же запрос через gh (release.yml даёт GH_TOKEN)."""
    return json.loads(subprocess.check_output(["gh", "api", path], text=True))


def summary_line(run_url, tree):
    return f"Reused full results of run {run_url} (same tree {tree})"

