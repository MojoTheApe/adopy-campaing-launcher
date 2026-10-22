"""Approval sources keep affected checks; combined batches prove the full tree."""
import json
import subprocess

import pytest

from scripts.acl_ci_scope import select


@pytest.fixture(params=[2, 3], ids=["v2", "v3-five-reviews"])
def candidate(tmp_path, request):
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=tmp_path, text=True).strip()
    git("init", "-q")
    git("config", "user.name", "Batch CI fixture")
    git("config", "user.email", "batch-ci@example.invalid")
    policy = {"schema_version": request.param, "implementation_limit": 5,
              "review_limit": 5 if request.param == 3 else 1, "batch_limit": 5}
    (tmp_path / "tracker").mkdir()
    (tmp_path / "delivery.json").write_text(json.dumps({"workflow": {"staging": True, "execution": policy}}))
    (tmp_path / "tracker/config.json").write_text(json.dumps({
        "project": {"id_prefix": "ACL"}, "execution_policy": policy,
        "workflow": ["idea", "backlog", "todo", "in_progress", "review", "approved", "stage", "merged"]}))
    (tmp_path / "app.py").write_text("# initial product\n")
    git("add", "."); git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")

    def change(path="app.py"):
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# updated candidate\n")
        git("add", "."); git("commit", "-qm", "candidate")
        return git("rev-parse", "HEAD")

    def event(head, branch="codex/acl-1748-example", target="main"):
        return {"pull_request": {"base": {"sha": base, "ref": target}, "head": {"sha": head, "ref": branch}}}
    return tmp_path, base, change, event


@pytest.mark.parametrize("target", ["main", "stage"])
def test_individual_source_uses_retained_affected_checks(candidate, target):
    root, _, change, event = candidate
    payload = event(change(), target=target)
    result = select(root, "pull_request", payload)
    assert result["windows"] and result["linux"] and result["python"] == ["3.13"]
    assert not select(root, "pull_request", payload, workflow="windows-installer.yml")["windows"]


def test_source_dependency_change_still_runs_full_matrix_and_installer(candidate):
    root, _, change, event = candidate
    payload = event(change("requirements.txt"))
    assert select(root, "pull_request", payload)["python"] == ["3.11", "3.13"]
    assert select(root, "pull_request", payload, workflow="windows-installer.yml")["windows"]


@pytest.mark.parametrize("branch,target", [
    ("codex/batch-" + "a" * 32, "stage"), ("stage", "main"),
    ("codex/batch-invalid", "stage"), ("unknown", "main"),
    ("codex/acl-1748-example", "unexpected")])
def test_combined_promotion_and_unknown_routes_require_every_check_even_for_docs(candidate, branch, target):
    root, _, change, event = candidate
    payload = event(change("docs/note.md"), branch, target)
    result = select(root, "pull_request", payload)
    assert result["windows"] and result["linux"] and result["python"] == ["3.11", "3.13"]
    assert select(root, "pull_request", payload, workflow="windows-installer.yml")["windows"]


@pytest.mark.parametrize("branch", ["stage", "main", "master"])
def test_batch_push_proves_complete_actual_branch(candidate, branch):
    root, base, change, _ = candidate
    payload = {"before": base, "after": change("docs/note.md")}
    result = select(root, "push", payload, ref="refs/heads/" + branch)
    assert result["windows"] and result["linux"] and result["python"] == ["3.11", "3.13"]
    assert select(root, "push", payload, ref="refs/heads/" + branch, workflow="windows-installer.yml")["windows"]


@pytest.mark.parametrize("bad", [None, [], {"schema_version": 2},
    {"schema_version": 2, "implementation_limit": True, "review_limit": 1, "batch_limit": 5}])
def test_invalid_or_drifted_opt_in_never_downgrades_checks(candidate, bad):
    root, _, change, event = candidate
    payload = event(change("docs/note.md"))
    descriptor = {"workflow": {"staging": True, "execution": bad}}
    (root / "delivery.json").write_text(json.dumps(descriptor))
    result = select(root, "pull_request", payload)
    assert result["windows"] and result["python"] == ["3.11", "3.13"]


def test_docs_source_can_skip_product_jobs_but_batch_cannot(candidate):
    root, _, change, event = candidate
    head = change("docs/note.md")
    assert not select(root, "pull_request", event(head))["windows"]
    assert select(root, "pull_request", event(head, "codex/batch-" + "a" * 32, "stage"))["windows"]


def test_existing_v1_route_keeps_prior_selection(candidate):
    root, _, change, event = candidate
    payload = event(change(), target="stage")
    for path, key in (("delivery.json", "workflow"), ("tracker/config.json", None)):
        data = json.loads((root / path).read_text())
        policy = data["workflow"]["execution"] if key else data["execution_policy"]
        policy.pop("batch_limit"); policy["schema_version"] = 1; policy["review_limit"] = 1
        (root / path).write_text(json.dumps(data))
    assert select(root, "pull_request", payload)["python"] == ["3.13"]
    payload["pull_request"]["base"]["ref"] = "main"
    assert select(root, "pull_request", payload)["python"] == ["3.11", "3.13"]


def test_malformed_descriptor_shape_falls_back_to_complete_checks(candidate):
    root, _, change, event = candidate
    payload = event(change("docs/note.md"))
    (root / "delivery.json").write_text(json.dumps({"workflow": []}))
    result = select(root, "pull_request", payload)
    assert result["windows"] and result["linux"] and result["python"] == ["3.11", "3.13"]


