"""CI selection must never hide runtime changes or broken diff discovery."""
import subprocess

import pytest
from scripts.acl_ci_scope import changed_paths, classify, select


@pytest.mark.parametrize("paths", [
    ["README.md", "docs/DEVELOPMENT_WORKFLOW.md"],
    ["tracker/config.json", "tracker/engine.json", "AGENTS.md"],
])
def test_planning_and_tracker_metadata_prs_use_light_checks(paths):
    assert classify(paths, "pull_request")["windows"] is False


@pytest.mark.parametrize("paths", [
    [], ["README.md", "dashboard.py"], ["requirements.txt"],
    ["packaging/windows/build.ps1"], [".github/workflows/ci.yml"],
    ["scripts/acl_ci_scope.py"], ["tracker/cli.py"], ["scripts/task_tracker.py"],
    ["tests/test_acl_ci_scope.py"], ["docs/runtime.py"], ["new-file"],
    ["dashboard/help.md"], ["docs/note.md\nrequirements.txt"],
])
def test_unknown_and_product_changes_keep_windows(paths):
    assert classify(paths, "pull_request")["windows"] is True


@pytest.mark.parametrize("event", ["push", "workflow_dispatch", "", "unknown"])
def test_master_and_manual_runs_always_validate_the_product(event):
    assert classify(["README.md"], event)["windows"] is True


def test_real_diff_includes_deleted_product_file_when_renamed_to_docs(tmp_path):
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=tmp_path, text=True).strip()
    git("init", "-q")
    git("config", "user.name", "CI fixture")
    git("config", "user.email", "ci@example.invalid")
    (tmp_path / "app.py").write_text("# product code\n")
    git("add", "app.py"); git("commit", "-qm", "initial")
    base = git("rev-parse", "HEAD")
    (tmp_path / "docs").mkdir()
    (tmp_path / "app.py").rename(tmp_path / "docs" / "note.md")
    git("add", "-A"); git("commit", "-qm", "move code")
    head = git("rev-parse", "HEAD")
    paths = changed_paths(tmp_path, base, head)
    assert set(paths) == {"app.py", "docs/note.md"}
    assert select(tmp_path, "pull_request", {"pull_request": {"base": {"sha": base}, "head": {"sha": head}}})["windows"]


@pytest.mark.parametrize("event", [{}, {"pull_request": None}, {"pull_request": {"base": {"sha": "a"*40}, "head": {"sha": "b"*40}}}])
def test_missing_event_or_git_history_falls_back_to_full_validation(tmp_path, event):
    assert select(tmp_path, "pull_request", event)["windows"] is True
