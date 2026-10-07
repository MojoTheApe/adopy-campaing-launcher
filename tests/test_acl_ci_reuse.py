"""ACL-1565: reuse a full green run only for a tree git proves identical; any doubt runs in full.

Recorded GitHub API payloads (trimmed) live in tests/fixtures/ci_reuse. ACL-1582:
``ci_full_jobs.json`` is the recorded run renamed to the current Linux + Windows
job layout; the recording itself is ``ci_full_jobs_legacy.json``. Their
commit sha is replaced by a commit of a real temporary git repository, and the
payload's own ``tree_id`` is set to a lie, so every tree comparison here is
computed by git. No test touches the network.
"""
import copy
import json
from pathlib import Path
import subprocess

import pytest
import yaml

from scripts import acl_ci_reuse as reuse
from scripts import acl_ci_scope as scope

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).with_name("fixtures") / "ci_reuse"
REPO = "MojoTheApe/adopy-campaing-launcher"
LIE = "f" * 40


def recorded(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def git(root, *args):
    return subprocess.check_output(["git", *args], cwd=root, text=True).strip()


@pytest.fixture
def promotion(tmp_path):
    """stage S (green full push run) -> main merge commit M with the same tree; X differs."""
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.name", "CI fixture")
    git(tmp_path, "config", "user.email", "ci@example.invalid")
    (tmp_path / "app.py").write_text("# v1\n")
    git(tmp_path, "add", "app.py"); git(tmp_path, "commit", "-qm", "base")
    git(tmp_path, "checkout", "-qb", "stage")
    (tmp_path / "app.py").write_text("# v2\n")
    git(tmp_path, "commit", "-qam", "stage change")
    stage = git(tmp_path, "rev-parse", "HEAD")
    git(tmp_path, "checkout", "-q", "main")
    git(tmp_path, "merge", "-q", "--no-ff", "--no-edit", "-m", "promote", "stage")
    main = git(tmp_path, "rev-parse", "HEAD")
    git(tmp_path, "checkout", "-qb", "other", "stage")
    (tmp_path / "app.py").write_text("# v3\n")
    git(tmp_path, "commit", "-qam", "different tree")
    other = git(tmp_path, "rev-parse", "HEAD")
    git(tmp_path, "checkout", "-q", "main")
    assert git(tmp_path, "rev-parse", "HEAD^{tree}") == git(tmp_path, "rev-parse", f"{stage}^{{tree}}")
    return {"root": tmp_path, "stage": stage, "main": main, "other": other,
            "tree": git(tmp_path, "rev-parse", "HEAD^{tree}")}


class FakeApi:
    """Answers like GitHub from recorded payloads; records every request path."""

    def __init__(self, workflow="ci.yml", runs_by_sha=None, jobs_by_run=None, error=None, ignore_filter=False):
        self.workflow, self.calls, self.error, self.ignore_filter = workflow, [], error, ignore_filter
        self.runs_by_sha, self.jobs_by_run = runs_by_sha or {}, jobs_by_run or {}

    def __call__(self, path):
        self.calls.append(path)
        if self.error:
            raise self.error
        if "/jobs?" in path:
            run_id = int(path.split("/runs/")[1].split("/")[0])
            return self.jobs_by_run.get(run_id, {"total_count": 0, "jobs": []})
        assert f"/actions/workflows/{self.workflow}/runs?" in path
        sha = path.split("head_sha=")[1].split("&")[0]
        if self.ignore_filter:
            return {"workflow_runs": [r for rows in self.runs_by_sha.values() for r in rows]}
        return {"workflow_runs": self.runs_by_sha.get(sha, [])}


def green_api(commit, workflow="ci.yml"):
    """The real recorded stage runs (pull_request + push) and the push run's full jobs, moved to ``commit``."""
    if workflow == "ci.yml":
        runs, jobs = recorded("ci_runs_same_sha.json"), recorded("ci_full_jobs.json")
    else:
        runs, jobs = recorded("installer_runs_same_sha.json"), recorded("installer_full_jobs.json")
    rows = []
    for run in runs["workflow_runs"]:
        run = copy.deepcopy(run)
        run["head_sha"] = run["head_commit"]["id"] = commit
        run["head_commit"]["tree_id"] = LIE
        rows.append(run)
    push = next(r for r in rows if r["event"] == "push")
    pull = next(r for r in rows if r["event"] == "pull_request")
    # У pull_request-запуска те же «полные» задания, но его дерево недоказуемо.
    pull_jobs = copy.deepcopy(jobs)
    for job in pull_jobs["jobs"]:
        job["run_id"] = pull["id"]
    return FakeApi(workflow, {commit: rows}, {push["id"]: jobs, pull["id"]: pull_jobs}), push


def decide(promotion, api, event="push", ref="refs/heads/main", workflow="ci.yml", run_id="999"):
    return scope.decide(promotion["root"], event, {} if event != "pull_request" else None,
                        ref=ref, workflow=workflow, repo=REPO, api=api, run_id=run_id)


# --- the happy path: provable reuse -------------------------------------------------------------

def test_main_push_reuses_the_green_stage_run_of_the_same_tree(promotion):
    api, push = green_api(promotion["stage"])
    result = decide(promotion, api)
    assert result == {"windows": False, "linux": False, "reason": "reused-green-full-run-of-same-tree",
                      "reused_from": push["html_url"], "tree": promotion["tree"], "commit": promotion["stage"],
                      "reused_runs": [push["html_url"]], "python": ["3.11", "3.13"]}
    assert reuse.summary_line(result["reused_from"], result["tree"]) == (f"Reused full results of run {push['html_url']} "
                                          f"(same tree {promotion['tree']})")


def test_stage_to_main_pr_merge_commit_reuses_the_stage_run(promotion, monkeypatch):
    # The PR checkout is a merge commit; only its git tree matters.
    api, push = green_api(promotion["stage"])
    monkeypatch.setattr(scope, "select", lambda root, name, event, **kw: {
        "windows": True, "linux": True, "reason": "product", "python": ["3.11", "3.13"]})
    result = scope.decide(promotion["root"], "pull_request", {}, ref="refs/pull/7/merge",
                          workflow="ci.yml", repo=REPO, api=api, run_id="999")
    assert result["windows"] is False and result["reused_from"] == push["html_url"]


def test_installer_classify_reuses_the_same_way(promotion):
    api, push = green_api(promotion["stage"], "windows-installer.yml")
    result = decide(promotion, api, workflow="windows-installer.yml")
    assert result["windows"] is False and result["reused_from"] == push["html_url"]
    assert all("/workflows/windows-installer.yml/" in c for c in api.calls if "/jobs?" not in c and "/pulls" not in c)


# --- any doubt runs in full ---------------------------------------------------------------------

@pytest.mark.parametrize("event,ref", [("workflow_dispatch", "refs/heads/main"),
                                       ("push", "refs/tags/acl-release-2026-10-04"),
                                       ("schedule", "refs/heads/main"), ("", "")])
def test_release_tags_and_manual_runs_always_run_in_full(promotion, event, ref):
    api, _ = green_api(promotion["stage"])
    assert decide(promotion, api, event=event, ref=ref)["windows"] is True
    assert api.calls == []


def test_no_token_means_no_lookup_and_a_full_run(promotion):
    assert decide(promotion, None)["windows"] is True


def test_api_error_runs_in_full(promotion):
    result = decide(promotion, FakeApi(error=OSError("HTTP 500")))
    assert result["windows"] is True and result["reuse"] == "lookup-failed-run-full"


def test_no_matching_run_runs_in_full(promotion):
    result = decide(promotion, FakeApi())
    assert result["windows"] is True and result["reuse"] == "no-proven-full-run"


def test_a_green_run_of_a_different_tree_is_never_reused(promotion):
    # The API ignores head_sha and offers a green run for another commit; git says the tree differs.
    api, _ = green_api(promotion["other"])
    api.ignore_filter = True
    assert decide(promotion, api)["windows"] is True


@pytest.mark.parametrize("field,value", [
    ("event", "pull_request"), ("conclusion", "failure"), ("conclusion", "cancelled"),
    ("status", "in_progress"), ("path", ".github/workflows/release.yml"),
    ("repository", {"full_name": "someone/fork"}), ("head_repository", {"full_name": "someone/fork"}),
    ("head_sha", LIE), ("html_url", "http://example.invalid/run"),
    ("id", "37197547291"),                                                  # R11: id must be an int
    ("html_url", "https://github.com/someone/fork/actions/runs/37197547291"),
    ("html_url", "https://github.com/MojoTheApe/adopy-campaing-launcher/actions/runs/1"),  # another run
    ("html_url", "https://github.com/MojoTheApe/adopy-campaing-launcher/actions/runs/37197547291\nwindows=true"),
    ("html_url", None),
])
def test_untrustworthy_candidate_run_is_refused(promotion, field, value):
    api, push = green_api(promotion["stage"])
    for run in api.runs_by_sha[promotion["stage"]]:
        run[field] = value
    assert decide(promotion, api)["windows"] is True


@pytest.mark.parametrize("mutate", [
    lambda jobs: jobs.pop(1),                                             # a shard is missing
    lambda jobs: jobs[1].update(conclusion="skipped"),                    # the run itself reused
    lambda jobs: jobs[2].update(conclusion="cancelled"),
    lambda jobs: jobs[3].update(conclusion="failure"),
    lambda jobs: jobs[4].update(status="in_progress", conclusion=None),
    lambda jobs: jobs[4].update(status="queued"),                         # R15: success but not completed
    lambda jobs: jobs[-1].update(conclusion="skipped"),                   # ACL verification
    lambda jobs: jobs[1].update(run_id=1),                                # a job of another run
    lambda jobs: jobs.append(dict(jobs[1], conclusion="failure")),        # duplicate name failed
    # ACL-1582: the Windows job of the new layout is required like any shard.
    lambda jobs: jobs.remove(next(j for j in jobs if j["name"] == "windows tests (3.13)")),
    lambda jobs: next(j for j in jobs if j["name"] == "windows tests (3.13)").update(conclusion="skipped"),
    lambda jobs: next(j for j in jobs if j["name"] == "windows tests (3.13)").update(conclusion="failure"),
    # A green job of the old layout next to the new ones makes the run doubtful.
    lambda jobs: jobs.append(dict(jobs[1], name="test (3.13, 1)")),
])
def test_partial_cancelled_or_failed_matrix_is_refused(promotion, mutate):
    api, push = green_api(promotion["stage"])
    mutate(api.jobs_by_run[push["id"]]["jobs"])
    result = decide(promotion, api)
    # ACL-1649: покрытие считается по заданиям, но «всё переиспользовано» — только при полном наборе.
    assert result["windows"] is True or result["linux"] is True


def test_a_green_full_run_of_the_old_layout_is_never_reused(promotion):
    """ACL-1582: decided — runs with the old job names are refused, not translated."""
    api, push = green_api(promotion["stage"])
    legacy = recorded("ci_full_jobs_legacy.json")
    assert all(j["conclusion"] == "success" for j in legacy["jobs"])
    api.jobs_by_run[push["id"]] = legacy
    result = decide(promotion, api)
    assert result["windows"] is True and result["reuse"] == "no-proven-full-run"


def test_the_ci_layout_requires_every_linux_shard_and_the_windows_job():
    required = set(reuse.REQUIRED_JOBS["ci.yml"])
    assert {f"linux tests ({p}, {s})" for p in ("3.11", "3.13") for s in (1, 2, 3)} <= required
    assert "windows tests (3.13)" in required and "ACL verification" in required
    assert not any(reuse.LEGACY_CI_JOB.fullmatch(name) for name in required)
    assert reuse.legacy_layout({"test (3.11, 1)": []}, "ci.yml")
    assert not reuse.legacy_layout({"test (3.11, 1)": []}, "windows-installer.yml")


def test_partial_installer_run_is_refused(promotion):
    api, push = green_api(promotion["stage"], "windows-installer.yml")
    for job in api.jobs_by_run[push["id"]]["jobs"]:
        if job["name"] == "Build, install, repair, and uninstall":
            job["conclusion"] = "skipped"
    assert decide(promotion, api, workflow="windows-installer.yml")["windows"] is True


def test_the_current_run_is_never_its_own_proof(promotion):
    api, push = green_api(promotion["stage"])
    assert decide(promotion, api, run_id=str(push["id"]))["windows"] is True


def test_docs_only_pr_stays_light_without_any_api_call(promotion, monkeypatch):
    api = FakeApi(error=AssertionError("must not be called"))
    monkeypatch.setattr(scope, "select", lambda root, name, event, **kw: {"windows": False, "linux": False, "reason": "docs"})
    assert scope.decide(promotion["root"], "pull_request", {}, api=api, repo=REPO)["windows"] is False


def test_unknown_workflow_is_never_reused(promotion):
    api, _ = green_api(promotion["stage"])
    assert reuse.find_reuse(promotion["root"], api, REPO, "release.yml", promotion["tree"]) is None


# --- the CLI writes outputs and the run summary --------------------------------------------------

def test_cli_writes_reuse_outputs_and_summary(promotion, monkeypatch, tmp_path_factory):
    api, push = green_api(promotion["stage"])
    out = tmp_path_factory.mktemp("gh")
    real_decide = scope.decide
    monkeypatch.setattr(scope.reuse, "github_api", lambda token, base: api)
    # Корень репозитория CLI берёт от файла скрипта; подменяем на временный git.
    monkeypatch.setattr(scope, "decide", lambda root, *a, **k: real_decide(promotion["root"], *a, **k))
    for key, value in {"GITHUB_TOKEN": "test-token", "GITHUB_REPOSITORY": REPO, "GITHUB_RUN_ID": "999",
                       "GITHUB_REF": "refs/heads/main", "GITHUB_OUTPUT": str(out / "o"),
                       "GITHUB_STEP_SUMMARY": str(out / "s")}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr("sys.argv", ["acl_ci_scope.py", "--event-name", "push", "--event-file", str(out / "none")])
    scope.main()
    assert (out / "o").read_text(encoding="utf-8") == (f"windows=false\nlinux=false\nreused_from={push['html_url']}\n"
                                                       'python=["3.11", "3.13"]\n')
    assert (out / "s").read_text(encoding="utf-8").strip() == (
        f"Reused full results of run {push['html_url']} (same tree {promotion['tree']})")


def test_cli_without_token_runs_in_full(monkeypatch, tmp_path):
    for key in ("GITHUB_TOKEN", "GITHUB_STEP_SUMMARY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "o"))
    monkeypatch.setattr("sys.argv", ["acl_ci_scope.py", "--event-name", "push", "--event-file", str(tmp_path / "x")])
    scope.main()
    assert (tmp_path / "o").read_text(encoding="utf-8") == 'windows=true\nlinux=true\nreused_from=\npython=["3.11", "3.13"]\n'


# --- the workflows are wired to it ----------------------------------------------------------------

def workflow(name):
    return yaml.safe_load((ROOT / ".github/workflows" / name).read_text(encoding="utf-8"))


def test_required_ci_jobs_match_the_real_test_matrix():
    # ACL-1581: версии Python Linux-заданий приходят из scope; push-прогон получает
    # полный список, при сбое scope — запасной полный список из самого выражения.
    # ACL-1582: Linux-задания по версиям и шардам, Windows-задание по версиям.
    ci = workflow("ci.yml")
    linux = ci["jobs"]["test-linux"]["strategy"]["matrix"]
    windows = ci["jobs"]["test-windows"]["strategy"]["matrix"]
    expression = linux["python-version"]
    assert expression == "${{ fromJSON(needs.scope.outputs.python || '[\"3.11\", \"3.13\"]') }}"
    fallback = json.loads(expression.split("|| '")[1].split("')")[0])
    push_versions = scope.classify([], "push")["python"]
    assert fallback == push_versions == list(reuse.FULL_PYTHON_VERSIONS) == list(reuse.CI_LINUX_PYTHONS)
    expected = {reuse.ci_linux_job(p, s) for p in push_versions for s in linux["shard"]}
    expected |= {reuse.ci_windows_job(p) for p in windows["python-version"]}
    expected.add(ci["jobs"]["admission"]["name"])
    assert set(reuse.REQUIRED_JOBS["ci.yml"]) == expected
    assert len(reuse.REQUIRED_JOBS["ci.yml"]) == 8
    assert ci["jobs"]["test-linux"]["name"] == "linux tests (${{ matrix.python-version }}, ${{ matrix.shard }})"
    assert ci["jobs"]["test-windows"]["name"] == "windows tests (${{ matrix.python-version }})"


# --- ACL-1581: a one-Python PR run is never a proof -------------------------------------------------

def one_python(jobs, run_id):
    """Задания PR-прогона ACL-1581: Linux только 3.13 × 3 шарда, Windows 3.13 и агрегат."""
    jobs["jobs"] = [dict(j, run_id=run_id) for j in jobs["jobs"] if not j["name"].startswith("linux tests (3.11")]
    assert sum(j["name"].startswith("linux tests (3.13") for j in jobs["jobs"]) == 3
    return jobs


def test_a_one_python_pr_run_is_never_a_proof(promotion):
    api, push = green_api(promotion["stage"])
    # Остаётся только pull_request-запуск с тремя заданиями 3.13 — как настоящий PR после ACL-1581.
    api.runs_by_sha[promotion["stage"]] = [r for r in api.runs_by_sha[promotion["stage"]] if r["event"] == "pull_request"]
    pull = api.runs_by_sha[promotion["stage"]][0]
    one_python(api.jobs_by_run[pull["id"]], pull["id"])
    for event in ("push", "pull_request"):
        result = decide(promotion, api, event=event)
        assert result["windows"] is True and result["reuse"] == "no-proven-full-run"
        assert result["linux"] is True


def test_a_one_python_matrix_is_not_full_even_on_a_push_run(promotion):
    api, push = green_api(promotion["stage"])
    one_python(api.jobs_by_run[push["id"]], push["id"])
    # ACL-1649: Windows и 3.13 покрыты, поэтому на пути в main недостающее — только 3.11.
    result = decide(promotion, api)
    assert result["windows"] is False and result["linux"] is True and result["python"] == ["3.11"]
    # Релизные ворота такого доказательства не принимают: полный набор требует и 3.11.
    assert reuse.find_reuse(promotion["root"], api, REPO, "ci.yml", promotion["tree"]) is None


def test_a_full_push_run_is_a_proof(promotion):
    api, push = green_api(promotion["stage"])
    assert push["event"] == "push"
    assert reuse.full_jobs_passed(api, REPO, push, "ci.yml") is True
    assert decide(promotion, api)["reused_from"] == push["html_url"]


def test_required_installer_jobs_match_the_real_workflow():
    jobs = workflow("windows-installer.yml")["jobs"]
    assert set(reuse.REQUIRED_JOBS["windows-installer.yml"]) == {
        jobs["phase-1-isolation"]["name"], jobs["phase-2-packaging-spike"]["name"], jobs["admission"]["name"]}


@pytest.mark.parametrize("name,flag", [("ci.yml", "ci.yml"), ("windows-installer.yml", "windows-installer.yml")])
def test_scope_jobs_read_actions_and_publish_the_reused_run(name, flag):
    data = workflow(name)
    # Только задание scope читает Actions; остальные (код PR на Windows) — нет.
    assert data["permissions"] == {"contents": "read"}
    job = data["jobs"]["scope"]
    # ACL-1649: pull-requests: read — связать коммит stage/main с влитым PR (commits/<sha>/pulls).
    # checks: read — аннотация scope-задания прогона PR о проверенном дереве (tested_tree).
    assert job["permissions"] == {"contents": "read", "actions": "read", "pull-requests": "read", "checks": "read"}
    assert all("permissions" not in other for key, other in data["jobs"].items() if key != "scope")
    assert job["outputs"]["reused_from"] == "${{ steps.scope.outputs.reused_from }}"
    step = next(s for s in job["steps"] if s.get("id") == "scope")
    assert step["run"] == f"python3 scripts/acl_ci_scope.py --workflow {flag}"
    assert step["env"]["GITHUB_TOKEN"] == "${{ github.token }}"
    # Release tags and manual runs never get here as a reuse (classify keeps them full).
    triggers = data[True]
    assert "tags" not in (triggers.get("push") or {})


# --- review L1/L3: the remaining guards ------------------------------------------------------------

@pytest.mark.parametrize("commit", ["", None, "abc", "F" * 40, "g" * 40, "a" * 39])
def test_candidate_commit_must_be_an_exact_sha_before_any_api_call(commit):
    api = FakeApi(error=AssertionError("must not be called"))
    assert reuse.proven_run(api, REPO, "ci.yml", commit) is None and api.calls == []


@pytest.mark.parametrize("repo", ["", None, "owner", "owner/repo/extra", "own er/repo", "owner/re po", "../x"])
def test_malformed_repository_is_never_queried(promotion, repo):
    api = FakeApi(error=AssertionError("must not be called"))
    assert reuse.find_reuse(promotion["root"], api, repo, "ci.yml", promotion["tree"]) is None
    assert api.calls == []


@pytest.mark.parametrize("tree", ["", None, "abc", "z" * 40, "A" * 40])
def test_tree_must_be_an_exact_sha(promotion, tree):
    with pytest.raises(ValueError, match="exact tree sha"):
        reuse.same_tree_commits(promotion["root"], tree)


@pytest.mark.parametrize("url,ok", [
    ("https://github.com/MojoTheApe/adopy-campaing-launcher/actions/runs/37197547291", True),
    ("https://github.com/MojoTheApe/adopy-campaing-launcher/actions/runs/37197547291/", False),
    ("https://github.com/MojoTheApe/adopy-campaing-launcher/actions/runs/37197547291\n", False),
    ("https://github.com/MojoTheApe/adopy-campaing-launcher/actions/runs/123", False),
    ("https://github.com.evil/MojoTheApe/adopy-campaing-launcher/actions/runs/37197547291", False),
    ("http://github.com/MojoTheApe/adopy-campaing-launcher/actions/runs/37197547291", False),
    ("https://github.com/MojoTheApe/other/actions/runs/37197547291", False),
])
def test_run_url_must_be_this_repositorys_exact_run(url, ok):
    assert reuse.valid_run_url(url, REPO, 37197547291) is ok


def test_an_invalid_reused_url_is_never_written_to_outputs(promotion, monkeypatch, tmp_path):
    # Даже если find_reuse вернул доказательство, кривой URL не уходит в GITHUB_OUTPUT.
    bad = "https://github.com/MojoTheApe/adopy-campaing-launcher/actions/runs/1\nwindows=false"
    monkeypatch.setattr(scope.reuse, "find_coverage", lambda *a, **k: {
        "covered": set(reuse.REQUIRED_JOBS["ci.yml"]),
        "runs": [{"run_id": 1, "run_url": bad, "commit": promotion["stage"], "tree": promotion["tree"]}]})
    real_decide = scope.decide
    monkeypatch.setattr(scope.reuse, "github_api", lambda token, base: FakeApi())
    monkeypatch.setattr(scope, "decide", lambda root, *a, **k: real_decide(promotion["root"], *a, **k))
    for key, value in {"GITHUB_TOKEN": "test-token", "GITHUB_REPOSITORY": REPO, "GITHUB_RUN_ID": "999",
                       "GITHUB_REF": "refs/heads/main", "GITHUB_OUTPUT": str(tmp_path / "o"),
                       "GITHUB_STEP_SUMMARY": str(tmp_path / "s")}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr("sys.argv", ["acl_ci_scope.py", "--event-name", "push", "--event-file", str(tmp_path / "x")])
    scope.main()
    assert (tmp_path / "o").read_text(encoding="utf-8") == 'windows=true\nlinux=true\nreused_from=\npython=["3.11", "3.13"]\n'
    assert "runs/1" not in (tmp_path / "s").read_text(encoding="utf-8")


def test_cli_writes_one_python_for_a_code_only_pr(monkeypatch, tmp_path):
    # ACL-1581: PR без зависимостей/бутстрапа получает матрицу только из 3.13.
    for key in ("GITHUB_TOKEN", "GITHUB_STEP_SUMMARY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(scope, "select", lambda root, name, event, **kw: scope.classify(["dashboard.py"], "pull_request", "stage"))
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "o"))
    monkeypatch.setattr("sys.argv", ["acl_ci_scope.py", "--event-name", "pull_request", "--event-file", str(tmp_path / "x")])
    scope.main()
    assert (tmp_path / "o").read_text(encoding="utf-8") == 'windows=true\nlinux=true\nreused_from=\npython=["3.13"]\n'
