"""ACL-1649: the PR -> stage -> main chain reuses what is provably already green.

A temporary git repository holds the three shapes GitHub produces: the PR head H, the squash commit
S on stage (same tree as H, parent P is inside H's history) and the merge commit M on main (same tree).
A fake Actions API answers like GitHub: runs by head_sha, jobs by run id, and the pull requests
that a commit closed. No test touches the network.
"""
import json
import subprocess

import pytest

from scripts import acl_ci_reuse as reuse
from scripts import acl_ci_scope as scope

REPO = "MojoTheApe/adopy-campaing-launcher"
URL = "https://github.com/" + REPO + "/actions/runs/{}"
BASE_JOBS = [f"linux tests (3.13, {s})" for s in (1, 2, 3)] + ["windows tests (3.13)", "ACL verification"]
PY311_JOBS = [f"linux tests (3.11, {s})" for s in (1, 2, 3)]


def git(root, *args):
    return subprocess.check_output(["git", *args], cwd=root, text=True).strip()


@pytest.fixture(scope="module")
def chain(tmp_path_factory):
    """Один репозиторий на модуль: каждый вызов git на Windows стоит секунды. Тесты только добавляют коммиты.

    B0 (main) -> P (stage tip) -> H (PR head); S = squash of H on stage; M = merge of S into main."""
    tmp_path = tmp_path_factory.mktemp("chain")
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.name", "CI fixture")
    git(tmp_path, "config", "user.email", "ci@example.invalid")
    (tmp_path / "app.py").write_text("# v0\n")
    git(tmp_path, "add", "app.py"); git(tmp_path, "commit", "-qm", "base")
    b0 = git(tmp_path, "rev-parse", "HEAD")
    git(tmp_path, "checkout", "-qb", "stage")
    (tmp_path / "app.py").write_text("# v1\n")
    git(tmp_path, "commit", "-qam", "stage tip")
    p = git(tmp_path, "rev-parse", "HEAD")
    git(tmp_path, "checkout", "-qb", "pr")
    (tmp_path / "app.py").write_text("# v2\n")
    git(tmp_path, "commit", "-qam", "the change")
    h = git(tmp_path, "rev-parse", "HEAD")
    tree = git(tmp_path, "rev-parse", "HEAD^{tree}")
    git(tmp_path, "checkout", "-q", "stage")
    s = git(tmp_path, "commit-tree", tree, "-p", p, "-m", "squash")
    git(tmp_path, "reset", "-q", "--hard", s)          # fixture-only: the temporary repository
    git(tmp_path, "checkout", "-q", "main")
    m = git(tmp_path, "commit-tree", tree, "-p", b0, "-p", s, "-m", "promote")
    mp = git(tmp_path, "commit-tree", tree, "-p", b0, "-p", s, "-m", "pull request merge ref")
    git(tmp_path, "reset", "-q", "--hard", m)
    git(tmp_path, "branch", "-D", "pr")                # the PR branch is not in a CI clone
    return {"root": tmp_path, "b0": b0, "p": p, "h": h, "s": s, "m": m, "mp": mp, "tree": tree}


def job(name, run_id, conclusion="success"):
    return {"name": name, "run_id": run_id, "status": "completed", "conclusion": conclusion}


class Api:
    """runs: {head_sha: [run]}; jobs: {run_id: [job]}; pulls: {commit: [pull request]}."""

    def __init__(self, runs=None, jobs=None, pulls=None, pulls_error=None):
        self.runs, self.jobs, self.pulls, self.pulls_error, self.calls = runs or {}, jobs or {}, pulls or {}, pulls_error, []

    def __call__(self, path):
        self.calls.append(path)
        if "/pulls" in path:
            if self.pulls_error:
                raise self.pulls_error
            return self.pulls.get(path.split("/commits/")[1].split("/")[0], [])
        if "/jobs?" in path:
            return {"jobs": self.jobs.get(int(path.split("/runs/")[1].split("/")[0]), [])}
        return {"workflow_runs": self.runs.get(path.split("head_sha=")[1].split("&")[0], [])}


def run(run_id, sha, event, branch, workflow="ci.yml"):
    return {"id": run_id, "head_sha": sha, "event": event, "head_branch": branch, "status": "completed",
            "conclusion": "success", "path": f".github/workflows/{workflow}", "html_url": URL.format(run_id),
            "repository": {"full_name": REPO}, "head_repository": {"full_name": REPO}}


def pull(number, head, merge, base="stage", merged=True):
    return {"number": number, "merged_at": "2026-10-06T10:00:00Z" if merged else None, "merge_commit_sha": merge,
            "base": {"ref": base, "repo": {"full_name": REPO}},
            "head": {"sha": head, "repo": {"full_name": REPO}}}


