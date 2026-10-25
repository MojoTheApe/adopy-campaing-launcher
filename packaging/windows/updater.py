"""Install a downloaded release and fall back to the previous version (ACL-1240).

Started by the dashboard as ``ACL.exe --apply-update <Setup.exe>
--expect-version <v> --previous-exe <old ACL.exe>`` right before the server
exits. Runs from the OLD version's directory, so the installer never has to
replace a file that is in use: every version lives in its own
``Programs\\ACL\\versions\\<version>`` folder.

1. wait until the old server has stopped (health gone, port free);
2. run the installer silently and wait for it;
3. start the new version and wait until ``/health`` answers with its version;
4. if any step fails, start the previous version again, put the shortcuts and
   ``current.json`` back, and record why.

Every outcome is written to ``%LOCALAPPDATA%\\ACL\\updates\\last_update.json``;
the dashboard shows it once after the restart.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

STOP_TIMEOUT_S = 90
INSTALL_TIMEOUT_S = 15 * 60
START_TIMEOUT_S = 180


class InstallationStillRunning(RuntimeError):
    """Do not launch another version while an installer may still be writing."""


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def own_health(local_acl_dir: Path) -> Callable[[], dict | None]:
    """`/health` МОЕГО дешборда, где бы он ни стоял; None — если он не работает.

    Раньше обновление спрашивало порт 9090. На машине с двумя людьми там мог
    отвечать дешборд соседа: обновление ждало бы, пока остановится чужой, и
    откатывалось по таймауту, а после установки могло принять чужой запуск
    за свой (ACL-1309).
    """
    import local_dashboard

    def probe() -> dict | None:
        found = local_dashboard.find_own(local_acl_dir)
        return found[1] if found else None

    return probe


def _no_health() -> dict | None:
    # Среда без настоящего `health` — ошибка сборки теста, а не «дешборд
    # остановлен»: иначе обновление решило бы, что ждать нечего.
    raise RuntimeError("Environment.health must be provided")


@dataclass
class Environment:
    """Everything the helper touches, replaceable in tests."""

    programs_dir: Path
    updates_dir: Path
    health: Callable[[], dict | None] = _no_health
    run: Callable[..., int] = lambda cmd, timeout: run_command(cmd, timeout)
    start: Callable[[list[str]], object] = lambda cmd: subprocess.Popen(
        cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
        env={**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"})
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    shortcuts: Callable[[Path], None] = lambda exe: repoint_shortcuts(exe)
    log: list[str] = field(default_factory=list)


def _wait(env: Environment, condition: Callable[[], bool], timeout: float) -> bool:
    deadline = env.clock() + timeout
    while env.clock() < deadline:
        if condition():
            return True
        env.sleep(1.0)
    return condition()


def _is_acl(env: Environment, version: str | None = None) -> bool:
    h = env.health()
    return bool(h and h.get("app") == "ACL" and h.get("ok") is True
                and (version is None or h.get("version") == version))


def _record(env: Environment, payload: dict) -> dict:
    env.updates_dir.mkdir(parents=True, exist_ok=True)
    path = env.updates_dir / "last_update.json"
    tmp = path.with_name(".last_update.json.tmp")
    tmp.write_text(json.dumps({**payload, "finished_at": _now(), "log": env.log[-20:]}, indent=2) + "\n",
                   encoding="utf-8")
    os.replace(tmp, path)
    (env.updates_dir / "pending.json").unlink(missing_ok=True)
    return payload


def apply_update(env: Environment, setup: Path, expect_version: str, previous_exe: Path) -> dict:
    previous_version = previous_exe.parent.name
    current_json = env.programs_dir / "current.json"
    saved_current = current_json.read_bytes() if current_json.is_file() else None
    base = {"from": previous_version, "to": expect_version}

    def roll_back(reason: str) -> dict:
        env.log.append(f"rolling back: {reason}")
        restored = True
        try:
            if saved_current is not None:
                current_json.write_bytes(saved_current)
            else:
                current_json.unlink(missing_ok=True)
            env.shortcuts(previous_exe)
        except Exception as exc:
            env.log.append(f"version pointer or shortcuts not restored: {exc}")
            restored = False
        try:
            if not _is_acl(env, previous_version) and env.health() is None:
                env.start([str(previous_exe), "--launcher-test"])
            back = _wait(env, lambda: _is_acl(env, previous_version), START_TIMEOUT_S)
        except Exception as exc:
            env.log.append(f"previous version could not start: {exc}")
            back = False
        return _record(env, {**base, "result": "rolled_back" if back and restored else "rollback_failed",
                             "reason": reason, "previous_running": back})

    env.log.append("waiting for the dashboard to stop")
    if not _wait(env, lambda: env.health() is None, STOP_TIMEOUT_S):
        return _record(env, {**base, "result": "not_started",
                             "reason": "The dashboard did not stop, so nothing was installed."})

    command = [str(setup), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/NOCANCEL",
               f"/LOG={env.updates_dir / f'setup-{expect_version}.log'}"]
    env.log.append(f"installing {setup.name}")
    try:
        code = env.run(command, timeout=INSTALL_TIMEOUT_S)
    except InstallationStillRunning:
        return _record(env, {**base, "result": "rollback_failed", "previous_running": False,
                             "reason": "The installer could not be stopped. Contact support before starting ACL again."})
    except Exception as exc:
        return roll_back(f"The installer could not run: {exc}")
    if code != 0:
        return roll_back(f"The installer stopped with code {code}.")

    new_exe = env.programs_dir / "versions" / expect_version / "ACL.exe"
    if not new_exe.is_file():
        return roll_back(f"The installer finished but {new_exe.name} of {expect_version} is missing.")
    env.log.append(f"starting {expect_version}")
    try:
        env.start([str(new_exe), "--launcher-test"])
    except Exception as exc:
        return roll_back(f"The new version could not start: {exc}")
    if not _wait(env, lambda: _is_acl(env, expect_version), START_TIMEOUT_S):
        stop = [str(new_exe), "--stop-server"]
        try:
            env.run(stop, timeout=30)
        except Exception as exc:
            env.log.append(f"stop of the new version failed: {exc}")
        _wait(env, lambda: env.health() is None, 30)
        return roll_back(f"Version {expect_version} did not start within {START_TIMEOUT_S // 60} minutes.")
    return _record(env, {**base, "result": "updated"})


def repoint_shortcuts(exe: Path) -> None:
    """Point the desktop and Start Menu "Open" shortcuts at ``exe`` (Windows only)."""
    if sys.platform != "win32":
        return
    # Windows known folders include redirected / OneDrive desktops. All ACL
    # launch shortcuts must return to the old executable, not only "Open".
    def quote(value):
        return "'" + str(value).replace("'", "''") + "'"
    script = (
        "$menu = Join-Path ([Environment]::GetFolderPath('Programs')) 'Adopy Campaign Launcher'; "
        "$desktop = Join-Path ([Environment]::GetFolderPath('DesktopDirectory')) 'Adopy Campaign Launcher.lnk'; "
        "$links = @($desktop); if (Test-Path -LiteralPath $menu) { "
        "$links += @(Get-ChildItem -LiteralPath $menu -Filter '*.lnk' | ForEach-Object { $_.FullName }) }; "
        "foreach ($link in $links) { if (Test-Path -LiteralPath $link) { "
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($link); "
        "if ([IO.Path]::GetFileName($s.TargetPath) -eq 'ACL.exe') { "
        f"$s.TargetPath = {quote(exe)}; $s.Save() " + "} } }")
    code = run_command(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script], 60)
    if code:
        raise OSError("Windows could not restore the ACL shortcuts")


def run_command(command: list[str], timeout: float) -> int:
    """Run outside the frozen bundle; stop the installer tree on timeout."""
    import psutil
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.kernel32.SetDllDirectoryW(None)
    try:
        process = psutil.Popen(command, env={**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"},
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            return process.wait(timeout=timeout)
        except psutil.TimeoutExpired:
            try:
                children = process.children(recursive=True)
                for child in reversed(children):
                    try:
                        child.kill()
                    except psutil.NoSuchProcess:
                        pass
                process.kill()
                process.wait(timeout=15)
                _, alive = psutil.wait_procs(children, timeout=15)
                if alive:
                    raise InstallationStillRunning()
            except Exception as exc:
                raise InstallationStillRunning() from exc
            raise TimeoutError("The installer exceeded its time limit and was stopped")
    finally:
        if sys.platform == "win32" and getattr(sys, "_MEIPASS", None):
            ctypes.windll.kernel32.SetDllDirectoryW(str(sys._MEIPASS))


def main(args: list[str], local_acl_dir: Path) -> int:
    try:
        setup = Path(args[args.index("--apply-update") + 1])
        expect = args[args.index("--expect-version") + 1]
        previous = Path(args[args.index("--previous-exe") + 1])
    except (ValueError, IndexError):
        return 2
    if (not re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z._-]{0,63}", expect)
            or previous.resolve() != Path(sys.executable).resolve()
            or setup.resolve().parent != (local_acl_dir / "updates").resolve()
            or not setup.is_file()):
        return 2
    programs = previous.parent.parent.parent  # …\Programs\ACL\versions\<v>\ACL.exe
    env = Environment(programs_dir=programs, updates_dir=local_acl_dir / "updates",
                      health=own_health(local_acl_dir))
    # Браузер не открываем: открытая страница сама ждёт /health и перезагружается.
    result = apply_update(env, setup, expect, previous)
    return 0 if result["result"] == "updated" else 1
