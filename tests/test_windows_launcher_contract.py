"""Static safety contract for the Windows files used by campaign managers."""
from __future__ import annotations

from pathlib import Path

import pytest

# ACL-1582: файл идёт только в Windows-задание CI; причина — в маркере.
pytestmark = pytest.mark.windows_only(
    reason="Windows launcher and installer contract; runs the PowerShell bootstrap harness")

_ROOT = Path(__file__).resolve().parents[1]


def _text(name: str) -> str:
    return (_ROOT / name).read_text(encoding="utf-8")


def test_launcher_requires_the_isolated_environment() -> None:
    launcher = _text("start_dashboard.bat")

    assert 'if not exist ".venv\\Scripts\\python.exe"' in launcher
    assert 'set "PY=python"' not in launcher


def test_launcher_never_kills_an_unknown_port_owner() -> None:
    launcher = _text("start_dashboard.bat").lower()

    assert "taskkill" not in launcher
    assert "/health" in launcher
    assert "port %port% is used by another application" in launcher


def test_auto_update_uses_only_the_promoted_channel_and_gate() -> None:
    launcher = _text("start_dashboard.bat")

    assert "git fetch --quiet origin main --tags" in launcher
    assert "scripts\\acl_update_gate.py --candidate origin/main" in launcher
    assert "@{u}" not in launcher
    assert "git pull" not in launcher
    assert "git reset --hard" not in launcher


def test_auto_update_uses_sha_versioned_runtime_not_the_working_venv() -> None:
    launcher = _text("start_dashboard.bat")

    assert "scripts\\acl_runtime_manager.py prepare --sha" in launcher
    assert "scripts\\acl_runtime_manager.py mark-pending" in launcher
    assert "scripts\\acl_runtime_manager.py activate --sha" in launcher
    assert "scripts\\acl_runtime_manager.py confirm-running" in launcher
    assert "scripts\\acl_runtime_manager.py recover-pending" in launcher
    assert "%LOCALAPPDATA%\\ACL\\runtimes\\%HEAD_AFTER%" in launcher
    update_block = launcher.split(":update", 1)[1]
    assert '"%PY%" -m pip install' not in update_block
    assert '"%BOOTSTRAP_PY%" -m pip install' not in update_block


def test_failed_candidate_is_not_retried_forever() -> None:
    manager = _text("scripts/acl_runtime_manager.py")

    assert "is_quarantined" in manager
    assert "candidate never reached confirmed ACL health" in manager


def test_installer_does_not_silently_enable_fleet_updates() -> None:
    installer = _text("install_acl.bat")

    assert "auto-update OFF" in installer
    assert "scripts\\acl_smoke.py" in installer
    assert "type nul" not in installer
    assert "scripts\\acl_runtime_manager.py recover-pending" in installer


def test_dashboard_button_update_is_a_one_shot_that_cannot_loop() -> None:
    """The marker is consumed BEFORE the update, not after it succeeds.

    Otherwise a candidate that fails to build would be retried on every single
    start, for the rest of that machine's life, with the buyer watching a minute
    of "checking for updates" before every morning's work.
    """
    launcher = _text("start_dashboard.bat")

    update_once = launcher.index('del "%LOCALAPPDATA%\\ACL\\update_once"')
    assert update_once < launcher.index("call :update")
    assert 'del "%RESTART_MARKER%"' in launcher


def test_dashboard_restart_waits_for_the_old_server_to_stop() -> None:
    """Relaunching too early finds a healthy dashboard and skips the update."""
    launcher = _text("start_dashboard.bat")

    waitdown = launcher.index(":waitdown")
    assert launcher.index('start "" "%~f0"', waitdown) > waitdown
    assert "the dashboard did not stop, so it was not updated" in launcher


def test_only_the_launcher_declares_itself_the_supervisor() -> None:
    """The Update button is offered on the strength of this flag alone."""
    launcher = _text("start_dashboard.bat")

    assert 'set "ACL_SUPERVISED=1"' in launcher
    assert launcher.index('set "ACL_SUPERVISED=1"') < launcher.index("serve.py --port %PORT%")
    assert "ACL_SUPERVISED" not in _text("serve.py")


def test_the_dashboard_never_moves_its_own_working_tree() -> None:
    """Only the launcher updates code, because only it can roll the update back.

    A live server that replaces its own files keeps serving from whatever it
    already imported and loads the new version for anything imported later —
    two releases answering the same page, with no way back.
    """
    module = _text("src/web/acl_update.py")

    for forbidden in ("checkout", "merge", "reset", "pull", "clean", "stash"):
        assert f'"{forbidden}"' not in module, f"the dashboard must not run git {forbidden}"


def test_stable_enrollment_requires_exact_promoted_release() -> None:
    enrollment = _text("enable_stable_updates.bat")

    assert "--candidate origin/main --enroll" in enrollment
    assert "auto_update" in enrollment


def test_the_dashboard_prints_acls_own_log() -> None:
    """A silent run is an undiagnosable one.

    Every RPA step is recorded with log.info(...), and nothing in the server
    process configured logging — so Python's last-resort handler printed
    WARNING and above and dropped the rest. A run started from the launcher
    showed nothing but the browser polling for job status, and when one stalled
    there was no record of what it had been doing.
    """
    import logging

    serve = _text("serve.py")
    assert "_configure_logging()" in serve
    assert serve.index("_configure_logging()") < serve.index("uvicorn.run(")

    namespace: dict = {"logging": logging, "sys": __import__("sys")}
    source = serve[serve.index("def _configure_logging"):serve.index("def main(")]
    exec(compile(source, "serve.py", "exec"), namespace)
    namespace["_configure_logging"]()

    # Ours speaks at INFO; third-party stays at WARNING or the one line that
    # matters is buried under playwright and urllib3.
    assert logging.getLogger("src.fb_ads_rpa.ads_manager_flow").isEnabledFor(logging.INFO)
    assert not logging.getLogger("urllib3.connectionpool").isEnabledFor(logging.INFO)


def test_bootstrap_scenarios_on_windows() -> None:
    import shutil
    import subprocess
    import sys
    import pytest
    if sys.platform != "win32":
        pytest.skip("Standalone bootstrap uses native Windows PowerShell")
    powershell = shutil.which("powershell.exe")
    assert powershell, "Windows PowerShell is required by the installer"
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                             str(root / "tests/bootstrap_windows_harness.ps1"),
                             "-Bootstrap", str(root / "Install ACL.bat")],
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr


def test_installers_do_not_relaunch_themselves() -> None:
    for name in ("Install ACL.bat", "install_acl.bat"):
        assert 'start "" "%~f0"' not in _text(name)
    assert "NEEDRESTART" not in _text("Install ACL.bat")


def test_a_green_install_opens_the_dashboard_itself() -> None:
    """"Start it with start_dashboard.bat" ended the install one step short.

    That step is where a new campaign manager stalls: the installer says ready,
    the screen shows nothing, and the next move is to ask somebody.
    """
    installer = _text("install_acl.bat")

    start = installer.index('start "" "%~dp0start_dashboard.bat"')
    # Only on a green doctor: a failed check must not be followed by a dashboard
    # that half-works, which reads as "it started, so it is fine".
    failure_branch = installer.index("if %RC% NEQ 0")
    assert failure_branch < start
    assert installer.index("exit /b %RC%", failure_branch) < start
