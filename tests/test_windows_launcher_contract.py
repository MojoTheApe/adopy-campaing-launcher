"""Static safety contract for the Windows files used by campaign managers."""
from __future__ import annotations

from pathlib import Path

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

    assert "git fetch --quiet origin master --tags" in launcher
    assert "scripts\\acl_update_gate.py --candidate origin/master" in launcher
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

    assert "--candidate origin/master --enroll" in enrollment
    assert "auto_update" in enrollment
