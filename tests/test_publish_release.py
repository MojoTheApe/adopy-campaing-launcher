import copy
import json
from pathlib import Path
import subprocess

import pytest

from scripts import acl_publish_release as pub

FIXTURES = Path(__file__).with_name("fixtures") / "ci_reuse"
# ACL-1582: текущая раскладка (Linux + Windows) и записанный прогон старой (всё на Windows).
FULL_JOBS = json.loads((FIXTURES / "ci_full_jobs.json").read_text(encoding="utf-8"))
LEGACY_JOBS = json.loads((FIXTURES / "ci_full_jobs_legacy.json").read_text(encoding="utf-8"))
TEST_JOB_PREFIXES = ("linux tests (", "windows tests (", "test (")


def jobs_of(run_id, conclusion="success", layout=None):
    """Full ci.yml jobs moved to ``run_id``; a reused run has its test jobs skipped."""
    jobs = copy.deepcopy(layout or FULL_JOBS)
    for job in jobs["jobs"]:
        job["run_id"] = run_id
        if job["name"].startswith(TEST_JOB_PREFIXES):
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
    state["jobs"] = jobs_of(42)
    def gh(*args):
        if args[1].endswith("/runs/42/jobs?filter=latest&per_page=100"):
            return json.dumps(state["jobs"])
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

@pytest.fixture(autouse=True)
def no_retry_pause(monkeypatch):
    monkeypatch.setattr(pub.reuse, "RETRY_PAUSE", 0)


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
             "main_jobs": jobs_of(42, "skipped"), "stage_jobs": jobs_of(41), "calls": [],
             "pulls": [], "annotations": []}
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
        if path == "repos/test/source/actions/runs/41":
            return json.dumps(state["stage_run"])
        if "/commits/" in path and path.endswith("/pulls?per_page=10"):
            return json.dumps(state["pulls"])
        if "/check-runs/" in path and "/annotations" in path:
            return json.dumps(state["annotations"])
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


# --- ACL-1582: the tag path with the Linux + Windows job layout ---------------------------------

def test_tag_path_accepts_a_main_run_with_every_linux_and_windows_job(promotion):
    names = {j["name"] for j in promotion["jobs"]["jobs"]}
    assert {"linux tests (3.11, 1)", "linux tests (3.13, 3)", "windows tests (3.13)", "ACL verification"} <= names
    assert pub.verify_promotion("acl-release-2026-09-19", "test/source") == "a" * 40


@pytest.mark.parametrize("missing", ["windows tests (3.13)", "linux tests (3.11, 2)", "linux tests (3.13, 1)",
                                     "ACL verification"])
def test_tag_path_refuses_a_main_run_without_one_job_of_the_layout(promotion, missing):
    promotion["jobs"]["jobs"] = [j for j in promotion["jobs"]["jobs"] if j["name"] != missing]
    with pytest.raises(ValueError, match="neither ran the full test matrix"):
        pub.verify_promotion("acl-release-2026-09-19", "test/source")


@pytest.mark.parametrize("job,conclusion", [("windows tests (3.13)", "failure"), ("windows tests (3.13)", "skipped"),
                                            ("linux tests (3.13, 2)", "cancelled")])
def test_tag_path_refuses_a_failed_or_skipped_linux_or_windows_job(promotion, job, conclusion):
    for row in promotion["jobs"]["jobs"]:
        if row["name"] == job:
            row["conclusion"] = conclusion
    with pytest.raises(ValueError, match="neither ran the full test matrix"):
        pub.verify_promotion("acl-release-2026-09-19", "test/source")


def test_tag_path_refuses_a_green_main_run_of_the_old_layout(promotion):
    # Записанный зелёный прогон до ACL-1582: все `test (py, shard)` на Windows.
    promotion["jobs"] = jobs_of(42, layout=LEGACY_JOBS)
    with pytest.raises(ValueError, match="layout before ACL-1582"):
        pub.verify_promotion("acl-release-2026-09-19", "test/source")


def test_tag_path_refuses_a_mixed_layout_even_if_the_new_jobs_are_green(promotion):
    legacy = [dict(j, run_id=42) for j in LEGACY_JOBS["jobs"] if j["name"] == "test (3.13, 1)"]
    promotion["jobs"]["jobs"].extend(legacy)
    with pytest.raises(ValueError, match="layout before ACL-1582"):
        pub.verify_promotion("acl-release-2026-09-19", "test/source")


def test_reused_main_run_refuses_an_old_layout_stage_run_as_proof(reused_release):
    reused_release["stage_jobs"] = jobs_of(41, layout=LEGACY_JOBS)
    with pytest.raises(ValueError, match="neither ran the full test matrix"):
        pub.verify_promotion("acl-release-2026-10-04", "test/source")


