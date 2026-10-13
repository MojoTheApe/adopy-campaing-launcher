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
    assert select(tmp_path, "pull_request", {"pull_request": {"base": {"sha": base, "ref": "stage"}, "head": {"sha": head}}})["windows"]


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
    result = classify(paths, "pull_request", "stage")
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
        result = classify(paths, "pull_request", "stage")
        assert result["windows"] is True and result["python"] == ["3.11", "3.13"], paths


@pytest.mark.parametrize("event", ["push", "workflow_dispatch", "", "unknown", "schedule"])
@pytest.mark.parametrize("paths", [[], ["dashboard.py"], ["README.md"], ["requirements.txt"]])
def test_push_and_manual_runs_always_run_both_pythons(event, paths):
    assert classify(paths, event)["python"] == ["3.11", "3.13"]


def test_empty_pr_diff_runs_both_pythons():
    assert classify([], "pull_request", "stage")["python"] == ["3.11", "3.13"]


def test_docs_only_pr_keeps_its_light_path():
    # Windows не запускается; список версий всё равно полный, чтобы матрица не была пустой.
    result = classify(["docs/note.md"], "pull_request", "stage")
    assert result["windows"] is False and result["python"] == ["3.11", "3.13"]


@pytest.mark.parametrize("event", [{}, {"pull_request": None}, {"pull_request": {"base": {"sha": "a"*40, "ref": "stage"}, "head": {"sha": "b"*40}}}])
def test_unclassifiable_pr_runs_both_pythons(tmp_path, event):
    assert select(tmp_path, "pull_request", event)["python"] == ["3.11", "3.13"]


@pytest.mark.parametrize("paths,expected", [(["dashboard.py"], ["3.13"]), (["requirements.txt"], ["3.11", "3.13"])])
def test_python_versions_helper(paths, expected):
    assert python_versions(paths, "pull_request", "stage") == expected
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
    event = lambda head: {"pull_request": {"base": {"sha": base, "ref": "stage"}, "head": {"sha": head}}}
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
    monkeypatch.setattr(scope, "decide", lambda *a, **k: {"windows": True, "linux": True, "reason": "x", "python": python})
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "o"))
    monkeypatch.setattr("sys.argv", ["acl_ci_scope.py", "--event-name", "push", "--event-file", str(tmp_path / "x")])
    scope.main()
    line = next(l for l in (tmp_path / "o").read_text(encoding="utf-8").splitlines() if l.startswith("python="))
    assert json.loads(line[len("python="):]) == expected


# --- ACL-1649: 3.11 and the installer only on the way to main ---------------------------------------

from scripts.acl_ci_scope import classify_installer, light_context, touches_installer  # noqa: E402


def test_light_context_is_a_pr_into_stage_or_a_push_to_stage():
    assert light_context("pull_request", "stage") and light_context("push", None, "stage")
    for event, base, branch in [("pull_request", "main", None), ("pull_request", "master", None),
                                ("pull_request", None, None), ("pull_request", "feature/x", None),
                                ("push", None, "main"), ("push", None, "master"), ("push", None, ""),
                                ("workflow_dispatch", None, "stage"), ("schedule", "stage", "stage")]:
        assert not light_context(event, base, branch), (event, base, branch)


@pytest.mark.parametrize("base", ["main", "master", None, "", "feature/x"])
def test_a_pr_not_into_stage_runs_python_3_11_too(base):
    assert classify(["dashboard.py"], "pull_request", base)["python"] == ["3.11", "3.13"]


def test_stage_push_runs_one_python_and_main_push_two():
    assert classify(["dashboard.py"], "push", branch="stage")["python"] == ["3.13"]
    assert classify(["requirements.txt"], "push", branch="stage")["python"] == ["3.11", "3.13"]
    assert classify([], "push", branch="stage")["python"] == ["3.11", "3.13"]     # no diff: full
    for branch in ("main", "master", "", None):
        assert classify(["dashboard.py"], "push", branch=branch)["python"] == ["3.11", "3.13"]
    assert classify(["dashboard.py"], "workflow_dispatch", branch="stage")["python"] == ["3.11", "3.13"]


def test_docs_only_push_to_stage_is_light_but_never_on_main():
    assert classify(["docs/note.md"], "push", branch="stage")["windows"] is False
    assert classify(["docs/note.md", "dashboard.py"], "push", branch="stage")["windows"] is True
    assert classify(["docs/note.md"], "push", branch="main")["windows"] is True


def test_classify_tells_linux_and_windows_apart():
    full = classify(["dashboard.py"], "pull_request", "stage")
    assert full["linux"] is True and full["windows"] is True
    docs = classify(["README.md"], "pull_request", "stage")
    assert docs["linux"] is False and docs["windows"] is False


