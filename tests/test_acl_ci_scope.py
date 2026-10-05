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
    ["packaging/windows/build.ps1"], [".github/workflows/ci.yml"],  # linux-ok: paths for the classifier, never run
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


# --- ACL-1581: one Python on PRs unless dependency/bootstrap inputs change ------------------------

from scripts.acl_ci_reuse import FULL_PYTHON_VERSIONS  # noqa: E402
from scripts.acl_ci_scope import PR_PYTHON_VERSIONS, python_versions  # noqa: E402


def test_full_and_pr_python_sets():
    assert FULL_PYTHON_VERSIONS == ("3.11", "3.13")
    assert PR_PYTHON_VERSIONS == ("3.13",)


@pytest.mark.parametrize("paths", [
    ["dashboard.py"], ["src/fb_ads_rpa/page_capture.py", "tests/test_page_capture.py"],
    ["README.md", "dashboard.py"], ["scripts/task_tracker.py"], ["docs/runtime.py"],
    ["tests/test_acl_ci_scope.py"], ["packaging_notes.txt"], ["constraints.md"],
    ["sub/.python-version"], ["sub/constraints.txt"], ["sub/requirements.in"], ["scripts/requirements_note.py"],
])
def test_pr_without_dependency_or_bootstrap_changes_runs_one_python(paths):
    result = classify(paths, "pull_request")
    assert result["windows"] is True and result["python"] == ["3.13"]


@pytest.mark.parametrize("path", [
    "requirements.in", "requirements-dev.in", "constraints.txt", "constraints-py313.txt", ".python-version",
    "requirements.txt", "requirements-dev.txt", "requirements-extras.txt", "requirements-new.txt",
    "pyproject.toml", "setup.py", "setup.cfg", "pytest.ini", "conftest.py", "tests/conftest.py",
    "install_acl.bat", "Install ACL.bat", "enable_stable_updates.bat", "start_dashboard.bat",
    "tests/bootstrap_windows_harness.ps1",  # linux-ok: paths for the classifier, never run
    "packaging/windows/build.ps1", "packaging/windows/installer.iss", "packaging/windows/setup_wizard.py",  # linux-ok: same
    ".github/workflows/ci.yml", ".github/workflows/windows-installer.yml", ".github/workflows/new.yml",
    "scripts/acl_test_shard.py", "scripts/acl_test_platform.py", "scripts/acl_ci_scope.py", "scripts/acl_ci_reuse.py",
])
def test_pr_touching_dependency_or_bootstrap_inputs_runs_both_pythons(path):
    for paths in ([path], ["dashboard.py", path, "docs/note.md"]):
        result = classify(paths, "pull_request")
        assert result["windows"] is True and result["python"] == ["3.11", "3.13"], paths


@pytest.mark.parametrize("event", ["push", "workflow_dispatch", "", "unknown", "schedule"])
@pytest.mark.parametrize("paths", [[], ["dashboard.py"], ["README.md"], ["requirements.txt"]])
def test_push_and_manual_runs_always_run_both_pythons(event, paths):
    assert classify(paths, event)["python"] == ["3.11", "3.13"]


def test_empty_pr_diff_runs_both_pythons():
    assert classify([], "pull_request")["python"] == ["3.11", "3.13"]


def test_docs_only_pr_keeps_its_light_path():
    # Windows не запускается; список версий всё равно полный, чтобы матрица не была пустой.
    result = classify(["docs/note.md"], "pull_request")
    assert result["windows"] is False and result["python"] == ["3.11", "3.13"]


@pytest.mark.parametrize("event", [{}, {"pull_request": None}, {"pull_request": {"base": {"sha": "a"*40}, "head": {"sha": "b"*40}}}])
def test_unclassifiable_pr_runs_both_pythons(tmp_path, event):
    assert select(tmp_path, "pull_request", event)["python"] == ["3.11", "3.13"]


@pytest.mark.parametrize("paths,expected", [(["dashboard.py"], ["3.13"]), (["requirements.txt"], ["3.11", "3.13"])])
def test_python_versions_helper(paths, expected):
    assert python_versions(paths, "pull_request") == expected
    assert python_versions(paths, "push") == ["3.11", "3.13"]


def test_real_pr_diff_with_requirements_runs_both_pythons(tmp_path):
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=tmp_path, text=True).strip()
    git("init", "-q")
    git("config", "user.name", "CI fixture")
    git("config", "user.email", "ci@example.invalid")
    (tmp_path / "app.py").write_text("# v1\n")
    git("add", "app.py"); git("commit", "-qm", "initial")
    base = git("rev-parse", "HEAD")
    (tmp_path / "app.py").write_text("# v2\n")
    git("commit", "-qam", "code only")
    code_only = git("rev-parse", "HEAD")
    event = lambda head: {"pull_request": {"base": {"sha": base}, "head": {"sha": head}}}
    assert select(tmp_path, "pull_request", event(code_only))["python"] == ["3.13"]
    (tmp_path / "requirements.txt").write_text("pytest\n")
    git("add", "requirements.txt"); git("commit", "-qm", "deps")
    assert select(tmp_path, "pull_request", event(git("rev-parse", "HEAD")))["python"] == ["3.11", "3.13"]


@pytest.mark.parametrize("python,expected", [
    ([], ["3.11", "3.13"]), (None, ["3.11", "3.13"]), (["2.7"], ["3.11", "3.13"]),
    (["2.7", "3.13"], ["3.13"]), (["3.13"], ["3.13"]),
])
def test_main_cleans_the_python_output(monkeypatch, tmp_path, python, expected):
    import json
    import scripts.acl_ci_scope as scope
    for key in ("GITHUB_TOKEN", "GITHUB_STEP_SUMMARY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(scope, "decide", lambda *a, **k: {"windows": True, "reason": "x", "python": python})
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "o"))
    monkeypatch.setattr("sys.argv", ["acl_ci_scope.py", "--event-name", "push", "--event-file", str(tmp_path / "x")])
    scope.main()
    line = next(l for l in (tmp_path / "o").read_text(encoding="utf-8").splitlines() if l.startswith("python="))
    assert json.loads(line[len("python="):]) == expected
