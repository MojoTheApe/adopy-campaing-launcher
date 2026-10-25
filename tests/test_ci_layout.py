"""ACL-1582: ci.yml runs neutral tests on Linux and windows_only tests on Windows.

Static checks of the workflow (PyYAML): the two test jobs, their selection,
their names (the reuse proof and the release gate look jobs up by name) and the
`ACL verification` aggregate over both.
"""
from __future__ import annotations

import itertools
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import acl_ci_reuse as reuse  # noqa: E402
from scripts import acl_ci_scope as scope  # noqa: E402

CI = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
JOBS = CI["jobs"]
# ACL-1649: Linux и Windows решаются отдельно (scope.outputs.linux / scope.outputs.windows).
GATES = {
    "test-linux": ("${{ always() && !cancelled() && (needs.scope.result != 'success' "
                   "|| needs.scope.outputs.linux != 'false') }}"),
    "test-windows": ("${{ always() && !cancelled() && (needs.scope.result != 'success' "
                     "|| needs.scope.outputs.windows != 'false') }}"),
}


def _render(template: str, **matrix) -> str:
    for key, value in matrix.items():
        template = template.replace("${{ matrix.%s }}" % key, str(value))
    return template


def _linux_push_pythons() -> list[str]:
    """ACL-1581: версии Linux-задания приходят из scope; push-прогон и запасной
    список выражения — полный набор, он и обязателен для доказательства."""
    expression = JOBS["test-linux"]["strategy"]["matrix"]["python-version"]
    assert expression == "${{ fromJSON(needs.scope.outputs.python || '[\"3.11\", \"3.13\"]') }}"
    fallback = json.loads(expression.split("|| '")[1].split("')")[0])
    assert fallback == scope.classify([], "push")["python"]
    return fallback


def _run(job: str) -> str:
    return "\n".join(step.get("run", "") for step in JOBS[job]["steps"])


def test_the_old_all_windows_test_job_is_gone():
    assert "test" not in JOBS
    assert set(JOBS) == {"scope", "process-instructions", "test-linux", "test-windows", "admission"}


def test_the_linux_job_runs_every_unmarked_file_on_ubuntu_in_shards():
    job = JOBS["test-linux"]
    assert job["runs-on"] == "ubuntu-latest"
    matrix = job["strategy"]["matrix"]
    assert tuple(_linux_push_pythons()) == reuse.CI_LINUX_PYTHONS
    assert tuple(matrix["shard"]) == reuse.CI_LINUX_SHARDS
    run = _run("test-linux")
    assert f"scripts/acl_test_shard.py --platform linux ${{{{ matrix.shard }}}} {len(reuse.CI_LINUX_SHARDS)}" in run
    assert "python -m playwright install --with-deps chromium" in run
    assert "scripts/ci_retry.sh" in run  # ACL-1710: ограниченный запуск с повторами
    assert "-n auto --dist loadfile" in run


def test_the_windows_job_runs_the_marked_files_the_smoke_set_and_the_app_smoke_check():
    job = JOBS["test-windows"]
    assert job["runs-on"] == "windows-2022"
    guard = next(step for step in job["steps"] if step.get("id") == "verify-windows-host")
    assert guard["shell"] == "pwsh" and not guard.get("continue-on-error")  # linux-ok: inspect YAML text, never execute PowerShell here
    assert "if" not in guard, "The native host check must run whenever the Windows job runs"
    assert "Get-CimInstance -ClassName Win32_OperatingSystem" in guard["run"]
    assert "$os.BuildNumber -ne '20348' -or $os.Caption -notlike '*Windows Server 2022*'" in guard["run"]
    assert "throw " in guard["run"] and "$ErrorActionPreference = 'Stop'" in guard["run"]
    assert job["steps"].index(guard) < next(i for i, step in enumerate(job["steps"]) if step.get("name") == "Install dependencies")
    pythons = tuple(job["strategy"]["matrix"]["python-version"])
    assert pythons == reuse.CI_WINDOWS_PYTHONS and "3.13" in pythons
    run = _run("test-windows")
    assert "scripts/acl_test_shard.py --platform windows 1 1" in run
    assert "python scripts/acl_smoke.py" in run
    assert "python scripts/acl_scan_secrets.py" in run
    assert "python -m playwright install chromium" in run
    assert "scripts/ci_retry.sh" in run  # ACL-1710


@pytest.mark.parametrize("job", ["test-linux", "test-windows"])
def test_both_test_jobs_follow_the_scope_decision(job):
    assert JOBS[job]["needs"] == "scope"
    assert JOBS[job]["if"] == GATES[job]
    assert JOBS[job]["strategy"]["fail-fast"] is False


