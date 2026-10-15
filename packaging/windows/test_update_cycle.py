"""Real two-package update/recovery acceptance on an empty hosted Windows runner.

Both versions are built from the candidate source. No release is published and no
GitHub download channel is exercised. The actual frozen helper, installers,
launchers, health endpoint, version pointer and Windows shortcuts are exercised.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request


ROOT = Path(__file__).resolve().parents[2]
ARTIFACTS = ROOT / "packaging/windows/.artifacts"
# ACL-1581: the whole green update step takes at most 137 s, so a helper that is still
# running after this is hung. Keep the CI step limit (windows-installer.yml) above
# earlier work + stop-server (30 s) + this wait + cleanup (30 s); a test checks it.
HELPER_WAIT_SECONDS = 150


def run(args, *, timeout=180, expected=0, env=None):
    result = subprocess.run([str(x) for x in args], capture_output=True, text=True,
                            timeout=timeout, env=env)
    if result.returncode != expected:
        raise RuntimeError(f"{Path(str(args[0])).name} exited {result.returncode}: "
                           f"{result.stdout[-2000:]} {result.stderr[-2000:]}")
    return result.stdout


def health():
    try:
        with urllib.request.urlopen("http://127.0.0.1:9090/health", timeout=2) as response:
            return json.load(response)
    except (OSError, ValueError, urllib.error.URLError):
        return None


def wait_version(version):
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        value = health()
        if value and value.get("ok") is True and value.get("app") == "ACL" and value.get("version") == version:
            return value
        time.sleep(0.5)
    raise RuntimeError(f"The actual installed version {version} did not become healthy")


def digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def shortcuts():
    script = r"""
    $ErrorActionPreference = 'Stop'
    $menu = Join-Path ([Environment]::GetFolderPath('Programs')) 'Adopy Campaign Launcher'
    $desktop = Join-Path ([Environment]::GetFolderPath('DesktopDirectory')) 'Adopy Campaign Launcher.lnk'
    $paths = @($desktop) + @(Get-ChildItem -LiteralPath $menu -Filter '*.lnk' | ForEach-Object { $_.FullName })
    $shell = New-Object -ComObject WScript.Shell
    $result = @{}
    foreach ($path in $paths) {
        if (-not (Test-Path -LiteralPath $path)) { throw "Missing shortcut: $path" }
        $link = $shell.CreateShortcut($path)
        if ([IO.Path]::GetFileName($link.TargetPath) -eq 'ACL.exe') {
            $result[$path] = @{target=$link.TargetPath; arguments=$link.Arguments}
        }
    }
    ConvertTo-Json -InputObject $result -Compress
    """
    return json.loads(run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script]))


def assert_shortcuts(executable):
    values = shortcuts()
    # Six application Start Menu entries plus the desktop entry; not Uninstall.
    if len(values) != 7 or any(Path(x["target"]).resolve() != executable.resolve() for x in values.values()):
        raise RuntimeError("Not every application shortcut points at the expected installed version")
    return values


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old-version", required=True)
    parser.add_argument("--version", required=True)
    args = parser.parse_args()
    if (sys.platform != "win32" or os.environ.get("GITHUB_ACTIONS") != "true"
            or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted"):
        parser.error("This acceptance harness requires an empty GitHub-hosted Windows runner")
    for version in (args.old_version, args.version):
        if not re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z._-]{0,63}", version):
            parser.error("Invalid test version")
    if args.old_version == args.version:
        parser.error("Two distinct actual package versions are required")

    programs = Path(os.environ["LOCALAPPDATA"]) / "Programs/ACL"
    versions = programs / "versions"
    if (programs / "current.json").exists() or (versions.exists() and any(versions.iterdir())):
        raise RuntimeError("Refusing to replace an existing installation")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 9090))  # Refuse any existing service before mutation.
    logs = ARTIFACTS / "update-cycle-logs"
    config = ARTIFACTS / "update-cycle-config"
    if config.exists():
        raise RuntimeError("A previous test configuration exists; inspect instead of overwriting it")
    logs.mkdir(parents=True, exist_ok=True)
    config.mkdir()
    fixtures = {".env": "ACL_NO_TELEMETRY=1\n",
                "ad_account_map.csv": "account_id,country\n",
                "targeting_registry.json": '{"Local synthetic":{"interests":"Finance","narrow":"Owners"}}',
                "workspace/preservation.txt": "synthetic workbook preservation\n",
                "creatives/preservation.txt": "synthetic creative preservation\n"}
    for name, content in fixtures.items():
        file = config / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(content, encoding="utf-8")
    preserved = {name: digest(config / name) for name in fixtures}
    # Also protect the separate synthetic machine files from test_installer.ps1.
    machine = Path(os.environ["LOCALAPPDATA"]) / "ACL"
    machine_files = {p: digest(p) for p in machine.iterdir() if p.is_file()} if machine.exists() else {}
    env = dict(os.environ, ACL_CONFIG_DIR=str(config), ACL_NO_TELEMETRY="1", ACL_SENTRY_DSN="",
               ACL_RELEASES_REPO=" ", ACL_UPDATE_TOKEN=" ")
    env.pop("ACL_BUILD_MANIFEST", None)  # Identity must come from each installed bundle.
    installers = {v: ARTIFACTS / "installer" / f"Adopy-Campaign-Launcher-Setup-{v}.exe"
                  for v in (args.old_version, args.version)}
    for file in installers.values():
        if not file.is_file():
            raise RuntimeError(f"Test package is missing: {file.name}")
    old_exe = versions / args.old_version / "ACL.exe"
    new_exe = versions / args.version / "ACL.exe"
    report = {"result": "failed", "source_sha": run(["git", "rev-parse", "HEAD"]).strip(),
              "tree_sha": run(["git", "rev-parse", "HEAD^{tree}"]).strip(),
              "run_id": os.environ.get("GITHUB_RUN_ID"), "checks": [],
              "test_packages": {v: {"sha256": digest(p), "size": p.stat().st_size} for v, p in installers.items()},
              "scope": "Same-candidate test versions; real Windows helper/installer/launcher. No published release, download-channel or live Multilogin pilot proof."}
    cleanup_safe = True

    def preserved_data():
        if any(digest(config / name) != value for name, value in preserved.items()):
            raise RuntimeError("Isolated machine/workspace/creative data changed")
        if any(not path.is_file() or digest(path) != value for path, value in machine_files.items()):
            raise RuntimeError("The earlier installer test's machine files changed")

    def start(exe, version):
        run([exe, "--launcher-test"], env=env, timeout=100)
        wait_version(version)

    def apply(previous, installer, expected_version, expected_code):
        nonlocal cleanup_safe
        updates = config / "updates"
        updates.mkdir(exist_ok=True)
        setup = updates / "Setup.exe"
        shutil.copyfile(installer, setup)
        if digest(setup) != digest(installer):
            raise RuntimeError("Copied installer bytes changed")
        receipt = updates / "last_update.json"
        receipt.unlink(missing_ok=True)
        (updates / "pending.json").write_text("{}", encoding="utf-8")
        cleanup_safe = False
        helper = subprocess.Popen([str(previous), "--apply-update", str(setup),
                                   "--expect-version", expected_version, "--previous-exe", str(previous)], env=env)
        try:
            run([previous, "--stop-server"], env=env, timeout=30)
            if helper.wait(timeout=HELPER_WAIT_SECONDS) != expected_code:
                raise RuntimeError("Actual update helper returned an unexpected result")
        finally:
            if helper.poll() is None:
                # An Inno child may outlive its waiting helper. Never start an
                # uninstaller while that child can still be changing files.
                cleanup_safe = False
                import psutil

                parent = psutil.Process(helper.pid)
                parent.suspend()
                children = parent.children(recursive=True)
                for child in children:
                    try:
                        child.suspend()
                    except psutil.NoSuchProcess:
                        pass
                for process in [*reversed(children), parent]:
                    try:
                        process.kill()
                    except psutil.NoSuchProcess:
                        pass
                _, alive = psutil.wait_procs([*children, parent], timeout=15)
                helper.wait(timeout=15)
                if alive:
                    raise RuntimeError("Update descendants still exist; automatic cleanup is unsafe")
                cleanup_safe = True
        result = json.loads(receipt.read_text())
        # In particular rollback_failed can mean a still-running installer;
        # helper exit alone is not sufficient permission to run an uninstaller.
        cleanup_safe = result.get("result") in {"updated", "rolled_back", "not_started"}
        if (updates / "pending.json").exists():
            raise RuntimeError("Update reservation was not cleared")
        preserved_data()
        return result

    try:
        cleanup_safe = False
        run([installers[args.old_version], "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
             "/TASKS=desktopicon", f"/LOG={logs / 'install-old.log'}"], env=env)
        cleanup_safe = True
        start(old_exe, args.old_version)
        assert_shortcuts(old_exe)
        preserved_data()
        report["checks"].append("Previous actual package installs and starts with isolated data")

        result = apply(old_exe, installers[args.version], args.version, 0)
        if result.get("result") != "updated" or result.get("from") != args.old_version or result.get("to") != args.version:
            raise RuntimeError("No genuine successful update receipt")
        wait_version(args.version)
        current = programs / "current.json"
        if json.loads(current.read_text())["version"] != args.version:
            raise RuntimeError("Active-version pointer did not advance")
        saved_pointer = current.read_bytes()
        saved_shortcuts = assert_shortcuts(new_exe)
        report["update"] = result
        report["checks"].append("Frozen helper runs the new installer and starts the new version; pointer and all shortcuts advance")
        run([new_exe, "--stop-server"], env=env, timeout=30)
        start(new_exe, args.version)
        preserved_data()
        report["checks"].append("Updated installed application restarts with preserved machine/workspace/creative data")

        # Run a REAL installer which changes the active pointer/shortcuts, but
        # intentionally cannot supply the requested test version. The unmodified
        # frozen helper must restore the previous working installation itself.
        missing = "missing-test-version"
        result = apply(new_exe, installers[args.old_version], missing, 1)
        expected_reason = f"The installer finished but ACL.exe of {missing} is missing."
        if (result.get("result") != "rolled_back" or result.get("previous_running") is not True
                or result.get("from") != args.version or result.get("to") != missing
                or result.get("reason") != expected_reason):
            raise RuntimeError("Recovery did not follow a successful real installer with a missing expected version")
        setup_log = config / "updates" / f"setup-{missing}.log"
        if not setup_log.is_file() or not setup_log.stat().st_size:
            raise RuntimeError("No real installer execution log for the recovery scenario")
        report["recovery_installer_log_sha256"] = digest(setup_log)
        wait_version(args.version)
        if current.read_bytes() != saved_pointer or shortcuts() != saved_shortcuts:
            raise RuntimeError("Recovery did not restore the exact pointer and shortcut arguments/targets")
        report["recovery"] = result
        report["checks"].append("After an installer/expected-version mismatch, helper restores and starts the previous version and restores all pointers/shortcuts")
        run([new_exe, "--stop-server"], env=env, timeout=30)
        start(new_exe, args.version)
        preserved_data()
        report["checks"].append("Recovered application restarts and synthetic data remains byte-identical")
        report["result"] = "passed"
    except Exception as error:
        report["error"] = str(error)
        raise
    finally:
        try:
            if not cleanup_safe:
                raise RuntimeError("Installer process termination is unverified; leaving the isolated runner for inspection")
            value = health()
            if value:
                if value.get("app") != "ACL" or value.get("version") not in installers:
                    raise RuntimeError("Unexpected service; refusing to stop it")
                run([versions / value["version"] / "ACL.exe", "--stop-server"], env=env, timeout=30)
            uninstall = programs / "unins000.exe"
            if uninstall.exists():
                run([uninstall, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
                     f"/LOG={logs / 'uninstall.log'}"], env=env)
            preserved_data()
        except Exception as error:
            report["result"] = "failed"
            report["cleanup_error"] = str(error)
        finally:
            (logs / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
            for file in (config / "updates").glob("*.log"):
                shutil.copyfile(file, logs / file.name)
        if report["result"] != "passed":
            raise RuntimeError("Update/recovery acceptance failed; inspect update-cycle-logs/report.json")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