def no_fetch(root, number):
    raise AssertionError("the PR head is already in the clone")


def checkout(chain, commit):
    git(chain["root"], "checkout", "-q", "--detach", commit)


def decide(chain, api, commit, event, ref, payload=None, workflow="ci.yml", fetch=no_fetch):
    checkout(chain, commit)
    return scope.decide(chain["root"], event, payload or {}, ref=ref, workflow=workflow, repo=REPO, api=api,
                        run_id="999", fetch=fetch)


def pr_run_api(chain, with_main_pr_run=False, installer=False):
    """The PR run of H (3.13 + Windows) and, optionally, the later stage -> main PR run (3.11)."""
    runs = {chain["h"]: [run(100, chain["h"], "pull_request", "codex/x")]}
    jobs = {100: [job(n, 100) for n in BASE_JOBS]}
    pulls = {chain["s"]: [pull(7, chain["h"], chain["s"])]}
    if with_main_pr_run:
        # The stage -> main PR: head is the stage tip S; it reused the PR run and ran 3.11 only.
        runs[chain["s"]] = [run(200, chain["s"], "pull_request", "stage")]
        jobs[200] = [job(n, 200) for n in PY311_JOBS] + [job("ACL verification", 200)]
        pulls[chain["m"]] = [pull(8, chain["s"], chain["m"], base="main")]
    return Api(runs, jobs, pulls)


def stage_event(chain):
    return {"before": chain["p"], "after": chain["s"]}


# --- the stage push reuses the PR's own run (the case that never fired before) -----------------------

def test_stage_push_reuses_the_green_pr_run_of_the_same_tree(chain):
    api = pr_run_api(chain)
    result = decide(chain, api, chain["s"], "push", "refs/heads/stage", stage_event(chain))
    assert result["windows"] is False and result["linux"] is False
    assert result["reused_from"] == URL.format(100) and result["reason"] == "reused-green-full-run-of-same-tree"


def test_the_stage_push_never_asks_for_python_3_11(chain):
    # Only 3.13 is needed on stage, so a run of 3.13 + Windows already covers everything.
    api = pr_run_api(chain)
    assert scope.select(chain["root"], "push", stage_event(chain), ref="refs/heads/stage")["python"] == ["3.13"]


@pytest.mark.parametrize("break_it", [
    lambda c, api: api.pulls.update({c["s"]: [pull(7, c["h"], c["m"])]}),              # PR closed another commit
    lambda c, api: api.pulls.update({c["s"]: [pull(7, c["h"], c["s"], merged=False)]}),
    lambda c, api: api.pulls.update({c["s"]: []}),                                     # no PR closed this commit
    lambda c, api: api.runs[c["h"]][0].update(conclusion="failure"),
    lambda c, api: api.runs[c["h"]][0].update(path=".github/workflows/release.yml"),
    lambda c, api: api.runs[c["h"]][0].update(head_repository={"full_name": "someone/fork"}),
    lambda c, api: api.runs[c["h"]][0].update(html_url="https://github.com/someone/fork/actions/runs/100"),
])
def test_a_pr_run_that_git_does_not_tie_to_the_commit_is_refused(chain, break_it):
    api = pr_run_api(chain)
    break_it(chain, api)
    result = decide(chain, api, chain["s"], "push", "refs/heads/stage", stage_event(chain))
    assert result["windows"] is True and result["reuse"] == "no-proven-full-run"


def test_a_pr_run_is_never_taken_for_a_commit_without_a_verified_head(chain):
    # Without pr_heads (the old behaviour) a pull_request run is no proof, even for the same sha.
    api = pr_run_api(chain)
    api.runs[chain["s"]] = [run(100, chain["s"], "pull_request", "stage")]
    assert reuse.find_coverage(chain["root"], api, REPO, "ci.yml", chain["tree"])["runs"] == []


def test_the_previous_branch_tip_must_be_inside_the_pr_history(chain):
    # H was branched from an older tip: the PR run merged a different base, its tree is not provable.
    git(chain["root"], "checkout", "-q", "--detach", chain["b0"])
    (chain["root"] / "app.py").write_text("# v2\n")
    git(chain["root"], "commit", "-qam", "same content, older base")
    old = git(chain["root"], "rev-parse", "HEAD")
    assert git(chain["root"], "rev-parse", f"{old}^{{tree}}") == chain["tree"]
    api = pr_run_api(chain)
    api.runs = {old: api.runs[chain["h"]]}
    api.runs[old][0]["head_sha"] = old
    api.pulls = {chain["s"]: [pull(7, old, chain["s"])]}
    assert reuse.verify_pr_heads(chain["root"], api, REPO, chain["s"], "stage", no_fetch) == []
    result = decide(chain, api, chain["s"], "push", "refs/heads/stage", stage_event(chain))
    assert result["windows"] is True and result["reuse"] == "no-proven-full-run"


