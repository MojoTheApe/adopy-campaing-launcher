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

ACL-1649: reuse the PR -> stage -> main chain.

- Покрытие складывается по заданиям из нескольких прогонов одного дерева: stage
  проверяет 3.13 + Windows, PR в main добирает 3.11, а push в main не запускает
  ничего (``find_coverage``). Релизные ворота по-прежнему требуют полный набор.
- Прогон ``pull_request`` теперь тоже доказательство, но только с аннотацией, которую его
  собственное scope-задание записало о реально проверенном дереве (``tested_tree``), и только
  для головы PR, влитого именно этим коммитом (``verify_pr_heads``: API ``commits/<sha>/pulls``,
  ``merge_commit_sha`` == коммит). Дерево merge-коммита PR видно только самому прогону, поэтому
  оно записывается там же; ``run.pull_requests`` после слияния пуст и для привязки не годится.
  Почему раньше reuse не срабатывал: прогоны ``pull_request`` не принимались вовсе, а голову PR
  squash-коммит stage не содержит.

Any doubt (API error, missing data, no matching run) returns ``None``: the
caller runs everything in full.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
import urllib.parse
import urllib.request

SHA = re.compile(r"[0-9a-f]{40}")
# owner/repo GitHub: без «..», пробелов и лишних сегментов пути.
REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_-][A-Za-z0-9_.-]*")
# События, у которых проверенный коммит — это head_sha запуска.
PROVABLE_EVENTS = {"push", "workflow_dispatch"}
# Сколько последних коммитов с тем же деревом проверяем: продвижение даёт 2-3.
MAX_CANDIDATES = 20

# Версии Python полного прогона ci.yml. ACL-1581: PR без изменений зависимостей
# и бутстрапа гоняет только 3.13 (scripts/acl_ci_scope.py), поэтому такой прогон
# никогда не «полный»; push-прогоны и запасной список матрицы — эти версии.
FULL_PYTHON_VERSIONS = ("3.11", "3.13")
# ACL-1649: PR в stage и push в stage проверяют только 3.13; 3.11 — перед main
# (PR в main, push в main/master, релиз, ручной запуск).
BASE_PYTHON_VERSIONS = ("3.13",)

# ACL-1582: раскладка полного push-прогона ci.yml. Непомеченные тесты идут на
# Linux (все версии полного прогона × шарды), помеченные windows_only — одним
# заданием на Windows. Имена заданий заданы в ci.yml явно (`name:`), а тесты
# (tests/test_acl_ci_reuse.py, tests/test_ci_layout.py) сверяют этот список с
# workflow, чтобы новая версия Python или шард не сделали «полным» прогон без них.
CI_LINUX_PYTHONS = FULL_PYTHON_VERSIONS
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
    return subprocess.check_output(["git", *args], cwd=root, text=True, encoding="utf-8", errors="replace").strip()


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


def run_header_ok(run, repo, workflow, commit, exclude_run_id=None, events=PROVABLE_EVENTS):
    """Фильтр API не принимаем на веру: каждое условие проверяем сами."""
    return not (not isinstance(run.get("id"), int) or str(run["id"]) == str(exclude_run_id or "")
                or run.get("head_sha") != commit
                or run.get("path") != f".github/workflows/{workflow}"
                or run.get("event") not in events
                or run.get("status") != "completed" or run.get("conclusion") != "success"
                or (run.get("repository") or {}).get("full_name") != repo
                or (run.get("head_repository") or {}).get("full_name") != repo
                or not valid_run_url(run.get("html_url"), repo, run["id"]))


# ACL-2107: список запусков GitHub по head_sha иногда приходит пустым, а commits/<sha>/pulls
# иногда падает. Релизные ворота повторяют такие запросы с паузой; сам CI (scope) - нет.
RETRY_ATTEMPTS = 3
RETRY_PAUSE = 2.0


