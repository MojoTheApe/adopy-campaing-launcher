"""ACL-1565: reuse a full green run only for a tree git proves identical; any doubt runs in full.

Recorded GitHub API payloads (trimmed) live in tests/fixtures/ci_reuse. Their
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
    assert result == {"windows": False, "reason": "reused-green-full-run-of-same-tree",
                      "reused_from": push["html_url"], "tree": promotion["tree"], "commit": promotion["stage"]}
    assert reuse.summary_line(result["reused_from"], result["tree"]) == (f"Reused full results of run {push['html_url']} "
                                          f"(same tree {promotion['tree']})")


def test_stage_to_main_pr_merge_commit_reuses_the_stage_run(promotion, monkeypatch):
    # The PR checkout is a merge commit; only its git tree matters.
    api, push = green_api(promotion["stage"])
    monkeypatch.setattr(scope, "select", lambda root, name, event: {"windows": True, "reason": "product"})
    result = scope.decide(promotion["root"], "pull_request", {}, ref="refs/pull/7/merge",
                          workflow="ci.yml", repo=REPO, api=api, run_id="999")
    assert result["windows"] is False and result["reused_from"] == push["html_url"]


def test_installer_classify_reuses_the_same_way(promotion):
    api, push = green_api(promotion["stage"], "windows-installer.yml")
    result = decide(promotion, api, workflow="windows-installer.yml")
    assert result["windows"] is False and result["reused_from"] == push["html_url"]
    assert all("/workflows/windows-installer.yml/" in c for c in api.calls if "/jobs?" not in c)


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
    lambda jobs: jobs[-1].update(conclusion="skipped"),                   # ACL verification
    lambda jobs: jobs[1].update(run_id=1),                                # a job of another run
    lambda jobs: jobs.append(dict(jobs[1], conclusion="failure")),        # duplicate name failed
])
def test_partial_cancelled_or_failed_matrix_is_refused(promotion, mutate):
    api, push = green_api(promotion["stage"])
    mutate(api.jobs_by_run[push["id"]]["jobs"])
    assert decide(promotion, api)["windows"] is True


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
    monkeypatch.setattr(scope, "select", lambda root, name, event: {"windows": False, "reason": "docs"})
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
    assert (out / "o").read_text(encoding="utf-8") == f"windows=false\nreused_from={push['html_url']}\n"
    assert (out / "s").read_text(encoding="utf-8").strip() == (
        f"Reused full results of run {push['html_url']} (same tree {promotion['tree']})")


def test_cli_without_token_runs_in_full(monkeypatch, tmp_path):
    for key in ("GITHUB_TOKEN", "GITHUB_STEP_SUMMARY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "o"))
    monkeypatch.setattr("sys.argv", ["acl_ci_scope.py", "--event-name", "push", "--event-file", str(tmp_path / "x")])
    scope.main()
    assert (tmp_path / "o").read_text(encoding="utf-8") == "windows=true\nreused_from=\n"


# --- the workflows are wired to it ----------------------------------------------------------------

def workflow(name):
    return yaml.safe_load((ROOT / ".github/workflows" / name).read_text(encoding="utf-8"))


def test_required_ci_jobs_match_the_real_test_matrix():
    ci = workflow("ci.yml")
    matrix = ci["jobs"]["test"]["strategy"]["matrix"]
    expected = {f"test ({p}, {s})" for p in matrix["python-version"] for s in matrix["shard"]}
    expected.add(ci["jobs"]["admission"]["name"])
    assert set(reuse.REQUIRED_JOBS["ci.yml"]) == expected


def test_required_installer_jobs_match_the_real_workflow():
    jobs = workflow("windows-installer.yml")["jobs"]
    assert set(reuse.REQUIRED_JOBS["windows-installer.yml"]) == {
        jobs["phase-1-isolation"]["name"], jobs["phase-2-packaging-spike"]["name"], jobs["admission"]["name"]}


@pytest.mark.parametrize("name,flag", [("ci.yml", "ci.yml"), ("windows-installer.yml", "windows-installer.yml")])
def test_scope_jobs_read_actions_and_publish_the_reused_run(name, flag):
    data = workflow(name)
    assert data["permissions"] == {"contents": "read", "actions": "read"}
    job = data["jobs"]["scope"]
    assert job["outputs"]["reused_from"] == "${{ steps.scope.outputs.reused_from }}"
    step = next(s for s in job["steps"] if s.get("id") == "scope")
    assert step["run"] == f"python3 scripts/acl_ci_scope.py --workflow {flag}"
    assert step["env"]["GITHUB_TOKEN"] == "${{ github.token }}"
    # Release tags and manual runs never get here as a reuse (classify keeps them full).
    triggers = data[True]
    assert "tags" not in (triggers.get("push") or {})
