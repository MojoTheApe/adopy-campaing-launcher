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


def test_installer_does_not_silently_enable_fleet_updates() -> None:
    installer = _text("install_acl.bat")

    assert "auto-update OFF" in installer
    assert "scripts\\acl_smoke.py" in installer
    assert "type nul" not in installer


def test_stable_enrollment_requires_exact_promoted_release() -> None:
    enrollment = _text("enable_stable_updates.bat")

    assert "--candidate origin/master --enroll" in enrollment
    assert "auto_update" in enrollment
