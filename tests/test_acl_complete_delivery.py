"""Exact release identities, retries and truthful post-publication acceptance."""
import copy
import importlib.util
import json
from pathlib import Path
import subprocess

import pytest
import yaml

from scripts import acl_complete_delivery as driver
from scripts.acl_delivery_evidence import evidence
from src.web.release_updates import is_newer

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def request_document():
    return {"eligible": True, "merge_sha": "a" * 40, "tree_sha": "b" * 40, "batch_id": "c" * 32,
            "issues": [924], "target": "stable installer", "policy": "d" * 64,
            "requested_at": "2026-10-15T07:00:00+00:00", "main_pr": 930, "ci_run": 123}


def test_release_identity_survives_ci_retry_and_advances_dated_versions(request_document):
    first = driver.release_tag(request_document)
    request_document["ci_run"] = 456
    assert first == driver.release_tag(request_document) == "acl-release-2026-10-15.930"
    assert is_newer(first.removeprefix("acl-release-"), "2026-10-08b")
    assert is_newer("2026-10-15.931", "2026-10-15.930")


@pytest.mark.parametrize("change", [{"merge_sha": "branch"}, {"batch_id": "arbitrary"}, {"issues": []}])
def test_unbound_request_cannot_create_release(request_document, change):
    request_document.update(change)
    with pytest.raises(ValueError):
        driver.release_tag(request_document)


def test_existing_tag_is_verified_without_repoint_or_republication(request_document):
    calls = []
    annotation = json.dumps({k: request_document[k] for k in ("batch_id", "issues", "target", "policy")}, sort_keys=True)
    def execute(*args):
        calls.append(args)
        if args[:2] == ("gh", "api"): return json.dumps(ci_for(request_document))
        if args[1:3] == ("ls-remote", "origin"): return "existing-tag-object"
        if args[1:3] == ("cat-file", "-t"): return "tag"
        if args[1:3] == ("cat-file", "-p"): return "success\n" + annotation
        return request_document["merge_sha"]
    assert driver.ensure_tag(request_document, 123, execute=execute)
    assert not any("push" in call or "--force" in call for call in calls)


def test_main_drift_stops_before_tag_write(request_document):
    def execute(*args):
        if args[:2] == ("gh", "api"): return json.dumps(ci_for(request_document))
        return "e" * 40
    with pytest.raises(ValueError, match="current main"):
        driver.ensure_tag(request_document, 123, execute=execute)


def ci_for(request):
    return {"head_sha": request["merge_sha"], "head_branch": "main", "path": ".github/workflows/ci.yml",
            "event": "push", "status": "completed", "conclusion": "success"}


@pytest.mark.parametrize("field,value", [("path", ".github/workflows/other.yml"), ("head_sha", "b" * 40),
    ("conclusion", "failure"), ("event", "pull_request")])
def test_wrong_native_ci_never_creates_an_immutable_tag(request_document, field, value):
    ci = dict(ci_for(request_document), **{field: value})
    calls = []
    def execute(*args):
        calls.append(args)
        return json.dumps(ci)
    with pytest.raises(ValueError, match="native main CI"):
        driver.ensure_tag(request_document, 123, execute=execute)
    assert len(calls) == 1


