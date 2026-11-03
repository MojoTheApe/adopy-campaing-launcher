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


def test_tag_check_avoids_gnu_only_regex() -> None:
    assert r"\bsuccess\b" not in _text("Install ACL.command")  # linux-ok: regex word boundary in a shell script, not a Windows path
    assert "grep -Eiwq success" in _text("Install ACL.command")


@pytest.mark.parametrize("name", ["start_dashboard.sh", "install_acl.sh"])
def test_config_dir_capture_takes_the_last_line(name: str) -> None:
    assert "print(get_local_config_dir())\" 2>/dev/null | tail -n 1" in _text(name)


def test_doctor_names_the_installer_of_this_os() -> None:
    assert '"install_acl.sh" if os.name != "nt" else "install_acl.bat"' in _text("scripts/acl_doctor.py")


def test_mac_guide_is_honest() -> None:
    guide = _text("docs/SETUP-MAC.md")
    assert "macOS 13+" not in guide
    assert "does not use it" not in guide
    assert "Open Anyway" in guide
    assert "goes through its local launcher API" not in guide
    assert "launcher API only" not in guide
    assert "Apple Silicon or Intel" not in guide
    assert "campaign builder, updates) is ported" not in guide
    assert "Privacy & Security" in guide


def test_one_file_installer_checks_mac_scripts_exist() -> None:
    lines = _text("Install ACL.command").splitlines()
    text = "\n".join(ln for ln in lines if not ln.lstrip().startswith("#"))
    check = text.index('cat-file -e "$SHA:$mac_script"')
    # проверка идёт до checkout и до переноса, и за ней стоит fail
    assert check < text.index("checkout --quiet --detach")
    assert check < text.index('mv "$CHECKOUT"')
    tail = text[check:check + 400]
    assert "|| fail" in tail and "does not support macOS" in tail
    assert "]         ||" not in text


# --- выбор релиза в "Install ACL.command" -----------------------------------

_GIT = shutil.which("git")
_BASH = shutil.which("bash")
_REPO = "MojoTheApe/adopy-campaing-launcher"
_RUN = f"https://github.com/{_REPO}/actions/runs/123"


def _pick_release_function() -> str:
    text = _text("Install ACL.command")
    start = text.index("pick_release() {")
    end = text.index("\n}\n", start) + 3
    return text[start:end]


def _git(repo: Path, *args: str) -> str:
    env = {"GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
    import os
    out = subprocess.run([_GIT, "-C", str(repo), *args], check=True, capture_output=True,
                         text=True, env={**os.environ, **env})
    return out.stdout.strip()


def _commit(repo: Path, msg: str) -> str:
    _git(repo, "commit", "--allow-empty", "-q", "-m", msg)
    return _git(repo, "rev-parse", "HEAD")


def _tag(repo: Path, name: str, commit: str, note: str, when: int) -> None:
    import os
    date = f"{1700000000 + when} +0000"
    env = {**os.environ, "GIT_COMMITTER_DATE": date, "GIT_COMMITTER_NAME": "T",
           "GIT_COMMITTER_EMAIL": "t@example.invalid"}
    subprocess.run([_GIT, "-C", str(repo), "tag", "-a", name, commit, "-m", note],
                   check=True, capture_output=True, env=env)


def _pick(repo: Path) -> subprocess.CompletedProcess:
    main = _git(repo, "rev-parse", "origin/main^{commit}")
    script = _pick_release_function() + f'\npick_release "{repo.as_posix()}" "{main}" "{_REPO}"\n'
    return subprocess.run([_BASH, "-c", script], capture_output=True, text=True)


@pytest.fixture()
def release_repo(tmp_path: Path):
    if not (_GIT and _BASH):
        pytest.skip("git and bash are required to run the release picker")
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q")
    first = _commit(repo, "released")
    _git(repo, "update-ref", "refs/remotes/origin/main", first)
    return repo, first


def test_pick_release_tag_on_tip(release_repo) -> None:
    repo, first = release_repo
    _tag(repo, "acl-release-1", first, f"CI success {_RUN}", 1)
    result = _pick(repo)
    assert result.returncode == 0 and result.stdout.split() == ["acl-release-1", first]


def test_pick_release_main_ahead_picks_tagged_ancestor(release_repo) -> None:
    repo, first = release_repo
    _tag(repo, "acl-release-1", first, f"CI success {_RUN}", 1)
    tip = _commit(repo, "main moved on")
    _git(repo, "update-ref", "refs/remotes/origin/main", tip)
    result = _pick(repo)
    assert result.returncode == 0 and result.stdout.split() == ["acl-release-1", first]


def test_pick_release_invalid_newer_tag_falls_back_to_older(release_repo) -> None:
    repo, first = release_repo
    _tag(repo, "acl-release-1", first, f"CI success {_RUN}", 1)
    tip = _commit(repo, "main moved on")
    _git(repo, "update-ref", "refs/remotes/origin/main", tip)
    _tag(repo, "acl-release-2", tip, f"Built {_RUN}", 2)  # no success recorded
    result = _pick(repo)
    assert result.returncode == 0 and result.stdout.split() == ["acl-release-1", first]
    # a valid newer tag wins
    _git(repo, "tag", "-d", "acl-release-2")
    _tag(repo, "acl-release-2", tip, f"CI success {_RUN}", 2)
    result = _pick(repo)
    assert result.stdout.split() == ["acl-release-2", tip]


def test_pick_release_ignores_tags_not_reachable_from_main(release_repo) -> None:
    repo, first = release_repo
    side = _commit(repo, "side commit not on main")
    _tag(repo, "acl-release-9", side, f"CI success {_RUN}", 9)
    result = _pick(repo)
    assert result.returncode != 0 and result.stdout.strip() == ""


def test_pick_release_without_tags_fails(release_repo) -> None:
    repo, _ = release_repo
    result = _pick(repo)
    assert result.returncode != 0 and result.stdout.strip() == ""


def test_pick_release_rejects_lightweight_and_foreign_run(release_repo) -> None:
    repo, first = release_repo
    _git(repo, "tag", "acl-release-light", first)
    _tag(repo, "acl-release-foreign", first, "CI success https://github.com/org/repo/actions/runs/1", 5)
    assert _pick(repo).returncode != 0