@pytest.mark.parametrize("path", [
    "packaging/windows/build.ps1", "packaging/windows/acl_windows.spec", ".github/workflows/windows-installer.yml",  # linux-ok: classifier input
    ".github/workflows/release.yml", "requirements.txt", "requirements-dev.in", "constraints.txt", "pyproject.toml",
    ".python-version", "install_acl.bat", "Install ACL.bat", "delivery.json", ".env.example", "assets/dashboard.ico",
    "scripts/acl_publish_release.py", "scripts/acl_release_manifest.py", "scripts/acl_sign_release.py",
    "scripts/acl_smoke.py", "scripts/acl_update_gate.py", "scripts/acl_scan_secrets.py", "scripts/acl_ci_scope.py",
    "src/web/acl_update.py", "src/web/release_updates.py", "src/web/release_signing.py", "src/web/machine_paths.py",
])
def test_installer_inputs_need_the_installer_in_the_light_context(path):
    assert touches_installer(path)
    for paths in ([path], ["src/web/app.py", path]):
        assert classify_installer(paths, "pull_request", "stage")["windows"] is True
        assert classify_installer(paths, "push", None, "stage")["windows"] is True


@pytest.mark.parametrize("paths", [
    ["src/web/app.py"], ["src/fb_ads_rpa/page_capture.py", "tests/test_page_capture.py"], ["config/rpa_recipes/a.yaml"],
    ["src/web/templates/report.html", "src/web/static/app.css"], ["knowledge_base/x.md"], ["scripts/task_tracker.py"],
    ["README.md", "docs/a.md"], ["tests/conftest.py"], ["scripts/acl_test_shard.py"], ["sub/requirements.txt"],
])
def test_app_code_alone_does_not_need_the_installer_before_main(paths):
    result = classify_installer(paths, "pull_request", "stage")
    assert result["windows"] is False and result["linux"] is False
    assert classify_installer(paths, "push", None, "stage")["windows"] is False


@pytest.mark.parametrize("event,base,branch", [
    ("pull_request", "main", None), ("pull_request", "master", None), ("pull_request", None, None),
    ("push", None, "main"), ("push", None, "master"), ("push", None, ""),
    ("workflow_dispatch", None, "stage"), ("", None, None),
])
@pytest.mark.parametrize("paths", [[], ["README.md"], ["src/web/app.py"]])
def test_installer_always_runs_on_the_main_release_path(paths, event, base, branch):
    assert classify_installer(paths, event, base, branch)["windows"] is True


def test_installer_without_a_diff_in_the_light_context_runs():
    assert classify_installer([], "pull_request", "stage")["windows"] is True
    assert classify_installer([], "push", None, "stage")["windows"] is True


def test_select_uses_the_event_diff_per_workflow(tmp_path):
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
    code = git("rev-parse", "HEAD")
    (tmp_path / "packaging").mkdir()
    (tmp_path / "packaging" / "x.ps1").write_text("x\n")  # linux-ok: a path for the classifier, never run
    git("add", "packaging/x.ps1"); git("commit", "-qm", "packaging")  # linux-ok: same
    pack = git("rev-parse", "HEAD")
    pr = lambda head, ref="stage": {"pull_request": {"base": {"sha": base, "ref": ref}, "head": {"sha": head}}}
    assert select(tmp_path, "pull_request", pr(code), workflow="windows-installer.yml")["windows"] is False
    assert select(tmp_path, "pull_request", pr(pack), workflow="windows-installer.yml")["windows"] is True
    assert select(tmp_path, "pull_request", pr(code, "main"), workflow="windows-installer.yml")["windows"] is True
    assert select(tmp_path, "pull_request", pr(code), workflow="ci.yml")["python"] == ["3.13"]
    assert select(tmp_path, "pull_request", pr(code, "main"), workflow="ci.yml")["python"] == ["3.11", "3.13"]
    push = {"before": base, "after": code}
    assert select(tmp_path, "push", push, ref="refs/heads/stage", workflow="windows-installer.yml")["windows"] is False
    assert select(tmp_path, "push", push, ref="refs/heads/stage", workflow="ci.yml")["python"] == ["3.13"]
    assert select(tmp_path, "push", push, ref="refs/heads/main", workflow="windows-installer.yml")["windows"] is True
    assert select(tmp_path, "push", push, ref="refs/heads/main", workflow="ci.yml")["python"] == ["3.11", "3.13"]
    # an unreadable diff (new branch: before is all zeros) is never light
    zero = {"before": "0" * 40, "after": code}
    assert select(tmp_path, "push", zero, ref="refs/heads/stage", workflow="windows-installer.yml")["windows"] is True
    assert select(tmp_path, "push", zero, ref="refs/heads/stage", workflow="ci.yml")["python"] == ["3.11", "3.13"]