# --- ACL-1581: PR runs test one Python on Linux; the tag path is unchanged ------------------------------

def one_python_jobs(run_id):
    jobs = jobs_of(run_id)
    jobs["jobs"] = [j for j in jobs["jobs"] if not j["name"].startswith("linux tests (3.11")]
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


def test_reused_main_run_refuses_a_stage_run_without_the_windows_job(reused_release):
    jobs = jobs_of(41)
    jobs["jobs"] = [j for j in jobs["jobs"] if not j["name"].startswith("windows tests (")]
    reused_release["stage_jobs"] = jobs
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


# --- ACL-1649: the full set may be split between the stage run and the main run itself -----------------

def _only(jobs, keep):
    jobs["jobs"] = [j for j in jobs["jobs"] if keep(j["name"])]
    return jobs


def test_the_main_runs_own_python_3_11_jobs_count_toward_the_release_proof(reused_release):
    # Stage ran 3.13 + Windows (python 3.13 only on stage); the main push ran just 3.11 and is partial.
    reused_release["stage_jobs"] = _only(reused_release["stage_jobs"], lambda n: "(3.11" not in n)
    reused_release["main_jobs"] = _only(jobs_of(42, "success"), lambda n: "(3.11" in n or n == "ACL verification")
    assert pub.verify_promotion("acl-release-2026-10-04", "test/source") == reused_release["main"]


def test_the_release_is_refused_when_python_3_11_ran_nowhere(reused_release):
    reused_release["stage_jobs"] = _only(reused_release["stage_jobs"], lambda n: "(3.11" not in n)
    reused_release["main_jobs"] = _only(jobs_of(42, "skipped"), lambda n: n == "ACL verification")
    with pytest.raises(ValueError, match="neither ran the full test matrix"):
        pub.verify_promotion("acl-release-2026-10-04", "test/source")


def test_gh_and_git_output_decoded_as_utf8(monkeypatch, tmp_path):
    """ACL-1743: на windows-latest вывод gh/git не должен декодироваться как cp1252."""
    from scripts import acl_ci_reuse as reuse

    payload = {"title": "Release — релиз", "note": "тест \u044f"}
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")  # содержит байт 0x8f

    def fake_check_output(cmd, **kwargs):
        # имитируем Windows: без явной кодировки текст читается как cp1252
        assert kwargs.get("encoding") == "utf-8", "subprocess должен декодировать вывод как UTF-8"
        return raw.decode(kwargs["encoding"], kwargs.get("errors", "strict")) + "\n"

    monkeypatch.setattr(subprocess, "check_output", fake_check_output)
    assert pub._api("repos/x/y") == payload
    assert pub._gh("api", "x").startswith("{")
    assert pub._git("log").startswith("{")
    assert reuse.gh_api("repos/x/y") == payload
    assert reuse._git(tmp_path, "log").startswith("{")


# --- ACL-2107: flaky GitHub listing must not refuse a release whose proof is named by id -------

REUSE_NOTE = {"title": "acl-reused-runs", "message": "runs=41"}


def _flaky(state, monkeypatch, *, empty_list_always=False, empty_list_first=0, pulls_fail=0):
    ok = pub._gh
    counters = {"list": 0, "pulls": 0}

    def gh(*args):
        path = args[1]
        if "/workflows/ci.yml/runs?" in path:
            counters["list"] += 1
            if empty_list_always or counters["list"] <= empty_list_first:
                return json.dumps({"workflow_runs": []})
        if "/commits/" in path and path.endswith("/pulls?per_page=10"):
            counters["pulls"] += 1
            if counters["pulls"] <= pulls_fail:
                raise subprocess.CalledProcessError(1, "gh")
        return ok(*args)
    monkeypatch.setattr(pub, "_gh", gh)
    return counters


def test_empty_head_sha_list_and_a_failing_pulls_call_still_prove_via_the_named_run_id(reused_release, monkeypatch):
    reused_release["annotations"] = [REUSE_NOTE]
    counters = _flaky(reused_release, monkeypatch, empty_list_always=True, pulls_fail=1)
    assert pub.verify_promotion("acl-release-2026-10-04", "test/source") == reused_release["main"]
    assert counters["pulls"] >= 2 and "repos/test/source/actions/runs/41" in reused_release["calls"]


def test_an_empty_head_sha_list_is_retried(reused_release, monkeypatch):
    counters = _flaky(reused_release, monkeypatch, empty_list_first=2)
    assert pub.verify_promotion("acl-release-2026-10-04", "test/source") == reused_release["main"]
    assert counters["list"] > 2