def retrying(api, attempts=None):
    """Обёртка ``api``: ошибка запроса повторяется с паузой, после последней попытки - исключение."""
    def call(path):
        total = max(1, attempts or RETRY_ATTEMPTS)
        for attempt in range(total):
            try:
                return api(path)
            except Exception:  # noqa: BLE001 - сеть/лимиты GitHub
                if attempt + 1 >= total:
                    raise
                time.sleep(RETRY_PAUSE)
    return call


def candidate_runs(api, repo, workflow, commit, exclude_run_id=None, events=PROVABLE_EVENTS, empty_retries=0):
    """Успешные запуски ``workflow`` на ``commit`` (только заголовок, без заданий).

    ``empty_retries``: сколько раз повторить, если список пришёл пустым (флак листинга GitHub)."""
    if not SHA.fullmatch(commit or ""):
        return []
    query = urllib.parse.urlencode({"head_sha": commit, "status": "success", "per_page": 20})
    payload = api(f"repos/{repo}/actions/workflows/{workflow}/runs?{query}")
    for _ in range(empty_retries):
        if payload.get("workflow_runs"):
            break
        time.sleep(RETRY_PAUSE)
        payload = api(f"repos/{repo}/actions/workflows/{workflow}/runs?{query}")
    return [run for run in payload.get("workflow_runs") or []
            if run_header_ok(run, repo, workflow, commit, exclude_run_id, events)]


def proven_run(api, repo, workflow, commit, exclude_run_id=None):
    """Успешный полный запуск ``workflow`` на ``commit`` или None."""
    for run in candidate_runs(api, repo, workflow, commit, exclude_run_id):
        if full_jobs_passed(api, repo, run, workflow):
            return run
    return None


def passed_names(jobs):
    """Имена заданий, у которых все строки последней попытки завершились успехом."""
    return {name for name, rows in jobs.items()
            if rows and all(r.get("status") == "completed" and r.get("conclusion") == "success" for r in rows)}


def required_jobs(workflow, pythons=FULL_PYTHON_VERSIONS, windows=True, linux=True):
    """Имена тестовых заданий, которые должны пройти в данном контексте (ACL-1649).

    Задание-итог (``ACL verification``) сюда не входит: оно всегда выполняется в самом
    прогоне, а прогон-источник принимается только целиком успешным."""
    if workflow != "ci.yml":
        return set(REQUIRED_JOBS[workflow])
    names = set()
    if linux:
        names |= {ci_linux_job(p, s) for p in pythons for s in CI_LINUX_SHARDS}
    if windows:
        names |= {ci_windows_job(p) for p in CI_WINDOWS_PYTHONS}
    return names


# ACL-1649: прогон pull_request доказывает дерево, только если его собственное scope-задание
# записало аннотацию с деревом, которое он реально проверял (checkout merge-коммита PR).
# Почему не run.pull_requests: после слияния PR и удаления ветки GitHub отдаёт там пустой список
# (проверено на прогонах влитых PR), так что привязка по нему никогда не сработала бы на stage.
MARKER_TITLE = "acl-tested-tree"
MARKER_MESSAGE = re.compile(r"tree=([0-9a-f]{40})")
SCOPE_JOB = {"ci.yml": "Classify and check project tooling", "windows-installer.yml": "Classify installer changes"}


def marker_line(tree):
    """Строка для stdout scope-шага: команда GitHub Actions, создающая аннотацию задания."""
    return f"::notice title={MARKER_TITLE}::tree={tree}"


REUSE_TITLE = "acl-reused-runs"
REUSE_MESSAGE = re.compile(r"runs=(\d+(?:,\d+)*)")


def reuse_marker_line(run_urls, repo):
    """Аннотация scope-задания: id прогонов, чьи задания этот прогон переиспользовал (ACL-2107)."""
    ids = []
    for url in run_urls or ():
        match = re.fullmatch(r"https://github\.com/" + re.escape(repo) + r"/actions/runs/(\d+)", str(url))
        if match and match[1] not in ids:
            ids.append(match[1])
    return f"::notice title={REUSE_TITLE}::runs={','.join(ids)}" if ids else ""


