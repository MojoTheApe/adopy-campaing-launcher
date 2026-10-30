"""macOS/Linux launcher and installer must keep the Windows launcher's guarantees.

start_dashboard.sh / install_acl.sh / Install ACL.command are the Mac
counterparts of the .bat files that tests/test_windows_launcher_contract.py
pins. These are static checks, so they run on every OS (including the Windows
CI job): they read the files, they do not execute them.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS = ["start_dashboard.sh", "install_acl.sh", "Install ACL.command",
            "scripts/install_dashboard_shortcut.sh"]


def _text(name: str) -> str:
    return (_ROOT / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("name", _SCRIPTS)
def test_scripts_are_lf_with_a_bash_shebang(name: str) -> None:
    raw = (_ROOT / name).read_bytes()

    assert raw.startswith(b"#!/usr/bin/env bash\n")
    assert b"\r" not in raw  # CRLF would make bash read `bash\r` and fail on a Mac


@pytest.mark.parametrize("name", _SCRIPTS)
def test_scripts_parse(name: str) -> None:
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash not available")

    result = subprocess.run([bash, "-n", str(_ROOT / name)], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr


def test_launcher_requires_the_isolated_environment() -> None:
    launcher = _text("start_dashboard.sh")

    assert '[ ! -x "$BOOTSTRAP_PY" ]' in launcher
    assert 'BOOTSTRAP_PY=".venv/bin/python"' in launcher
    assert 'PY="python' not in launcher


def test_launcher_never_kills_an_unknown_port_owner() -> None:
    launcher = _text("start_dashboard.sh").lower()

    assert "pkill" not in launcher
    assert "killall" not in launcher
    assert "kill -9" not in launcher
    assert "/health" in launcher
    assert "port $port is used by another application" in launcher


def test_update_uses_only_the_promoted_channel_and_gate() -> None:
    launcher = _text("start_dashboard.sh")

    assert "git fetch --quiet origin main --tags" in launcher
    assert "scripts/acl_update_gate.py --candidate origin/main" in launcher
    assert "git merge --ff-only origin/main" in launcher
    assert "git pull" not in launcher
    assert "@{u}" not in launcher


def test_update_order_matches_the_windows_launcher() -> None:
    """Gate, isolated runtime, pending marker, ff-merge, smoke, activate."""
    launcher = _text("start_dashboard.sh")
    steps = [
        "acl_update_gate.py --candidate origin/main",
        "acl_runtime_manager.py prepare",
        "acl_runtime_manager.py mark-pending",
        "git merge --ff-only origin/main",
        "scripts/acl_smoke.py",
        "acl_runtime_manager.py activate",
    ]

    positions = [launcher.index(step) for step in steps]

    assert positions == sorted(positions)


def test_update_is_opt_in_like_on_windows() -> None:
    launcher = _text("start_dashboard.sh")

    assert "auto_update" in launcher
    assert "update_once" in launcher
    # update_once must be deleted BEFORE the update runs.
    assert launcher.index('rm -f "$CONFIG_DIR/update_once"') < launcher.index("run_update\n")


def test_restart_marker_name_matches_the_dashboard() -> None:
    from src.web import acl_update

    assert acl_update.RESTART_MARKER in _text("start_dashboard.sh")


def test_installer_stays_inside_an_isolated_venv() -> None:
    installer = _text("install_acl.sh")

    assert "python -m venv .venv" not in installer  # always through a discovered 3.11+
    assert '-m venv .venv' in installer
    assert 'PY=".venv/bin/python"' in installer
    assert '"$PY" -m pip install' in installer
    assert "scripts/acl_smoke.py" in installer
    assert "scripts/acl_doctor.py" in installer
    assert "acl_migrate_config.py --provision" in installer


def test_one_file_installer_only_installs_a_tagged_release() -> None:
    installer = _text("Install ACL.command")

    assert "--branch main" in installer
    assert "acl-release-*" in installer
    assert "actions/runs/" in installer
    assert "checkout --quiet --detach" in installer
    assert "MojoTheApe/adopy-campaing-launcher" in installer
    assert "sudo" not in installer


def test_runtime_python_is_bin_python_off_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    sys.path.insert(0, str(_ROOT))
    from scripts import acl_runtime_manager as runtime

    venv = Path("venv-dir")

    monkeypatch.setattr(runtime, "os", types.SimpleNamespace(name="posix"))
    assert runtime._venv_python(venv) == venv / "bin" / "python"

    monkeypatch.setattr(runtime, "os", types.SimpleNamespace(name="nt"))
    assert runtime._venv_python(venv) == venv / "Scripts" / "python.exe"


def test_machine_folder_on_macos(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from config import paths

    monkeypatch.delenv("ACL_CONFIG_DIR", raising=False)
    monkeypatch.setattr(paths, "get_real_local_appdata", lambda: "")  # a Mac has no AppData
    monkeypatch.setattr(paths.sys, "platform", "darwin")
    monkeypatch.setattr(paths.os.path, "expanduser", lambda p: str(tmp_path) if p == "~" else p)

    folder = Path(paths.get_local_config_dir())

    assert folder == tmp_path / "Library" / "Application Support" / "ACL"
    # Outside the checkout, as everywhere else.
    assert _ROOT not in folder.parents
