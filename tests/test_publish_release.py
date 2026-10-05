import copy
import json
from pathlib import Path
import subprocess

import pytest

from scripts import acl_publish_release as pub

FULL_JOBS = json.loads((Path(__file__).with_name("fixtures") / "ci_reuse" / "ci_full_jobs.json").read_text(encoding="utf-8"))


def jobs_of(run_id, conclusion="success"):
    """Recorded full ci.yml jobs moved to ``run_id``; a reused run has its test jobs skipped."""
    jobs = copy.deepcopy(FULL_JOBS)
    for job in jobs["jobs"]:
        job["run_id"] = run_id
        if job["name"].startswith("test ("):
            job["conclusion"] = conclusion
    return jobs


@pytest.fixture
def promotion(monkeypatch):
    state = {"type": "tag", "head": "a" * 40, "main": "a" * 40,
             "note": "success https://github.com/test/source/actions/runs/42",
             "run": {"head_sha": "a" * 40, "status": "completed", "conclusion": "success",
                     "path": ".github/workflows/ci.yml", "head_branch": "main", "event": "push"}}
    def git(*args):
        if args[:2] == ("cat-file", "-t"): return state["type"]
        if args[:2] == ("cat-file", "-p"): return state["note"]
        if args == ("rev-parse", "origin/main"): return state["main"]
        if args == ("rev-parse", "HEAD"): return state["head"]
        return "a" * 40
    monkeypatch.setattr(pub, "_git", git)
    def gh(*args):
        if args[1].endswith("/runs/42/jobs?filter=latest&per_page=100"):
            return json.dumps(jobs_of(42))
        assert args == ("api", "repos/test/source/actions/runs/42")
        return json.dumps(state["run"])
    monkeypatch.setattr(pub, "_gh", gh)
    return state


def test_verified_main_promotion(promotion):
    assert pub.verify_promotion("acl-release-2026-09-19", "test/source") == "a" * 40


@pytest.mark.parametrize('branch', ['master', 'stage'])
def test_other_branch_success_cannot_publish_main_release(promotion, branch):
    promotion['run']['head_branch'] = branch
    with pytest.raises(ValueError, match='full main run'):
        pub.verify_promotion('acl-release-2026-09-19', 'test/source')


@pytest.mark.parametrize("key,value", [("type", "commit"), ("main", "b" * 40),
    ("head", "b" * 40), ("note", "success https://github.com/other/repo/actions/runs/42")])
def test_invalid_promotion_is_refused(promotion, key, value):
    promotion[key] = value
    with pytest.raises(ValueError): pub.verify_promotion("acl-release-2026-09-19", "test/source")


@pytest.mark.parametrize("key,value", [("head_sha", "b" * 40), ("conclusion", "failure"),
    ("status", "queued"), ("path", ".github/workflows/tracker.yml"),
    ("head_branch", "feature"), ("event", "pull_request")])
def test_unrelated_or_failed_ci_is_refused(promotion, key, value):
    promotion["run"][key] = value
    with pytest.raises(ValueError): pub.verify_promotion("acl-release-2026-09-19", "test/source")


def test_existing_release_is_never_modified(tmp_path, monkeypatch):
    calls = []
    def gh(*args):
        calls.append(args)
        return '[{"tagName":"acl-release-test"}]'
    monkeypatch.setattr(pub, "_gh", gh)
    with pytest.raises(ValueError, match="already exists"):
        pub.publish("acl-release-test", "test/releases", tmp_path)
    assert len(calls) == 1


@pytest.mark.parametrize("upload_fails", [False, True])
def test_release_only_becomes_visible_after_all_assets_upload(tmp_path, monkeypatch, upload_fails):
    (tmp_path / "release.json").write_text(json.dumps({"tag": "acl-release-test", "version": "test",
                                                     "installer": {"name": "Setup.exe"}}))
    for name in ["Setup.exe", "Setup.exe.sha256", "release-notes.md"]:
        (tmp_path / name).write_text("test")
    calls = []
    def gh(*args):
        calls.append(args)
        if args[:2] == ("release", "create") and upload_fails: raise RuntimeError("upload failed")
        return "[]"
    monkeypatch.setattr(pub, "_gh", gh)
    if upload_fails:
        with pytest.raises(RuntimeError): pub.publish("acl-release-test", "test/releases", tmp_path)
        assert len(calls) == 2
    else:
        pub.publish("acl-release-test", "test/releases", tmp_path)
        assert "--draft" in calls[1] and "--draft=false" in calls[2]
        assert "--clobber" not in str(calls)