def reused_run_ids(api, repo, jobs, workflow):
    """Id прогонов из аннотации scope-задания прогона; нет аннотации или ошибка - пустой список."""
    rows = jobs.get(SCOPE_JOB.get(workflow)) or []
    if len(rows) != 1 or not isinstance(rows[0].get("id"), int):
        return []
    found = []
    try:
        for note in api(f"repos/{repo}/check-runs/{rows[0]['id']}/annotations?per_page=50") or []:
            if note.get("title") == REUSE_TITLE:
                match = REUSE_MESSAGE.fullmatch(str(note.get("message") or "").strip())
                if match:
                    found += [int(x) for x in match[1].split(",") if int(x) not in found]
    except Exception:  # noqa: BLE001 - запасной источник; без него работает поиск по head_sha
        return []
    return found[:20]


def tested_tree(api, repo, jobs, workflow):
    """Дерево из аннотации scope-задания прогона или None (нет, неоднозначно или ошибка формата)."""
    rows = jobs.get(SCOPE_JOB.get(workflow)) or []
    if len(rows) != 1 or not isinstance(rows[0].get("id"), int):
        return None
    found = set()
    for note in api(f"repos/{repo}/check-runs/{rows[0]['id']}/annotations?per_page=50") or []:
        if note.get("title") == MARKER_TITLE:
            match = MARKER_MESSAGE.fullmatch(str(note.get("message") or "").strip())
            if not match:
                return None
            found.add(match[1])
    return found.pop() if len(found) == 1 else None


def verify_pr_heads(root, api, repo, commit, branch="", strict=False):
    """Головы PR, влитых именно коммитом ``commit`` (кандидаты для поиска прогонов ``pull_request``).

    Сама голова ничего не доказывает: прогон принимается только с аннотацией о проверенном
    дереве (``tested_tree``). Здесь только привязка «этот PR влит этим коммитом»:
    ``merge_commit_sha`` равен ``commit`` (commits/<sha>/pulls отдаёт и PR, где commit лишь
    один из коммитов), PR влит и из этого же репозитория. Ветка ``branch``, если задана, — база PR.
    Ошибка API - пустой список; при ``strict`` (релизные ворота) - исключение, а не «нет PR»."""
    if not SHA.fullmatch(commit or "") or not REPO.fullmatch(repo or ""):
        return []
    try:
        prs = api(f"repos/{repo}/commits/{commit}/pulls?per_page=10")
    except Exception:  # noqa: BLE001 — нет ответа API: без PR-доказательства, остальные источники работают
        if strict:
            raise
        return []
    heads = []
    for pr in prs if isinstance(prs, list) else []:
        head, number = (pr.get("head") or {}).get("sha"), pr.get("number")
        base = pr.get("base") or {}
        if (not pr.get("merged_at") or pr.get("merge_commit_sha") != commit
                or (branch and base.get("ref") != branch) or not isinstance(number, int)
                or not SHA.fullmatch(head or "")
                or (base.get("repo") or {}).get("full_name", repo) != repo
                or ((pr.get("head") or {}).get("repo") or {}).get("full_name", repo) != repo):
            continue
        heads.append(head)
    return heads


def collect_pr_heads(root, api, repo, tree, commits=(), strict=False):
    """Головы PR для всех коммитов с деревом ``tree``: сам проверяемый коммит и его двойники.

    Цепочка PR -> stage -> main: у коммита stage есть своя голова PR (и у слияния в main —
    голова stage), и именно прогон на этой голове покрывает дерево, когда прогоны stage и
    main сами пропущены по reuse. ``commits`` проверяются, только если их дерево равно ``tree``."""
    heads = []
    for commit in list(commits) + same_tree_commits(root, tree):
        if tree_of(root, commit) != tree:
            continue
        for head in verify_pr_heads(root, api, repo, commit, "", strict=strict):
            if head not in heads:
                heads.append(head)
    return heads