def test_persistent_empty_list_without_a_named_run_is_refused(reused_release, monkeypatch):
    _flaky(reused_release, monkeypatch, empty_list_always=True)
    with pytest.raises(ValueError, match="neither ran the full test matrix"):
        pub.verify_promotion("acl-release-2026-10-04", "test/source")


def test_pulls_api_error_after_retries_is_could_not_be_proven_not_no_pr(reused_release, monkeypatch):
    _flaky(reused_release, monkeypatch, pulls_fail=99)
    with pytest.raises(ValueError, match="could not be proven"):
        pub.verify_promotion("acl-release-2026-10-04", "test/source")


@pytest.mark.parametrize("break_it", [
    lambda s: s.update(stage_run=dict(s["stage_run"], head_sha="0" * 40)),            # foreign tree
    lambda s: s.update(stage_run=dict(s["stage_run"], event="pull_request"),
                       pulls=[_pr(s)]),                                               # no tested-tree note
    lambda s: s.update(stage_jobs=one_python_jobs(41)),                               # no Python 3.11
    lambda s: s.update(stage_jobs=jobs_of(41, "skipped")),
])
def test_a_named_run_is_still_rejected_when_it_does_not_prove_the_full_tree(reused_release, monkeypatch, break_it):
    reused_release["annotations"] = [REUSE_NOTE]
    break_it(reused_release)
    _flaky(reused_release, monkeypatch, empty_list_always=True)
    with pytest.raises(ValueError, match="neither ran the full test matrix"):
        pub.verify_promotion("acl-release-2026-10-04", "test/source")


def _pr(state):
    return {"number": 7, "merged_at": "2026-10-04T00:00:00Z", "merge_commit_sha": state["main"],
            "head": {"sha": state["stage"], "repo": {"full_name": "test/source"}},
            "base": {"ref": "main", "repo": {"full_name": "test/source"}}}


def _without_listing(monkeypatch):
    ok = pub._gh
    def gh(*args):
        if "/workflows/ci.yml/runs?" in args[1]:
            raise subprocess.CalledProcessError(1, "gh", stderr="listing unavailable")
        return ok(*args)
    monkeypatch.setattr(pub, "_gh", gh)


def test_complete_named_proof_never_requests_the_run_list(reused_release, monkeypatch):
    reused_release["annotations"] = [REUSE_NOTE]
    _without_listing(monkeypatch)
    assert pub.verify_promotion("acl-release-2026-10-04", "test/source") == reused_release["main"]
    assert not any("/workflows/" in path for path in reused_release["calls"])


def test_named_partial_proof_combines_with_main_jobs_without_listing(reused_release, monkeypatch):
    reused_release["annotations"] = [REUSE_NOTE]
    reused_release["stage_jobs"] = one_python_jobs(41)
    reused_release["main_jobs"] = _only(jobs_of(42), lambda n: "(3.11" in n or n in {"ACL verification", pub.reuse.SCOPE_JOB["ci.yml"]})
    _without_listing(monkeypatch)
    lines = []
    assert pub.verify_promotion("acl-release-2026-10-04", "test/source", report=lines.append) == reused_release["main"]
    assert "runs https://github.com/test/source/actions/runs/41, https://github.com/test/source/actions/runs/42" in lines[0]


def _legacy_scope_log(state, monkeypatch, **overrides):
    tree = subprocess.check_output(["git", "rev-parse", "HEAD^{tree}"], text=True).strip()
    payload = {"reason": "reused-green-full-run-of-same-tree", "tree": tree, "commit": state["stage"],
               "windows": False, "linux": False, "python": ["3.11", "3.13"],
               "reused_runs": ["https://github.com/test/source/actions/runs/41"],
               "reused_from": "https://github.com/test/source/actions/runs/41"}
    payload.update(overrides)
    scope_job = next(j for j in state["main_jobs"]["jobs"] if j["name"] == pub.reuse.SCOPE_JOB["ci.yml"])
    path = f"repos/test/source/actions/jobs/{scope_job['id']}/logs"
    ok = pub._gh
    calls = []
    def gh(*args):
        if args[-1].endswith("/logs"):
            calls.append(args[-1])
            assert args == ("api", "--allow-escape-sequences", path)
            return ("2026-10-22T15:01:20.000Z ##[group]Run python3 scripts/acl_ci_scope.py --workflow ci.yml\n"
                    "2026-10-22T15:01:23.123Z " + json.dumps(payload) + "\n"
                    "2026-10-22T15:01:24.000Z ##[group]Run python3 scripts/acl_scan_secrets.py\n")
        return ok(*args)
    monkeypatch.setattr(pub, "_gh", gh)
    return calls