# --- ACL-1565: a main run that reused a full green run of the same tree -----------------------

@pytest.fixture
def reused_release(tmp_path, monkeypatch):
    """Real git: stage S had the full run 41; main merge M (same tree) has run 42 with tests skipped."""
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=tmp_path, text=True).strip()
    git("init", "-q", "-b", "main")
    git("config", "user.name", "CI fixture"); git("config", "user.email", "ci@example.invalid")
    (tmp_path / "app.py").write_text("# v1\n"); git("add", "app.py"); git("commit", "-qm", "base")
    git("checkout", "-qb", "stage"); (tmp_path / "app.py").write_text("# v2\n"); git("commit", "-qam", "stage")
    stage = git("rev-parse", "HEAD")
    git("checkout", "-q", "main"); git("merge", "-q", "--no-ff", "--no-edit", "-m", "promote", "stage")
    main = git("rev-parse", "HEAD")
    git("update-ref", "refs/remotes/origin/main", main)
    git("tag", "-a", "acl-release-2026-10-04", "-m", "CI success https://github.com/test/source/actions/runs/42")
    monkeypatch.chdir(tmp_path)
    run = lambda rid, sha, branch: {"id": rid, "head_sha": sha, "status": "completed", "conclusion": "success",
                                    "path": ".github/workflows/ci.yml", "head_branch": branch, "event": "push",
                                    "html_url": f"https://github.com/test/source/actions/runs/{rid}",
                                    "repository": {"full_name": "test/source"},
                                    "head_repository": {"full_name": "test/source"}}
    state = {"stage": stage, "main": main, "main_run": run(42, main, "main"), "stage_run": run(41, stage, "stage"),
             "main_jobs": jobs_of(42, "skipped"), "stage_jobs": jobs_of(41), "calls": []}
    def gh(*args):
        path = args[1]
        state["calls"].append(path)
        if path == "repos/test/source/actions/runs/42":
            return json.dumps(state["main_run"])
        if path.startswith("repos/test/source/actions/runs/42/jobs"):
            return json.dumps(state["main_jobs"])
        if path.startswith("repos/test/source/actions/runs/41/jobs"):
            return json.dumps(state["stage_jobs"])
        if path.startswith("repos/test/source/actions/workflows/ci.yml/runs?"):
            sha = path.split("head_sha=")[1].split("&")[0]
            return json.dumps({"workflow_runs": [r for r in (state["stage_run"], state["main_run"]) if r["head_sha"] == sha]})
        raise AssertionError(path)
    monkeypatch.setattr(pub, "_gh", gh)
    return state


def test_reused_main_run_is_accepted_with_a_provable_full_run_of_the_same_tree(reused_release):
    assert pub.verify_promotion("acl-release-2026-10-04", "test/source") == reused_release["main"]
    assert any(f"head_sha={reused_release['stage']}" in c for c in reused_release["calls"])


@pytest.mark.parametrize("break_it", [
    lambda s: s.update(stage_run=dict(s["stage_run"], conclusion="failure")),
    lambda s: s.update(stage_run=dict(s["stage_run"], event="pull_request")),
    lambda s: s.update(stage_jobs=jobs_of(41, "skipped")),        # the stage run reused too
    lambda s: s.update(stage_jobs=jobs_of(41, "cancelled")),
    lambda s: s.update(stage_run=dict(s["stage_run"], head_sha="0" * 40)),  # no run for this tree
])
def test_reused_main_run_without_a_provable_full_run_is_refused(reused_release, break_it):
    break_it(reused_release)
    with pytest.raises(ValueError, match="neither ran the full test matrix"):
        pub.verify_promotion("acl-release-2026-10-04", "test/source")


def test_main_run_with_a_failed_test_job_is_refused(reused_release):
    reused_release["main_jobs"] = jobs_of(42, "failure")
    reused_release["stage_jobs"] = jobs_of(41, "failure")
    with pytest.raises(ValueError, match="neither ran the full test matrix"):
        pub.verify_promotion("acl-release-2026-10-04", "test/source")