def test_real_annotated_tag_publication_retries_without_repointing(tmp_path, request_document):
    repo = tmp_path / "checkout"
    remote = tmp_path / "remote.git"
    repo.mkdir()
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()
    git("init", "-q", "-b", "main")
    git("config", "user.name", "Delivery fixture")
    git("config", "user.email", "delivery@example.invalid")
    (repo / "candidate.txt").write_text("reviewed candidate")
    git("add", "candidate.txt"); git("commit", "-qm", "candidate")
    subprocess.run(["git", "clone", "-q", "--bare", str(repo), str(remote)], check=True)
    git("remote", "add", "origin", str(remote))
    request_document["merge_sha"] = git("rev-parse", "HEAD")
    git("update-ref", "refs/remotes/origin/main", request_document["merge_sha"])
    def execute(*args):
        if args[:2] == ("gh", "api"): return json.dumps(ci_for(request_document))
        return subprocess.check_output(args, cwd=repo, text=True).strip()
    tag = driver.ensure_tag(request_document, 123, execute=execute)
    identity = git("ls-remote", "origin", "refs/tags/" + tag)
    assert git("cat-file", "-t", "refs/tags/" + tag) == "tag"
    assert driver.ensure_tag(request_document, 123, execute=execute) == tag
    request_document["target"] = "another target"
    with pytest.raises(ValueError, match="another identity"):
        driver.ensure_tag(request_document, 123, execute=execute)
    assert git("ls-remote", "origin", "refs/tags/" + tag) == identity
    assert git("rev-parse", "HEAD") == request_document["merge_sha"]


@pytest.mark.parametrize("bad", ["draft", "commit", "api"])
def test_existing_release_is_never_overwritten_or_guessed(bad):
    def execute(*args):
        if args[:3] == ("gh", "release", "list"):
            if bad == "api": raise RuntimeError("unavailable")
            return json.dumps([{"tagName": "acl-release-test", "isDraft": bad == "draft", "isPrerelease": False}])
        if args[:3] == ("gh", "release", "download"):
            return json.dumps({"tag": "acl-release-test", "commit": "a" * 40})
        return "b" * 40 if bad == "commit" else "a" * 40
    with pytest.raises((ValueError, RuntimeError)):
        driver.published("acl-release-test", "example/releases", execute=execute)


def test_stale_version_cannot_replace_a_newer_stable_update_channel():
    def execute(*args):
        if args[:3] == ("gh", "release", "list"):
            return json.dumps([{"tagName": "acl-release-2026-10-16.940", "isDraft": False, "isPrerelease": False}])
        if args[:3] == ("gh", "release", "view"):
            return json.dumps({"tagName": "acl-release-2026-10-16.940"})
        assert args[:3] == ("gh", "release", "download")
        return json.dumps({"version": "2026-10-16.940"})
    with pytest.raises(ValueError, match="newer published"):
        driver.published("acl-release-2026-10-15.930", "example/releases", execute=execute)


def report_for(request):
    request["tag"] = driver.release_tag(request)
    return {"result": "passed", "source_sha": request["merge_sha"], "tree_sha": request["tree_sha"],
            "checks": ["install", "update", "restart", "recovery", "data preserved"],
            "update": {"result": "updated"}, "recovery": {"result": "rolled_back"},
            "published": {"repository": "example/releases", "release_url": "https://example.invalid/release",
                          "current": {"commit": request["merge_sha"], "tag": request["tag"],
                                      "installer": {"sha256": "f" * 64}}}}


def test_receipt_requires_actual_published_acceptance(request_document):
    report = report_for(request_document)
    result = evidence(request_document, report, "https://example.invalid/native-run")
    assert result["merge_shas"] == [request_document["merge_sha"]]
    assert result["result"] == "passed" and result["target"] == request_document["target"]


@pytest.mark.parametrize("broken", ["unpublished", "failed", "wrong-source", "wrong-tree", "recovery", "update"])
def test_missing_acceptance_stays_pending(request_document, broken):
    report = report_for(request_document)
    if broken == "unpublished": report.pop("published")
    if broken == "failed": report["result"] = "failed"
    if broken == "wrong-source": report["source_sha"] = "0" * 40
    if broken == "wrong-tree": report["tree_sha"] = "0" * 40
    if broken in ("recovery", "update"): report[broken]["result"] = "failed"
    with pytest.raises(ValueError): evidence(request_document, report, "https://example.invalid/run")