def test_a_pr_head_missing_from_the_clone_is_fetched_and_a_failed_fetch_means_no_proof(chain):
    ghost = "a" * 40                      # an object the clone does not have
    api = pr_run_api(chain)
    api.pulls = {chain["s"]: [pull(7, ghost, chain["s"])]}
    calls = []
    def failing(root, number):
        calls.append(number)
        return False
    assert reuse.verify_pr_heads(chain["root"], api, REPO, chain["s"], "stage", failing) == [] and calls == [7]
    assert reuse.verify_pr_heads(chain["root"], Api(pulls_error=OSError("HTTP 500")), REPO, chain["s"], "stage", failing) == []


def test_a_pr_with_a_different_tree_is_never_a_proof(chain):
    git(chain["root"], "checkout", "-q", "--detach", chain["p"])
    (chain["root"] / "app.py").write_text("# other\n")
    git(chain["root"], "commit", "-qam", "other tree")
    other = git(chain["root"], "rev-parse", "HEAD")
    api = pr_run_api(chain)
    api.pulls = {chain["s"]: [pull(7, other, chain["s"])]}
    api.runs = {other: [run(100, other, "pull_request", "codex/x")]}
    assert reuse.verify_pr_heads(chain["root"], api, REPO, chain["s"], "stage", no_fetch) == []


# --- stage -> main: only what stage did not cover is run ----------------------------------------------

def test_main_pr_reuses_the_pr_run_and_runs_only_python_3_11(chain):
    api = pr_run_api(chain)
    # The checkout of the main PR is a merge commit with the stage tree; its head, the stage tip S, closed PR 7.
    result = decide(chain, api, chain["mp"], "pull_request", "refs/pull/8/merge",
                    {"pull_request": {"base": {"ref": "main", "sha": chain["b0"]}, "head": {"sha": chain["s"]}}})
    assert result["windows"] is False and result["linux"] is True
    assert result["python"] == ["3.11"] and result["reason"] == "partly-reused-green-runs-of-same-tree"
    assert result["reused_from"] == URL.format(100)


def test_main_push_needs_nothing_when_stage_and_the_main_pr_covered_the_full_set(chain):
    api = pr_run_api(chain, with_main_pr_run=True)
    result = decide(chain, api, chain["m"], "push", "refs/heads/main", {"before": chain["b0"], "after": chain["m"]})
    assert result["windows"] is False and result["linux"] is False
    assert result["reused_runs"] == [URL.format(100), URL.format(200)] or sorted(result["reused_runs"]) == [URL.format(100), URL.format(200)]


def test_main_push_without_the_main_pr_run_still_runs_python_3_11(chain):
    api = pr_run_api(chain)
    result = decide(chain, api, chain["m"], "push", "refs/heads/main", {"before": chain["b0"], "after": chain["m"]})
    assert result["python"] == ["3.11"] and result["linux"] is True and result["windows"] is False


def test_main_push_with_no_earlier_run_runs_everything(chain):
    result = decide(chain, Api(), chain["m"], "push", "refs/heads/main", {"before": chain["b0"], "after": chain["m"]})
    assert result["windows"] is True and result["linux"] is True and result["python"] == ["3.11", "3.13"]
    assert result["reuse"] == "no-proven-full-run"


def test_a_failed_windows_job_in_the_pr_run_is_not_covered(chain):
    api = pr_run_api(chain)
    for j in api.jobs[100]:
        if j["name"] == "windows tests (3.13)":
            j["conclusion"] = "failure"
    api.runs[chain["h"]][0]["conclusion"] = "failure"          # a run with a failed job is not green
    result = decide(chain, api, chain["s"], "push", "refs/heads/stage", stage_event(chain))
    assert result["windows"] is True and result["linux"] is True and result["reuse"] == "no-proven-full-run"


def test_a_skipped_job_of_a_green_run_is_never_covered(chain):
    api = pr_run_api(chain)
    for j in api.jobs[100]:
        if j["name"] == "linux tests (3.13, 2)":
            j["conclusion"] = "skipped"
    result = decide(chain, api, chain["s"], "push", "refs/heads/stage", stage_event(chain))
    assert result["windows"] is False and result["linux"] is True        # only the missing shard's version reruns
    assert result["python"] == ["3.13"] and result["reason"] == "partly-reused-green-runs-of-same-tree"


# --- release gate: the full set can be gathered from several runs, 3.11 included ------------------------