def test_job_names_are_exactly_the_required_jobs_of_the_reuse_proof():
    linux, windows = JOBS["test-linux"], JOBS["test-windows"]
    lm, wm = linux["strategy"]["matrix"], windows["strategy"]["matrix"]
    names = {_render(linux["name"], **{"python-version": p, "shard": s})
             for p, s in itertools.product(_linux_push_pythons(), lm["shard"])}
    names |= {_render(windows["name"], **{"python-version": p}) for p in wm["python-version"]}
    names.add(JOBS["admission"]["name"])
    assert names == set(reuse.REQUIRED_JOBS["ci.yml"])
    assert all("${{" not in n for n in names)
    assert not any(reuse.LEGACY_CI_JOB.fullmatch(n) for n in names)


def test_acl_verification_aggregates_scope_and_both_test_jobs():
    admission = JOBS["admission"]
    assert admission["name"] == "ACL verification"
    assert admission["if"] == "${{ always() }}"
    assert admission["needs"] == ["scope", "process-instructions", "test-linux", "test-windows"]
    env = admission["steps"][0]["env"]
    assert env["LINUX_TESTS"] == "${{ needs.test-linux.result }}"
    assert env["WINDOWS_TESTS"] == "${{ needs.test-windows.result }}"
    assert env["PROCESS_INSTRUCTIONS"] == "${{ needs.process-instructions.result }}"


def test_the_guard_runs_in_the_fast_scope_job():
    assert "tests/test_acl_test_platform.py" in _run("scope")
    assert "tests/test_ci_layout.py" in _run("scope")


def _admission(**env) -> bool:
    script = JOBS["admission"]["steps"][0]["run"]
    result = subprocess.run(["bash", "-e", "-c", script], env=dict(os.environ, **env), capture_output=True)
    return result.returncode == 0


@pytest.mark.skipif(sys.platform == "win32" or not shutil.which("bash"),  # linux-ok: runs in the Ubuntu scope job
                    reason="the admission script is bash on ubuntu-latest")
def test_acl_verification_fails_closed_unless_both_test_jobs_passed():
    full = {"SCOPE": "success", "PROCESS_INSTRUCTIONS": "success", "WINDOWS": "true", "LINUX": "true", "REUSED": "",
            "LINUX_TESTS": "success", "WINDOWS_TESTS": "success"}
    light = dict(full, WINDOWS="false", LINUX="false", LINUX_TESTS="skipped", WINDOWS_TESTS="skipped")
    only_linux = dict(full, WINDOWS="false", WINDOWS_TESTS="skipped")           # PR into main: 3.11 only
    only_windows = dict(full, LINUX="false", LINUX_TESTS="skipped")
    assert all(_admission(**case) for case in (full, light, only_linux, only_windows))
    for field in ("LINUX_TESTS", "WINDOWS_TESTS"):
        for bad in ("failure", "cancelled", "skipped", ""):
            assert not _admission(**dict(full, **{field: bad})), (field, bad)
        assert not _admission(**dict(light, **{field: "success"})), field   # light run cannot hide a job
        assert not _admission(**dict(light, **{field: "failure"})), field
    for case in (only_linux, only_windows):
        for field in ("LINUX_TESTS", "WINDOWS_TESTS"):
            for bad in ("failure", "cancelled", ""):
                assert not _admission(**dict(case, **{field: bad})), (field, bad)
    assert not _admission(**dict(full, SCOPE="failure"))
    for bad in ("failure", "cancelled", "skipped", ""):
        assert not _admission(**dict(full, PROCESS_INSTRUCTIONS=bad))
        assert not _admission(**dict(light, PROCESS_INSTRUCTIONS=bad))
    assert not _admission(**dict(full, WINDOWS=""))
    assert not _admission(**dict(full, LINUX=""))


def test_chromium_install_is_bounded_in_both_test_jobs():
    # ACL-1710: зависший apt-get update не должен жечь job до отмены GitHub
    assert JOBS["test-linux"]["timeout-minutes"] <= 32
    assert JOBS["test-windows"]["timeout-minutes"] <= 30
    for job in ("test-linux", "test-windows"):
        step = next(s for s in JOBS[job]["steps"] if s.get("name", "").startswith("Install Chromium"))
        assert 0 < step["timeout-minutes"] <= 16
        assert "scripts/ci_retry.sh" in step["run"]


def test_process_instruction_check_requires_actual_fast_tooling_success():
    job = JOBS["process-instructions"]
    assert job["name"] == "Process instructions"
    assert job["needs"] == "scope" and job["if"] == "${{ always() }}"
    assert "python3 scripts/acl_process_instructions.py" in _run("process-instructions")
    assert 'test "$SCOPE" = success' in _run("process-instructions")