@pytest.mark.parametrize("field,value", [
    ("review_limit", 0), ("review_limit", 6), ("review_limit", True),
    ("batch_limit", 6), ("batch_limit", True), ("schema_version", 4),
])
def test_invalid_v3_policy_keeps_full_checks_even_when_mirrored(candidate, field, value):
    root, _, change, event = candidate
    payload = event(change("docs/note.md"))
    for name in ("delivery.json", "tracker/config.json"):
        path = root / name
        data = json.loads(path.read_text())
        policy = data["workflow"]["execution"] if name == "delivery.json" else data["execution_policy"]
        policy.update(schema_version=3, review_limit=5, batch_limit=5)
        policy[field] = value
        path.write_text(json.dumps(data))
    result = select(root, "pull_request", payload)
    assert result["reason"] == "classification-unavailable-run-full"
    assert result["windows"] and result["linux"] and result["python"] == ["3.11", "3.13"]
    assert select(root, "pull_request", payload, workflow="windows-installer.yml")["windows"]


@pytest.mark.parametrize("missing", ["policy", "file"])
def test_missing_descriptor_policy_cannot_hide_schema3_tracker(candidate, missing):
    root, _, change, event = candidate
    payload = event(change("docs/note.md"))
    path = root / "delivery.json"
    if missing == "file":
        path.unlink()
    else:
        data = json.loads(path.read_text()); data["workflow"].pop("execution")
        path.write_text(json.dumps(data))
    path = root / "tracker/config.json"
    data = json.loads(path.read_text()); data["execution_policy"].update(schema_version=3, review_limit=5)
    path.write_text(json.dumps(data))
    result = select(root, "pull_request", payload)
    assert result["reason"] == "classification-unavailable-run-full" and result["windows"]


def test_no_stage_keeps_source_affected_and_main_batch_full_checks(candidate):
    root, _, change, event = candidate
    path = root / "delivery.json"
    data = json.loads(path.read_text()); data["workflow"]["staging"] = False
    path.write_text(json.dumps(data))
    path = root / "tracker/config.json"
    data = json.loads(path.read_text()); data["workflow"].remove("stage")
    data["delivery_policy"] = {"staging": {"enabled": False}}
    path.write_text(json.dumps(data))
    head = change()
    assert select(root, "pull_request", event(head))["python"] == ["3.13"]
    result = select(root, "pull_request", event(head, "codex/batch-" + "a" * 32, "main"))
    assert result["python"] == ["3.11", "3.13"] and result["windows"]
    assert select(root, "pull_request", event(head, "codex/batch-" + "a" * 32, "main"),
                  workflow="windows-installer.yml")["windows"]


def test_no_stage_drift_never_reduces_checks(candidate):
    root, _, change, event = candidate
    head = change("docs/note.md")
    path = root / "delivery.json"
    data = json.loads(path.read_text()); data["workflow"]["staging"] = False
    path.write_text(json.dumps(data))
    assert select(root, "pull_request", event(head))["reason"] == "classification-unavailable-run-full"


# ACL-2107: a combined batch PR always selects the full matrix and never borrows jobs.
def _with_body(payload, body):
    payload["pull_request"]["body"] = body
    return payload


@pytest.mark.parametrize("path", ["docs/note.md", "tests/test_x.py", "app.py"])
def test_batch_branch_selects_full_matrix_for_any_change(candidate, path):
    root, _, change, event = candidate
    result = select(root, "pull_request", event(change(path), "codex/batch-" + "a" * 32, "main"))
    assert result["windows"] and result["linux"] and result["python"] == ["3.11", "3.13"]


@pytest.mark.parametrize("branch", ["codex/acl-1748-example", "work/acl-2107-x", "unknown"])
def test_tracker_batch_line_in_body_selects_full_matrix(candidate, branch):
    root, _, change, event = candidate
    payload = _with_body(event(change("tests/test_x.py"), branch, "main"),
                         "Tracker issues: ACL-1\r\nTracker batch: " + "c" * 32 + "\r\n")
    result = select(root, "pull_request", payload)
    assert result["windows"] and result["linux"] and result["python"] == ["3.11", "3.13"]


def test_batch_is_full_even_without_explicit_execution_policy(candidate):
    root, _, change, event = candidate
    (root / "delivery.json").unlink()
    (root / "tracker/config.json").write_text(json.dumps({"project": {"id_prefix": "ACL"}}))
    result = select(root, "pull_request", event(change("tests/test_x.py"), "codex/batch-" + "a" * 32, "main"))
    assert result["windows"] and result["linux"] and result["python"] == ["3.11", "3.13"]


def test_test_only_source_pr_still_skips_python_3_11(candidate):
    root, _, change, event = candidate
    payload = _with_body(event(change("tests/test_x.py")), "Tracker issues: ACL-1\nmention of Tracker batch: in prose\n")
    assert select(root, "pull_request", payload)["python"] == ["3.13"]


def test_batch_pr_decision_never_reuses_other_runs(candidate, monkeypatch):
    from scripts import acl_ci_scope as scope
    root, _, change, event = candidate
    calls = []

    def api(path):
        calls.append(path)
        raise AssertionError("a batch run must not look for reusable runs")
    head = change("tests/test_x.py")
    result = scope.decide(root, "pull_request", event(head, "codex/batch-" + "a" * 32, "main"),
                          repo="o/r", api=api)
    assert not calls and "reused_from" not in result and "reuse" not in result
    assert result["linux"] and result["windows"] and result["python"] == ["3.11", "3.13"]
    # A source PR still goes through the ACL-1649 reuse lookup (the failing fake makes it run full).
    calls.clear()
    source = scope.decide(root, "pull_request", event(head), repo="o/r", api=api)
    assert calls and source["reuse"] == "lookup-failed-run-full" and source["python"] == ["3.13"]