def test_the_full_set_is_gathered_across_runs_for_the_release_gate(chain):
    checkout(chain, chain["m"])
    api = pr_run_api(chain, with_main_pr_run=True)
    heads = reuse.collect_pr_heads(chain["root"], api, REPO, chain["tree"], [chain["m"]], no_fetch)
    assert chain["h"] in heads and chain["s"] in heads
    proof = reuse.find_reuse(chain["root"], api, REPO, "ci.yml", chain["tree"], pr_heads=heads)
    assert proof and {r["run_id"] for r in proof["runs"]} == {100, 200}
    covered = reuse.find_coverage(chain["root"], api, REPO, "ci.yml", chain["tree"], pr_heads=heads)["covered"]
    assert set(reuse.REQUIRED_JOBS["ci.yml"]) <= covered


def test_the_release_gate_refuses_when_python_3_11_never_ran(chain):
    checkout(chain, chain["m"])
    api = pr_run_api(chain)                                  # 3.13 + Windows only
    heads = reuse.collect_pr_heads(chain["root"], api, REPO, chain["tree"], [chain["m"]], no_fetch)
    assert reuse.find_reuse(chain["root"], api, REPO, "ci.yml", chain["tree"], pr_heads=heads) is None


def test_the_release_gate_refuses_without_windows(chain):
    checkout(chain, chain["m"])
    api = pr_run_api(chain, with_main_pr_run=True)
    api.jobs[100] = [j for j in api.jobs[100] if not j["name"].startswith("windows")]
    heads = reuse.collect_pr_heads(chain["root"], api, REPO, chain["tree"], [chain["m"]], no_fetch)
    assert reuse.find_reuse(chain["root"], api, REPO, "ci.yml", chain["tree"], pr_heads=heads) is None


# --- the installer workflow uses the same chain ---------------------------------------------------------

def test_installer_runs_once_on_the_pr_and_is_reused_afterwards(chain):
    installer = ["Verify build inputs", "Build, install, repair, and uninstall", "Installer verification"]
    runs = {chain["h"]: [run(300, chain["h"], "pull_request", "codex/x", "windows-installer.yml")]}
    api = Api(runs, {300: [job(n, 300) for n in installer]}, {chain["s"]: [pull(7, chain["h"], chain["s"])]})
    result = decide(chain, api, chain["s"], "push", "refs/heads/stage", stage_event(chain), "windows-installer.yml")
    # The squash touches app.py only: the installer is not even needed on stage ...
    assert result["windows"] is False
    # ... and on the path to main it is needed, and the PR's run of it is reused.
    result = decide(chain, api, chain["m"], "push", "refs/heads/main", {"before": chain["b0"], "after": chain["m"]},
                    "windows-installer.yml")
    assert result["windows"] is False and result["reused_from"] == URL.format(300)


def test_installer_on_the_main_path_runs_when_no_earlier_run_covers_it(chain):
    result = decide(chain, Api(), chain["m"], "push", "refs/heads/main", {"before": chain["b0"], "after": chain["m"]},
                    "windows-installer.yml")
    assert result["windows"] is True and result["reuse"] == "no-proven-full-run"


# --- the CLI writes the new outputs -----------------------------------------------------------------------

def test_cli_writes_linux_and_python_for_a_partial_reuse(chain, monkeypatch, tmp_path_factory):
    api = pr_run_api(chain)
    out = tmp_path_factory.mktemp("gh")
    checkout(chain, chain["mp"])
    real_decide = scope.decide
    event_file = out / "event.json"
    event_file.write_text(json.dumps(
        {"pull_request": {"base": {"ref": "main", "sha": chain["b0"]}, "head": {"sha": chain["s"]}}}), encoding="utf-8")
    monkeypatch.setattr(scope.reuse, "github_api", lambda token, base: api)
    monkeypatch.setattr(scope, "decide", lambda root, *a, **k: real_decide(chain["root"], *a, fetch=no_fetch, **k))
    for key, value in {"GITHUB_TOKEN": "t", "GITHUB_REPOSITORY": REPO, "GITHUB_RUN_ID": "999",
                       "GITHUB_REF": "refs/pull/8/merge", "GITHUB_OUTPUT": str(out / "o"),
                       "GITHUB_STEP_SUMMARY": str(out / "s")}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr("sys.argv", ["acl_ci_scope.py", "--event-name", "pull_request", "--event-file", str(event_file)])
    scope.main()
    assert (out / "o").read_text(encoding="utf-8") == (
        f"windows=false\nlinux=true\nreused_from={URL.format(100)}\npython=[\"3.11\"]\n")
    assert f"Reused passed jobs of the same tree from run {URL.format(100)}" in (out / "s").read_text(encoding="utf-8")
