"""ACL-1649: the PR -> stage -> main chain reuses what is provably already green.

A temporary git repository holds the shapes GitHub produces: the PR head H, the squash commit S on
stage (same tree as H) and the merge commit M on main. M has the REAL main shape: its first parent is
the previous promotion (a merge commit that is NOT in stage history), second parent is S.
A fake Actions API answers like GitHub: runs by head_sha, jobs by run id, annotations by job id and the
pull requests that a commit closed. A pull_request run counts only with the annotation its own scope job
wrote about the tree it tested (run.pull_requests is empty after a merge, so it cannot be the binding).
No test touches the network.
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
SCOPE_JOB = reuse.SCOPE_JOB["ci.yml"]


def git(root, *args):
    return subprocess.check_output(["git", *args], cwd=root, text=True).strip()


@pytest.fixture(scope="module")
def chain(tmp_path_factory):
    """Один репозиторий на модуль: каждый вызов git на Windows стоит секунды. Тесты только добавляют коммиты.

    B0 (main) -> P (stage tip); M0 = previous promotion on main (parents B0, P, outside later stage history);
    H = PR head; S = squash of H on stage; M = merge of S into main with first parent M0."""
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
    p_tree = git(tmp_path, "rev-parse", "HEAD^{tree}")
    m0 = git(tmp_path, "commit-tree", p_tree, "-p", b0, "-p", p, "-m", "Merge pull request #1 from stage")
    git(tmp_path, "checkout", "-qb", "pr")
    (tmp_path / "app.py").write_text("# v2\n")
    git(tmp_path, "commit", "-qam", "the change")
    h = git(tmp_path, "rev-parse", "HEAD")
    tree = git(tmp_path, "rev-parse", "HEAD^{tree}")
    git(tmp_path, "checkout", "-q", "stage")
    s = git(tmp_path, "commit-tree", tree, "-p", p, "-m", "squash")
    git(tmp_path, "reset", "-q", "--hard", s)          # fixture-only: the temporary repository
    git(tmp_path, "checkout", "-q", "main")
    git(tmp_path, "reset", "-q", "--hard", m0)
    m = git(tmp_path, "commit-tree", tree, "-p", m0, "-p", s, "-m", "Merge pull request #8 from stage")
    mp = git(tmp_path, "commit-tree", tree, "-p", m0, "-p", s, "-m", "pull request merge ref")
    git(tmp_path, "reset", "-q", "--hard", m)
    git(tmp_path, "branch", "-D", "pr")                # the PR branch is not in a CI clone
    assert subprocess.call(["git", "merge-base", "--is-ancestor", m0, s], cwd=tmp_path) != 0   # the real shape
    return {"root": tmp_path, "b0": b0, "p": p, "m0": m0, "h": h, "s": s, "m": m, "mp": mp, "tree": tree}


def job(name, run_id, conclusion="success", job_id=None):
    row = {"name": name, "run_id": run_id, "status": "completed", "conclusion": conclusion}
    if job_id is not None:
        row["id"] = job_id
    return row


def scope_job(run_id):
    return job(SCOPE_JOB, run_id, job_id=run_id * 10)


class Api:
    """runs: {head_sha: [run]}; jobs: {run_id: [job]}; pulls: {commit: [PR]}; notes: {job_id: [annotation]}."""

    def __init__(self, runs=None, jobs=None, pulls=None, notes=None, pulls_error=None):
        self.runs, self.jobs, self.pulls, self.notes = runs or {}, jobs or {}, pulls or {}, notes or {}
        self.pulls_error, self.calls = pulls_error, []

    def __call__(self, path):
        self.calls.append(path)
        if "/annotations" in path:
            return self.notes.get(int(path.split("/check-runs/")[1].split("/")[0]), [])
        if "/pulls" in path:
            if self.pulls_error:
                raise self.pulls_error
            return self.pulls.get(path.split("/commits/")[1].split("/")[0], [])
        if "/jobs?" in path:
            return {"jobs": self.jobs.get(int(path.split("/runs/")[1].split("/")[0]), [])}
        return {"workflow_runs": self.runs.get(path.split("head_sha=")[1].split("&")[0], [])}


def note(tree, title=reuse.MARKER_TITLE, message=None):
    return {"title": title, "message": message if message is not None else f"tree={tree}", "annotation_level": "notice"}


def run(run_id, sha, event, branch, workflow="ci.yml"):
    return {"id": run_id, "head_sha": sha, "event": event, "head_branch": branch, "status": "completed",
            "conclusion": "success", "path": f".github/workflows/{workflow}", "html_url": URL.format(run_id),
            "repository": {"full_name": REPO}, "head_repository": {"full_name": REPO}}


def pull(number, head, merge, base="stage", merged=True):
    return {"number": number, "merged_at": "2026-10-06T10:00:00Z" if merged else None, "merge_commit_sha": merge,
            "base": {"ref": base, "repo": {"full_name": REPO}},
            "head": {"sha": head, "repo": {"full_name": REPO}}}


def checkout(chain, commit):
    git(chain["root"], "checkout", "-q", "--detach", commit)


def decide(chain, api, commit, event, ref, payload=None, workflow="ci.yml"):
    checkout(chain, commit)
    return scope.decide(chain["root"], event, payload or {}, ref=ref, workflow=workflow, repo=REPO, api=api,
                        run_id="999")


def pr_run_api(chain, with_main_pr_run=False):
    """The PR run of H (3.13 + Windows) and, optionally, the later stage -> main PR run (3.11).

    Every pull_request run carries the annotation of the tree its scope job saw."""
    runs = {chain["h"]: [run(100, chain["h"], "pull_request", "codex/x")]}
    jobs = {100: [scope_job(100)] + [job(n, 100) for n in BASE_JOBS]}
    notes = {1000: [note(chain["tree"])]}
    pulls = {chain["s"]: [pull(7, chain["h"], chain["s"])]}
    if with_main_pr_run:
        # The stage -> main PR: head is the stage tip S; it reused the PR run and ran 3.11 only.
        runs[chain["s"]] = [run(200, chain["s"], "pull_request", "stage")]
        jobs[200] = [scope_job(200)] + [job(n, 200) for n in PY311_JOBS] + [job("ACL verification", 200)]
        notes[2000] = [note(chain["tree"])]
        pulls[chain["m"]] = [pull(8, chain["s"], chain["m"], base="main")]
    return Api(runs, jobs, pulls, notes)


def stage_event(chain):
    return {"before": chain["p"], "after": chain["s"]}


def main_event(chain):
    return {"before": chain["m0"], "after": chain["m"]}


# --- the stage push reuses the PR's own run (the case that never fired before) -----------------------

def test_stage_push_reuses_the_green_pr_run_of_the_same_tree(chain):
    api = pr_run_api(chain)
    result = decide(chain, api, chain["s"], "push", "refs/heads/stage", stage_event(chain))
    assert result["windows"] is False and result["linux"] is False
    assert result["reused_from"] == URL.format(100) and result["reason"] == "reused-green-full-run-of-same-tree"


def test_the_stage_push_never_asks_for_python_3_11(chain):
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


# --- B3: a PR run counts only with the annotation about the tree it tested -------------------------------

@pytest.mark.parametrize("make_notes", [
    lambda t: [],                                             # an unrelated run on the same head: no marker
    lambda t: [note("a" * 40)],                               # retargeted PR: the old base tested another tree
    lambda t: [note(None, message="tree=not-a-sha")],
    lambda t: [note(None, message="")],
    lambda t: [note(None, title="something-else")],
    lambda t: [note(None, message="tree=" + "a" * 40 + " extra")],
    lambda t: [note("a" * 40), note("b" * 40)],               # two different claims are ambiguous
    lambda t: [note(t), note("a" * 40)],                      # the right tree next to another one: ambiguous
    lambda t: [note("a" * 40), note(t)],
    lambda t: [note(t), note(None, message="tree=zzz")],      # the right tree next to a malformed marker
    lambda t: [note(None, message="oops"), note(t)],
])
def test_a_pr_run_without_a_matching_tree_marker_is_refused(chain, make_notes):
    api = pr_run_api(chain)
    api.notes[1000] = make_notes(chain["tree"])
    result = decide(chain, api, chain["s"], "push", "refs/heads/stage", stage_event(chain))
    assert result["windows"] is True and result["reuse"] == "no-proven-full-run"


def test_an_unrelated_pr_run_on_the_same_head_adds_no_coverage(chain):
    # PR #9 from the same branch into main: its run tested merge(main, H), whose tree differs.
    api = pr_run_api(chain)
    api.runs[chain["h"]].append(run(300, chain["h"], "pull_request", "codex/x"))
    api.jobs[300] = [scope_job(300)] + [job(n, 300) for n in PY311_JOBS] + [job("ACL verification", 300)]
    api.notes[3000] = [note("c" * 40)]
    checkout(chain, chain["s"])
    heads = reuse.collect_pr_heads(chain["root"], api, REPO, chain["tree"], [chain["s"]])
    cov = reuse.find_coverage(chain["root"], api, REPO, "ci.yml", chain["tree"], pr_heads=heads)
    assert [r["run_id"] for r in cov["runs"]] == [100]
    assert not set(reuse.REQUIRED_JOBS["ci.yml"]) <= cov["covered"]


def test_two_different_markers_on_one_job_are_ambiguous_never_picked(chain):
    # Strict: whatever order or set iteration gives, a run that claims two trees proves neither.
    for notes in ([note(chain["tree"]), note("a" * 40)], [note("a" * 40), note(chain["tree"])],
                  [note("a" * 40), note("b" * 40)], [note(chain["tree"]), note(None, message="tree=zzz")]):
        api = pr_run_api(chain)
        api.notes[1000] = notes
        assert reuse.tested_tree(api, REPO, reuse.latest_jobs(api, REPO, {"id": 100}), "ci.yml") is None
    api = pr_run_api(chain)
    api.notes[1000] = [note(chain["tree"]), note(chain["tree"])]          # the same claim twice is still one claim
    assert reuse.tested_tree(api, REPO, reuse.latest_jobs(api, REPO, {"id": 100}), "ci.yml") == chain["tree"]


def test_a_marker_on_a_run_with_two_scope_jobs_is_ambiguous(chain):
    api = pr_run_api(chain)
    api.jobs[100].append(job(SCOPE_JOB, 100, job_id=1001))
    assert reuse.tested_tree(api, REPO, reuse.latest_jobs(api, REPO, {"id": 100}), "ci.yml") is None


def test_the_marker_is_read_from_the_scope_job_of_the_current_layout(chain):
    api = pr_run_api(chain)
    jobs = reuse.latest_jobs(api, REPO, {"id": 100})
    assert reuse.tested_tree(api, REPO, jobs, "ci.yml") == chain["tree"]
    assert reuse.tested_tree(api, REPO, {}, "ci.yml") is None
    assert reuse.tested_tree(api, REPO, jobs, "release.yml") is None


def test_a_pr_run_is_never_taken_for_a_commit_without_a_verified_head(chain):
    # Without pr_heads a pull_request run is no proof, even for the same sha and with a marker.
    api = pr_run_api(chain)
    api.runs[chain["s"]] = [run(100, chain["s"], "pull_request", "stage")]
    assert reuse.find_coverage(chain["root"], api, REPO, "ci.yml", chain["tree"])["runs"] == []


# --- R2-B1: only pull_request runs are taken from a PR head, and only with the marker -------------------

FULL_JOBS = [f"linux tests ({p}, {k})" for p in ("3.11", "3.13") for k in (1, 2, 3)] + [
    "windows tests (3.13)", "ACL verification"]


@pytest.mark.parametrize("event", ["workflow_dispatch", "push"])
def test_a_push_or_dispatch_run_on_a_pr_head_with_another_tree_is_no_proof(chain, event):
    # H tested tree(H) (full set, no marker). The squash S2 landed on a stage that had moved, so its tree
    # differs from tree(H); H is still the head of the PR that S2 closed.
    git(chain["root"], "checkout", "-q", "--detach", chain["p"])
    (chain["root"] / "other.py").write_text("another task landed first\n")
    git(chain["root"], "add", "other.py"); git(chain["root"], "commit", "-qm", "other")
    moved = git(chain["root"], "rev-parse", "HEAD")
    h_tree = git(chain["root"], "rev-parse", f"{chain['h']}^{{tree}}")
    s2 = git(chain["root"], "commit-tree", git(chain["root"], "rev-parse", f"{moved}^{{tree}}"), "-p", moved, "-m", "squash 2")
    tree2 = git(chain["root"], "rev-parse", f"{s2}^{{tree}}")
    assert tree2 != h_tree
    api = Api(runs={chain["h"]: [run(500, chain["h"], event, "codex/x")]},
              jobs={500: [scope_job(500)] + [job(n, 500) for n in FULL_JOBS]},
              pulls={s2: [pull(7, chain["h"], s2)]})
    checkout(chain, s2)
    heads = reuse.collect_pr_heads(chain["root"], api, REPO, tree2, [s2])
    assert chain["h"] in heads
    assert reuse.find_coverage(chain["root"], api, REPO, "ci.yml", tree2, pr_heads=heads)["runs"] == []
    assert reuse.find_reuse(chain["root"], api, REPO, "ci.yml", tree2, pr_heads=heads) is None


def test_the_stage_push_run_does_not_count_for_a_main_commit_with_a_hotfix(chain):
    # main got a hotfix: tree(M2) != tree(S). The PR into main has head S, so S is a verified PR head,
    # but a push run on S proves tree(S), not tree(M2).
    git(chain["root"], "checkout", "-q", "--detach", chain["m"])
    (chain["root"] / "hotfix.py").write_text("hotfix on main\n")
    git(chain["root"], "add", "hotfix.py"); git(chain["root"], "commit", "-qm", "hotfix")
    hot = git(chain["root"], "rev-parse", "HEAD")
    m2 = git(chain["root"], "commit-tree", git(chain["root"], "rev-parse", f"{hot}^{{tree}}"), "-p", hot, "-p", chain["s"],
             "-m", "Merge pull request #9 from stage")
    tree2 = git(chain["root"], "rev-parse", f"{m2}^{{tree}}")
    assert tree2 != chain["tree"]
    api = Api(runs={chain["s"]: [run(600, chain["s"], "push", "stage")]},
              jobs={600: [job(n, 600) for n in FULL_JOBS]},
              pulls={m2: [pull(9, chain["s"], m2, base="main")]})
    checkout(chain, m2)
    heads = reuse.collect_pr_heads(chain["root"], api, REPO, tree2, [m2])
    assert chain["s"] in heads
    assert reuse.find_coverage(chain["root"], api, REPO, "ci.yml", tree2, pr_heads=heads)["runs"] == []
    result = scope.decide(chain["root"], "push", {"before": hot, "after": m2}, ref="refs/heads/main", workflow="ci.yml",
                          repo=REPO, api=api, run_id="999")
    assert result["windows"] is True and result["linux"] is True and result["reuse"] == "no-proven-full-run"


def test_the_pr_api_failing_means_no_pr_proof(chain):
    api = Api(pulls_error=OSError("HTTP 500"))
    assert reuse.verify_pr_heads(chain["root"], api, REPO, chain["s"], "stage") == []


def test_the_scope_step_prints_the_tested_tree_marker_for_pr_runs(chain, monkeypatch, tmp_path, capsys):
    checkout(chain, chain["mp"])
    monkeypatch.setattr(scope, "decide", lambda root, *a, **k: {"windows": True, "linux": True, "reason": "x",
                                                                "python": ["3.13"]})
    monkeypatch.setattr(scope.reuse, "tree_of", lambda root, rev: chain["tree"])
    for key in ("GITHUB_TOKEN", "GITHUB_STEP_SUMMARY", "GITHUB_OUTPUT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr("sys.argv", ["acl_ci_scope.py", "--event-name", "pull_request", "--event-file", str(tmp_path / "x")])
    scope.main()
    assert f"::notice title=acl-tested-tree::tree={chain['tree']}" in capsys.readouterr().out
    monkeypatch.setattr("sys.argv", ["acl_ci_scope.py", "--event-name", "push", "--event-file", str(tmp_path / "x")])
    scope.main()
    assert "acl-tested-tree" not in capsys.readouterr().out


# --- stage -> main: only what stage did not cover is run ----------------------------------------------

def test_main_pr_reuses_the_pr_run_and_runs_only_python_3_11(chain):
    api = pr_run_api(chain)
    result = decide(chain, api, chain["mp"], "pull_request", "refs/pull/8/merge",
                    {"pull_request": {"base": {"ref": "main", "sha": chain["m0"]}, "head": {"sha": chain["s"]}}})
    assert result["windows"] is False and result["linux"] is True
    assert result["python"] == ["3.11"] and result["reason"] == "partly-reused-green-runs-of-same-tree"
    assert result["reused_from"] == URL.format(100)


def test_main_push_with_the_real_merge_shape_needs_nothing_when_the_full_set_was_covered(chain):
    # B2: the first parent of M is the previous promotion, outside stage history; the binding still works.
    api = pr_run_api(chain, with_main_pr_run=True)
    result = decide(chain, api, chain["m"], "push", "refs/heads/main", main_event(chain))
    assert result["windows"] is False and result["linux"] is False
    assert sorted(result["reused_runs"]) == [URL.format(100), URL.format(200)]


def test_main_push_without_the_main_pr_run_still_runs_python_3_11(chain):
    api = pr_run_api(chain)
    result = decide(chain, api, chain["m"], "push", "refs/heads/main", main_event(chain))
    assert result["python"] == ["3.11"] and result["linux"] is True and result["windows"] is False


def test_main_push_with_no_earlier_run_runs_everything(chain):
    result = decide(chain, Api(), chain["m"], "push", "refs/heads/main", main_event(chain))
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


# --- release gate: the full set is gathered across runs, the main run's own jobs included -----------------

def gate(chain, api, own_run=None):
    checkout(chain, chain["m"])
    heads = reuse.collect_pr_heads(chain["root"], api, REPO, chain["tree"], [chain["m"]])
    return reuse.find_reuse(chain["root"], api, REPO, "ci.yml", chain["tree"], pr_heads=heads)


def main_push_api(chain, own_jobs=PY311_JOBS):
    """Stage PR run (3.13 + Windows), the stage -> main PR run (nothing new) and the main push run itself."""
    api = pr_run_api(chain)
    api.runs[chain["m"]] = [run(999, chain["m"], "push", "main")]
    api.jobs[999] = [job(n, 999) for n in own_jobs] + [job("ACL verification", 999)]
    return api


def test_the_release_gate_counts_the_main_runs_own_python_3_11_jobs(chain):
    # B2: the main push ran only 3.11 (partial); the rest comes from the PR run. The gate must not exclude it.
    proof = gate(chain, main_push_api(chain))
    assert proof and {r["run_id"] for r in proof["runs"]} == {100, 999}
    assert chain["h"] in reuse.collect_pr_heads(chain["root"], main_push_api(chain), REPO, chain["tree"], [chain["m"]])


def test_the_full_set_is_gathered_across_runs_for_the_release_gate(chain):
    api = pr_run_api(chain, with_main_pr_run=True)
    proof = gate(chain, api)
    assert proof and {r["run_id"] for r in proof["runs"]} == {100, 200}
    covered = reuse.find_coverage(chain["root"], api, REPO, "ci.yml", chain["tree"],
                                  pr_heads=reuse.collect_pr_heads(chain["root"], api, REPO, chain["tree"], [chain["m"]]))["covered"]
    assert set(reuse.REQUIRED_JOBS["ci.yml"]) <= covered


def test_the_release_gate_refuses_when_python_3_11_never_ran(chain):
    assert gate(chain, pr_run_api(chain)) is None                      # 3.13 + Windows only


def test_the_release_gate_refuses_when_the_main_run_ran_only_some_3_11_shards(chain):
    assert gate(chain, main_push_api(chain, PY311_JOBS[:2])) is None


def test_the_release_gate_refuses_without_windows(chain):
    api = pr_run_api(chain, with_main_pr_run=True)
    api.jobs[100] = [j for j in api.jobs[100] if not j["name"].startswith("windows")]
    assert gate(chain, api) is None


def test_the_release_gate_refuses_a_pr_run_without_the_marker(chain):
    api = pr_run_api(chain, with_main_pr_run=True)
    api.notes[2000] = []                                               # the 3.11 run cannot be tied to the tree
    assert gate(chain, api) is None
    api = pr_run_api(chain, with_main_pr_run=True)
    api.notes[1000] = [note("d" * 40)]                                 # the 3.13 + Windows run tested another tree
    assert gate(chain, api) is None


# --- the installer workflow uses the same chain ---------------------------------------------------------

INSTALLER_JOBS = ["Verify build inputs", "Build, install, repair, and uninstall", "Installer verification"]


def installer_api(chain, jobs=INSTALLER_JOBS):
    sj = reuse.SCOPE_JOB["windows-installer.yml"]
    runs = {chain["h"]: [run(300, chain["h"], "pull_request", "codex/x", "windows-installer.yml")]}
    rows = [job(sj, 300, job_id=3000)] + [job(n, 300) for n in jobs]
    return Api(runs, {300: rows}, {chain["s"]: [pull(7, chain["h"], chain["s"])]}, {3000: [note(chain["tree"])]})


def test_installer_runs_once_on_the_pr_and_is_reused_afterwards(chain):
    api = installer_api(chain)
    result = decide(chain, api, chain["s"], "push", "refs/heads/stage", stage_event(chain), "windows-installer.yml")
    assert result["windows"] is False        # the squash touches app.py only: not needed on stage
    result = decide(chain, api, chain["m"], "push", "refs/heads/main", main_event(chain), "windows-installer.yml")
    assert result["windows"] is False and result["reused_from"] == URL.format(300)


def test_installer_run_that_skipped_the_build_is_no_reuse_source(chain):
    # N4: a stage run with the installer skipped still passes "Installer verification"; it built nothing.
    api = installer_api(chain, ["Installer verification"])
    for j in api.jobs[300]:
        if j["name"] != "Installer verification" and j["name"] != reuse.SCOPE_JOB["windows-installer.yml"]:
            j["conclusion"] = "skipped"
    result = decide(chain, api, chain["m"], "push", "refs/heads/main", main_event(chain), "windows-installer.yml")
    assert result["windows"] is True and result.get("reused_from") is None and result["reuse"] == "no-proven-full-run"


def test_installer_on_the_main_path_runs_when_no_earlier_run_covers_it(chain):
    result = decide(chain, Api(), chain["m"], "push", "refs/heads/main", main_event(chain), "windows-installer.yml")
    assert result["windows"] is True and result["reuse"] == "no-proven-full-run"


# --- the CLI writes the new outputs -----------------------------------------------------------------------

def test_cli_writes_linux_and_python_for_a_partial_reuse(chain, monkeypatch, tmp_path_factory):
    api = pr_run_api(chain)
    out = tmp_path_factory.mktemp("gh")
    checkout(chain, chain["mp"])
    real_decide = scope.decide
    event_file = out / "event.json"
    event_file.write_text(json.dumps(
        {"pull_request": {"base": {"ref": "main", "sha": chain["m0"]}, "head": {"sha": chain["s"]}}}), encoding="utf-8")
    monkeypatch.setattr(scope.reuse, "github_api", lambda token, base: api)
    monkeypatch.setattr(scope, "decide", lambda root, *a, **k: real_decide(chain["root"], *a, **k))
    for key, value in {"GITHUB_TOKEN": "t", "GITHUB_REPOSITORY": REPO, "GITHUB_RUN_ID": "999",
                       "GITHUB_REF": "refs/pull/8/merge", "GITHUB_OUTPUT": str(out / "o"),
                       "GITHUB_STEP_SUMMARY": str(out / "s")}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr("sys.argv", ["acl_ci_scope.py", "--event-name", "pull_request", "--event-file", str(event_file)])
    scope.main()
    assert (out / "o").read_text(encoding="utf-8") == (
        f"windows=false\nlinux=true\nreused_from={URL.format(100)}\npython=[\"3.11\"]\n")
    assert f"Reused passed jobs of the same tree from run {URL.format(100)}" in (out / "s").read_text(encoding="utf-8")