# --- ACL-1649 review: bundled files and the installer ---------------------------------------------------

import re  # noqa: E402
from pathlib import Path  # noqa: E402

from scripts.acl_ci_scope import BUNDLED_DIRS, BUNDLED_FILES, added_or_deleted_paths, is_bundled  # noqa: E402

SPEC = Path(__file__).resolve().parents[1] / "packaging/windows/acl_windows.spec"


def test_the_bundled_list_covers_every_data_path_of_the_spec():
    text = SPEC.read_text(encoding="utf-8")
    block = text[text.index("datas = ["):text.index("] + playwright_datas")]
    paths = ["/".join(re.findall(r'"([^"]+)"', chunk))
             for chunk in re.findall(r'PROJECT_ROOT((?: / "[^"]+")+)\)', block)]
    assert len(paths) >= 14, paths
    for path in paths:
        assert is_bundled(path) or is_bundled(path + "/x"), path          # a file, or a directory with files under it
    # and nothing in the lists that the spec does not bundle
    for entry in BUNDLED_FILES | set(BUNDLED_DIRS):
        assert entry.rstrip("/") in paths, entry


def test_a_hidden_import_of_the_spec_triggers_the_installer():
    text = SPEC.read_text(encoding="utf-8")
    assert '"scripts.acl_runtime_manager"' in text
    assert touches_installer("scripts/acl_runtime_manager.py") and touches_installer("scripts/acl_smoke.py")


@pytest.mark.parametrize("path", ["config/rpa_recipes/new.yaml", "knowledge_base/new.md", "src/web/static/new.js",
                                  "src/web/templates/new.html", "config/benchmarks.json"])
def test_adding_or_deleting_a_bundled_file_needs_the_installer_but_editing_it_does_not(path):
    assert classify_installer([path], "pull_request", "stage")["windows"] is False             # content edit
    assert classify_installer([path], "pull_request", "stage", structural=[path])["windows"] is True
    assert classify_installer([path], "push", None, "stage", structural=[path])["windows"] is True
    assert classify_installer([path], "pull_request", "stage", structural=["src/web/app.py"])["windows"] is False


def test_added_and_deleted_paths_come_from_the_real_diff(tmp_path):
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=tmp_path, text=True).strip()
    git("init", "-q")
    git("config", "user.name", "CI fixture")
    git("config", "user.email", "ci@example.invalid")
    for name in ("keep.txt", "gone.txt", "edit.txt"):
        (tmp_path / name).write_text("1\n")
    git("add", "keep.txt", "gone.txt", "edit.txt"); git("commit", "-qm", "initial")
    base = git("rev-parse", "HEAD")
    (tmp_path / "gone.txt").rename(tmp_path / "renamed.txt")
    (tmp_path / "edit.txt").write_text("2\n")
    (tmp_path / "new.txt").write_text("1\n")
    git("add", "-A"); git("commit", "-qm", "change")                       # fixture-only temporary repository
    assert sorted(added_or_deleted_paths(tmp_path, base, git("rev-parse", "HEAD"))) == ["gone.txt", "new.txt", "renamed.txt"]
    with pytest.raises(ValueError):
        added_or_deleted_paths(tmp_path, "a", "b")


def test_select_sees_a_new_bundled_file_in_the_pr_diff(tmp_path):
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=tmp_path, text=True).strip()
    git("init", "-q")
    git("config", "user.name", "CI fixture")
    git("config", "user.email", "ci@example.invalid")
    (tmp_path / "knowledge_base").mkdir()
    (tmp_path / "knowledge_base" / "a.md").write_text("1\n")
    git("add", "knowledge_base/a.md"); git("commit", "-qm", "initial")
    base = git("rev-parse", "HEAD")
    (tmp_path / "knowledge_base" / "a.md").write_text("2\n")
    git("commit", "-qam", "edit")
    edit_only = git("rev-parse", "HEAD")
    (tmp_path / "knowledge_base" / "b.md").write_text("1\n")
    git("add", "knowledge_base/b.md"); git("commit", "-qm", "new file")
    added = git("rev-parse", "HEAD")
    pr = lambda head: {"pull_request": {"base": {"sha": base, "ref": "stage"}, "head": {"sha": head}}}
    assert select(tmp_path, "pull_request", pr(edit_only), workflow="windows-installer.yml")["windows"] is False
    result = select(tmp_path, "pull_request", pr(added), workflow="windows-installer.yml")
    assert result["windows"] is True and result["reason"] == "bundled-file-added-or-deleted"