def test_legacy_main_scope_stdout_names_the_direct_run_proof(reused_release, monkeypatch):
    logs = _legacy_scope_log(reused_release, monkeypatch)
    _without_listing(monkeypatch)
    assert pub.verify_promotion("acl-release-2026-10-04", "test/source") == reused_release["main"]
    assert len(logs) == 1 and "repos/test/source/actions/runs/41" in reused_release["calls"]


def test_legacy_stdout_pr_proof_survives_transient_binding_failure(reused_release, monkeypatch):
    reused_release["stage_run"]["event"] = "pull_request"
    reused_release["pulls"] = [_pr(reused_release)]
    tree = subprocess.check_output(["git", "rev-parse", "HEAD^{tree}"], text=True).strip()
    reused_release["annotations"] = [{"title": "acl-tested-tree", "message": "tree=" + tree}]
    _legacy_scope_log(reused_release, monkeypatch)
    counters = _flaky(reused_release, monkeypatch, empty_list_always=True, pulls_fail=1)
    assert pub.verify_promotion("acl-release-2026-10-04", "test/source") == reused_release["main"]
    assert counters["pulls"] >= 2 and counters["list"] == 0


@pytest.mark.parametrize("break_it", [
    lambda s: s.update(stage_run=dict(s["stage_run"], repository={"full_name": "foreign/repo"})),
    lambda s: s.update(stage_run=dict(s["stage_run"], path=".github/workflows/other.yml")),
    lambda s: s.update(stage_run=dict(s["stage_run"], event="schedule")),
    lambda s: s.update(stage_run=dict(s["stage_run"], head_sha="0" * 40)),
    lambda s: s.update(stage_run=dict(s["stage_run"], event="pull_request"), pulls=[_pr(s)]),
    lambda s: s.update(stage_run=dict(s["stage_run"], event="pull_request"),
                       annotations=[{"title": "acl-tested-tree", "message": "tree=" + "f" * 40}], pulls=[_pr(s)]),
    lambda s: s.update(stage_jobs=one_python_jobs(41)),
    lambda s: s.update(stage_jobs=jobs_of(41, "skipped")),
])
def test_legacy_scope_ids_still_need_trusted_full_run_proof(reused_release, monkeypatch, break_it):
    _legacy_scope_log(reused_release, monkeypatch)
    break_it(reused_release)
    _flaky(reused_release, monkeypatch, empty_list_always=True)
    with pytest.raises(ValueError, match="neither ran the full test matrix"):
        pub.verify_promotion("acl-release-2026-10-04", "test/source")


@pytest.mark.parametrize("overrides", [
    {"tree": "f" * 40}, {"commit": "invalid"}, {"reason": "product"},
    {"reused_runs": ["https://github.com/foreign/repo/actions/runs/41"]},
    {"reused_from": "https://github.com/test/source/actions/runs/41?untrusted=true"},
])
def test_legacy_scope_payload_does_not_supply_ids_when_invalid(reused_release, monkeypatch, overrides):
    _legacy_scope_log(reused_release, monkeypatch, **overrides)
    counters = _flaky(reused_release, monkeypatch, empty_list_always=True)
    with pytest.raises(ValueError, match="neither ran the full test matrix"):
        pub.verify_promotion("acl-release-2026-10-04", "test/source")
    assert "repos/test/source/actions/runs/41" not in reused_release["calls"] and counters["list"] > 0


def test_urls_or_json_outside_the_classifier_step_do_not_supply_ids():
    payload = json.dumps({"tree": "a" * 40, "commit": "b" * 40,
                          "reason": "reused-green-full-run-of-same-tree",
                          "reused_from": "https://github.com/test/source/actions/runs/41"})
    log = ("https://github.com/test/source/actions/runs/41\n" + payload + "\n"
           "##[group]Run python3 -m pytest tests/\n" + payload)
    assert pub.reuse.scope_log_run_ids(log, "test/source", "ci.yml", "a" * 40) == []


def test_new_annotation_does_not_need_legacy_logs(reused_release, monkeypatch):
    reused_release["annotations"] = [REUSE_NOTE]
    ok = pub._gh
    def gh(*args):
        assert not args[-1].endswith("/logs")
        return ok(*args)
    monkeypatch.setattr(pub, "_gh", gh)
    _without_listing(monkeypatch)
    assert pub.verify_promotion("acl-release-2026-10-04", "test/source") == reused_release["main"]