def find_coverage(root, api, repo, workflow, tree, exclude_run_id=None, pr_heads=(), run_ids=(), empty_retries=0):
    """Какие задания уже прошли на этом дереве: объединение по нескольким прогонам.

    -> {"covered": set имён, "runs": [{run_id, run_url, commit, event, branch, jobs}]}.
    Прогоны ``push``/``workflow_dispatch`` — на коммитах с этим деревом по git. Прогоны
    ``pull_request`` — только на головах из ``pr_heads`` и только с аннотацией о том же дереве
    (``tested_tree``); без аннотации, с другим деревом или чужим PR прогон не принимается.
    Прогон засчитывается, лишь если его итоговое задание прошло и он дал хотя бы одно
    тестовое задание (иначе ``reused_from`` указывал бы на прогон, который ничего не запускал).
    Прогоны старой раскладки не считаются. Исключения API/git не глотаются."""
    covered, runs = set(), []
    if workflow not in REQUIRED_JOBS or not REPO.fullmatch(repo or ""):
        return {"covered": covered, "runs": runs}
    events = {}
    for commit in same_tree_commits(root, tree):
        # Повторная проверка дерева кандидата отдельной командой git.
        if tree_of(root, commit) == tree:
            events[commit] = set(PROVABLE_EVENTS)
    for head in pr_heads:
        if SHA.fullmatch(head or ""):
            # Только pull_request (и только с аннотацией): push/dispatch на голове PR доказывают дерево
            # самой головы, а не дерево влитого коммита. Если голова при этом ещё и коммит с тем же
            # деревом, push-прогоны уже допущены циклом выше по git-проверке дерева.
            events.setdefault(head, set()).add("pull_request")
    relevant = set(REQUIRED_JOBS[workflow])
    admission = REQUIRED_JOBS[workflow][-1]

    def accept(run, commit):
        if any(r["run_id"] == run["id"] for r in runs):
            return
        jobs = latest_jobs(api, repo, run)
        if legacy_layout(jobs, workflow):
            return
        if run["event"] == "pull_request" and tested_tree(api, repo, jobs, workflow) != tree:
            return
        passed = passed_names(jobs)
        # Итоговое задание прогона (ACL verification / Installer verification) тоже должно
        # быть успешным: без него прогон не доказательство, даже если часть заданий зелёная.
        if admission not in passed:
            return
        names = passed & relevant
        if (names - {admission}) - covered:
            covered.update(names)
            runs.append({"run_id": run["id"], "run_url": run["html_url"], "commit": commit, "tree": tree,
                         "event": run["event"], "branch": run.get("head_branch"), "jobs": sorted(names)})

    # ACL-2107: прогоны, названные самим main-прогоном (reused_runs), берём напрямую по id -
    # без листинга по head_sha. Проверки те же: заголовок, привязка коммита (дерево git или голова
    # влитого PR), аннотация дерева для pull_request, задания.
    for rid in run_ids:
        if not isinstance(rid, int):
            continue
        run = api(f"repos/{repo}/actions/runs/{rid}")
        commit = (run or {}).get("head_sha")
        if commit in events and run_header_ok(run, repo, workflow, commit, exclude_run_id, events[commit]):
            accept(run, commit)
    for commit, allowed in events.items():
        for run in candidate_runs(api, repo, workflow, commit, exclude_run_id, allowed, empty_retries):
            accept(run, commit)
    return {"covered": covered, "runs": runs}


def find_reuse(root, api, repo, workflow, tree, exclude_run_id=None, pr_heads=(), run_ids=(), empty_retries=0):
    """Зелёные прогоны того же дерева, вместе покрывающие ПОЛНЫЙ набор, или None.

    Релизные ворота и полный reuse требуют все задания ``REQUIRED_JOBS`` (3.11 + 3.13 +
    Windows). Исключения API/git не глотаются здесь: вызывающий решает «полный прогон»."""
    coverage = find_coverage(root, api, repo, workflow, tree, exclude_run_id, pr_heads, run_ids, empty_retries)
    if not coverage["runs"] or not set(REQUIRED_JOBS[workflow]) <= coverage["covered"]:
        return None
    return dict(coverage["runs"][0], runs=coverage["runs"])


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
    return json.loads(subprocess.check_output(["gh", "api", path], text=True, encoding="utf-8", errors="replace"))


def summary_line(run_url, tree):
    return f"Reused full results of run {run_url} (same tree {tree})"