def test_actual_published_manifest_must_cover_exact_request(request_document):
    path = ROOT / "packaging" / "windows" / "published_update.py"
    spec = importlib.util.spec_from_file_location("published_acceptance", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    request_document["tag"] = driver.release_tag(request_document)
    manifest = {"tag": request_document["tag"], "version": request_document["tag"].removeprefix("acl-release-"),
                "commit": request_document["merge_sha"], "fixed": [{"issue": "ACL-1924"}]}
    release = {"tag_name": request_document["tag"], "draft": False, "prerelease": False}
    module.validate_current(manifest, release, request_document)
    for field in ("tag", "commit", "fixed", "version"):
        bad = copy.deepcopy(manifest)
        bad[field] = [] if field == "fixed" else "different"
        with pytest.raises(ValueError): module.validate_current(bad, release, request_document)


def test_automatic_driver_requires_trusted_request_and_native_success():
    workflow = yaml.safe_load((ROOT / ".github/workflows/complete-delivery.yml").read_text())
    jobs = workflow["jobs"]
    assert jobs["verify"]["name"] == "Verify published Windows package"
    assert jobs["publish"]["uses"] == "./.github/workflows/release.yml"
    assert jobs["publish"]["with"]["continuation"] is True
    assert jobs["record"]["needs"] == ["resolve", "verify"]
    assert workflow["concurrency"]["cancel-in-progress"] is False
    commands = "\n".join(s.get("run", "") for s in jobs["resolve"]["steps"])
    assert "tracker_delivery_receipt.py resolve" in commands and "result\" != 75" in commands
    commands = "\n".join(s.get("run", "") for s in jobs["record"]["steps"])
    assert "--verification-job" in commands and "--apply" in commands
    recovery = "\n".join(s.get("run", "") for s in jobs["resume-receipt"]["steps"])
    assert "tracker_delivery_receipt.py record" in recovery
    assert "test_update_cycle" not in recovery and "release.yml" not in recovery


@pytest.mark.parametrize("verify_attempt,record_attempt", [(2, 2), (1, 2)])
def test_partial_retries_keep_actual_upstream_artifact_outputs(verify_attempt, record_attempt):
    workflow = yaml.safe_load((ROOT / ".github/workflows/complete-delivery.yml").read_text())
    jobs = workflow["jobs"]
    # GitHub reuses resolve1 after a failed publisher/verifier; it also clones
    # verify1 after only record failed. Persisted outputs retain original values.
    available = {"acl-delivery-request-91-1", f"acl-delivery-evidence-91-{verify_attempt}"}
    outputs = {"resolve": {"request_artifact": "acl-delivery-request-91-1"},
               "verify": {"evidence_artifact": f"acl-delivery-evidence-91-{verify_attempt}"}}
    def selected(job):
        step = next(s for s in jobs[job]["steps"] if s.get("uses") == "actions/download-artifact@v4")
        expression = step["with"]["name"]
        import re
        match = re.fullmatch(r"\$\{\{ needs\.(resolve|verify)\.outputs\.(\w+) \}\}", expression)
        assert match, "A rerun must consume the actual producer output, never its own attempt"
        return outputs[match[1]][match[2]]
    assert selected("verify") in available and selected("record") in available
    assert selected("verify").endswith("-1") and record_attempt == 2
    upload = next(s for s in jobs["verify"]["steps"] if s.get("name") == "Keep isolated package acceptance and delivery evidence")
    assert "delivery/request.json" in upload["with"]["path"] and "delivery/evidence.json" in upload["with"]["path"]


def test_cloned_native_logs_bind_the_original_artifact_producer():
    # The latest native clone may be attempt2 while these actual bytes came from1.
    log = '2026-10-15T12:00:00Z TRACKER_DELIVERY_ARTIFACT {"name":"acl-delivery-evidence-91-1"}\n'
    assert driver.producer_artifact(log, 91) == "acl-delivery-evidence-91-1"
    with pytest.raises(ValueError): driver.producer_artifact(log, 92)
    with pytest.raises(ValueError): driver.producer_artifact(log + log, 91)
    with pytest.raises(ValueError): driver.producer_artifact("new verifier failed before acceptance", 91)