def test_reuse_lookup_api_error_refuses_the_release(reused_release, monkeypatch):
    def broken(*args):
        if "/workflows/" in args[1]:
            raise subprocess.CalledProcessError(1, "gh")
        return reused_release_gh(*args)
    reused_release_gh = pub._gh
    monkeypatch.setattr(pub, "_gh", broken)
    with pytest.raises(ValueError, match="could not be proven"):
        pub.verify_promotion("acl-release-2026-10-04", "test/source")


# --- review M1/M2 ------------------------------------------------------------------------------

def test_jobs_api_error_on_the_recorded_main_run_stops_the_release(promotion, monkeypatch):
    # A mutant that swallows this error and returns the sha must fail here.
    ok = pub._gh
    def broken(*args):
        if "/jobs?" in args[1]:
            raise subprocess.CalledProcessError(1, "gh")
        return ok(*args)
    monkeypatch.setattr(pub, "_gh", broken)
    with pytest.raises(subprocess.CalledProcessError):
        pub.verify_promotion("acl-release-2026-09-19", "test/source")


def test_jobs_api_error_on_the_candidate_proof_run_means_no_proof(reused_release, monkeypatch):
    ok = pub._gh
    def broken(*args):
        if args[1].startswith("repos/test/source/actions/runs/41/jobs"):
            raise subprocess.CalledProcessError(1, "gh")
        return ok(*args)
    monkeypatch.setattr(pub, "_gh", broken)
    with pytest.raises(ValueError, match="could not be proven"):
        pub.verify_promotion("acl-release-2026-10-04", "test/source")


def test_reused_release_names_the_run_that_really_tested_the_tree(reused_release):
    lines = []
    pub.verify_promotion("acl-release-2026-10-04", "test/source", report=lines.append)
    tree = subprocess.check_output(["git", "rev-parse", "HEAD^{tree}"], text=True).strip()
    assert lines == [f"Main run https://github.com/test/source/actions/runs/42 reused full results of run "
                     f"https://github.com/test/source/actions/runs/41 (commit {reused_release['stage']}, "
                     f"same tree {tree})"]


def test_full_release_names_the_main_run(promotion):
    lines = []
    pub.verify_promotion("acl-release-2026-09-19", "test/source", report=lines.append)
    assert lines == ["Full test matrix ran in the recorded main run https://github.com/test/source/actions/runs/42"]


# --- ACL-1581: PR runs test one Python; the tag path is unchanged ------------------------------

def one_python_jobs(run_id):
    jobs = jobs_of(run_id)
    jobs["jobs"] = [j for j in jobs["jobs"] if not j["name"].startswith("test (3.11")]
    return jobs


def test_tag_path_full_push_run_still_publishes(promotion):
    lines = []
    assert pub.verify_promotion("acl-release-2026-09-19", "test/source", report=lines.append) == "a" * 40
    assert lines == ["Full test matrix ran in the recorded main run https://github.com/test/source/actions/runs/42"]


def test_tag_path_reused_main_run_still_publishes(reused_release):
    assert pub.verify_promotion("acl-release-2026-10-04", "test/source") == reused_release["main"]


def test_only_a_one_python_pr_run_for_the_tree_refuses_the_release(reused_release):
    reused_release["stage_run"] = dict(reused_release["stage_run"], event="pull_request")
    reused_release["stage_jobs"] = one_python_jobs(41)
    with pytest.raises(ValueError, match="neither ran the full test matrix"):
        pub.verify_promotion("acl-release-2026-10-04", "test/source")


def test_a_one_python_matrix_is_refused_even_on_a_push_run(reused_release):
    reused_release["stage_jobs"] = one_python_jobs(41)
    with pytest.raises(ValueError, match="neither ran the full test matrix"):
        pub.verify_promotion("acl-release-2026-10-04", "test/source")


def test_a_one_python_main_run_is_not_a_full_run(reused_release):
    # Даже если бы main-прогон сам прогнал только 3.13, он не «полный» и нужен доказуемый полный прогон.
    reused_release["main_jobs"] = one_python_jobs(42)
    reused_release["stage_jobs"] = one_python_jobs(41)
    with pytest.raises(ValueError, match="neither ran the full test matrix"):
        pub.verify_promotion("acl-release-2026-10-04", "test/source")
