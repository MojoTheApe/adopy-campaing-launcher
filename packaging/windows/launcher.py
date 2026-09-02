"""User-facing Windows launcher around the frozen ACL pilot runtime."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
import webbrowser
from pathlib import Path
from typing import Any


DEFAULT_PORT = 9090
HEALTH_TIMEOUT_SECONDS = 60
SERVER_MUTEX_NAME = r"Local\ACL-Campaign-Launcher-Server-v1"
_MUTEX_HANDLE: int | None = None


def _bundle_root() -> Path:
    """Return PyInstaller's data directory, or this file's directory in dev."""
    frozen_root = getattr(sys, "_MEIPASS", None)
    return Path(frozen_root) if frozen_root else Path(__file__).resolve().parent


def _local_acl_dir() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    base = Path(local_app_data) if local_app_data else Path.home() / ".local" / "share"
    return base / "ACL"


def _manifest_path() -> Path:
    explicit = os.environ.get("ACL_BUILD_MANIFEST", "").strip()
    return Path(explicit) if explicit else _bundle_root() / "acl-build-manifest.json"


def _version_payload() -> dict[str, Any]:
    return json.loads(_manifest_path().read_text(encoding="utf-8"))


def _version_text() -> str:
    payload = _version_payload()
    return "\n".join(
        (
            f"Adopy Campaign Launcher {payload.get('version', 'unknown')}",
            f"pilot core: {payload.get('core_baseline_sha', 'unknown')}",
            f"installer source: {payload.get('installer_source_sha', 'unknown')}",
        )
    )


def _show_message(title: str, message: str, *, error: bool = False) -> None:
    """Use a native dialog in the windowed build and stderr everywhere else."""
    if sys.platform == "win32":
        try:
            import ctypes

            flags = 0x10 if error else 0x40  # MB_ICONERROR / MB_ICONINFORMATION
            ctypes.windll.user32.MessageBoxW(None, message, title, flags)
            return
        except Exception:
            pass
    stream = sys.stderr if error else sys.stdout
    if stream is not None:
        print(f"{title}: {message}", file=stream)


def _print_version() -> int:
    try:
        message = _version_text()
    except Exception as exc:
        _show_message(
            "Adopy Campaign Launcher version error",
            f"Version metadata is unavailable.\n\n{type(exc).__name__}: {exc}",
            error=True,
        )
        return 1

    if sys.stdout is None:
        _show_message("Adopy Campaign Launcher version", message)
    else:
        print(message)
    return 0


def _runtime_workdir() -> Path:
    """Return a per-user writable directory for relative logs and output."""
    workdir = _local_acl_dir() / "workspace"
    workdir.mkdir(parents=True, exist_ok=True)
    return workdir


def _logs_dir() -> Path:
    path = _local_acl_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _server_state_path() -> Path:
    return _local_acl_dir() / "server.json"


def _configured_port(args: list[str]) -> int:
    try:
        index = args.index("--port")
        return int(args[index + 1])
    except (ValueError, IndexError):
        return DEFAULT_PORT


def _health_payload(port: int) -> dict[str, Any] | None:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/health",
            timeout=1.0,
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return payload if isinstance(payload, dict) else None
    except (OSError, ValueError, urllib.error.URLError):
        return None


def _is_acl_healthy(port: int) -> bool:
    payload = _health_payload(port)
    return bool(payload and payload.get("app") == "ACL" and payload.get("ok") is True)


def _port_is_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _acquire_server_mutex() -> bool:
    """Prevent two packaged ACL server processes from racing on Windows."""
    global _MUTEX_HANDLE
    if sys.platform != "win32":
        return True
    import ctypes

    handle = ctypes.windll.kernel32.CreateMutexW(None, False, SERVER_MUTEX_NAME)
    if not handle:
        return False
    _MUTEX_HANDLE = int(handle)
    return ctypes.windll.kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS


def _write_server_state(port: int) -> str:
    token = uuid.uuid4().hex
    try:
        version = str(_version_payload().get("version", "unknown"))
    except Exception:
        version = "unknown"
    state = {
        "schema": 1,
        "pid": os.getpid(),
        "port": port,
        "executable": str(Path(sys.executable).resolve()),
        "version": version,
        "token": token,
    }
    path = _server_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{token}.tmp")
    temp.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)
    return token


def _remove_server_state(token: str) -> None:
    path = _server_state_path()
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if state.get("token") == token:
            path.unlink(missing_ok=True)
    except Exception:
        pass


def _ensure_server_streams() -> None:
    """The GUI executable has no stdio; route backend output to durable logs."""
    log_dir = _logs_dir()
    if sys.stdout is None:
        sys.stdout = open(log_dir / "server.stdout.log", "a", encoding="utf-8", buffering=1)
    if sys.stderr is None:
        sys.stderr = open(log_dir / "server.stderr.log", "a", encoding="utf-8", buffering=1)


def _run_server(args: list[str]) -> int:
    if not _acquire_server_mutex():
        return 0 if _is_acl_healthy(_configured_port(args)) else 1

    _ensure_server_streams()
    server_args = [arg for arg in args if arg != "--server"]
    if "--no-browser" not in server_args:
        server_args.append("--no-browser")
    port = _configured_port(server_args)
    sys.argv = [sys.argv[0], *server_args]

    from serve import main as serve_main

    runtime_workdir = _runtime_workdir()
    os.chdir(runtime_workdir)
    os.environ.setdefault("ACL_RUNTIME_DIR", str(runtime_workdir))
    token = _write_server_state(port)
    try:
        serve_main()
    finally:
        _remove_server_state(token)
    return 0


def _executable_command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, str(Path(__file__).resolve())]


def _launch_normal(*, open_browser: bool = True) -> int:
    port = DEFAULT_PORT
    url = f"http://127.0.0.1:{port}/"
    if _is_acl_healthy(port):
        if open_browser:
            webbrowser.open(url)
        return 0
    if _port_is_in_use(port):
        _show_message(
            "Adopy Campaign Launcher cannot start",
            f"Port {port} is already used by another application. The launcher did not stop it. "
            "Close that application or contact support, then try again.",
            error=True,
        )
        return 2

    command = [*_executable_command(), "--server", "--no-browser", "--port", str(port)]
    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=creationflags,
    )

    deadline = time.monotonic() + HEALTH_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if _is_acl_healthy(port):
            if open_browser:
                webbrowser.open(url)
            return 0
        if process.poll() is not None:
            break
        time.sleep(0.5)

    _show_message(
        "Adopy Campaign Launcher did not start",
        "The local dashboard could not start. No campaign action was performed.\n\n"
        f"Diagnostics: {_logs_dir()}",
        error=True,
    )
    return 1


def _stop_server() -> int:
    """Stop only the exact ACL executable recorded by this installation."""
    path = _server_state_path()
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        pid = int(state["pid"])
        port = int(state["port"])
        expected_exe = os.path.normcase(str(Path(state["executable"]).resolve()))
        import psutil

        process = psutil.Process(pid)
        actual_exe = os.path.normcase(str(Path(process.exe()).resolve()))
        if actual_exe != expected_exe or not _is_acl_healthy(port):
            return 2
        process.terminate()
        process.wait(timeout=15)
        return 0
    except (FileNotFoundError, ProcessLookupError):
        return 0
    except Exception as exc:
        # psutil.NoSuchProcess is intentionally handled by this broader path:
        # it is harmless but importing psutil in the except clause would make
        # module availability affect launcher import.
        if type(exc).__name__ == "NoSuchProcess":
            return 0
        _show_message("Adopy Campaign Launcher stop failed", f"Could not stop the launcher safely.\n\n{exc}", error=True)
        return 1


def _open_local_directory(name: str) -> int:
    path = _local_acl_dir() / name if name else _local_acl_dir()
    path.mkdir(parents=True, exist_ok=True)
    if hasattr(os, "startfile"):
        os.startfile(path)  # type: ignore[attr-defined]
        return 0
    _show_message("Adopy Campaign Launcher folder", str(path))
    return 0


def main() -> int:
    args = sys.argv[1:]
    if "--acl-version" in args:
        return _print_version()
    if "--smoke-check" in args:
        from scripts.acl_smoke import main as smoke_main

        return smoke_main()
    if "--wizard-smoke-check" in args:
        import tkinter  # noqa: F401
        from machine_config import validate_oauth_client  # noqa: F401
        from setup_wizard import setup_is_complete  # noqa: F401

        if sys.stdout is not None:
            print("Adopy Campaign Launcher Setup import check OK.")
        return 0
    if "--export-diagnostics" in args or "--diagnostics-smoke-check" in args:
        from diagnostics import export_diagnostics

        output = export_diagnostics(_local_acl_dir(), _manifest_path())
        if "--diagnostics-smoke-check" in args and sys.stdout is not None:
            print(f"Adopy Campaign Launcher diagnostic export OK: {output}")
        else:
            _show_message("Adopy Campaign Launcher diagnostics", f"Sanitized diagnostic bundle created:\n\n{output}")
        return 0
    if "--stop-server" in args:
        return _stop_server()
    if "--settings" in args:
        return _open_local_directory("")
    if "--diagnostics" in args:
        return _open_local_directory("diagnostics")
    if "--setup" in args:
        from setup_wizard import run_setup_wizard

        run_setup_wizard(_local_acl_dir(), _version_text())
        return 0
    if "--launcher-test" in args:
        return _launch_normal(open_browser=False)
    if "--server" in args or "--no-browser" in args:
        return _run_server(args)
    if not _is_acl_healthy(DEFAULT_PORT):
        from setup_wizard import run_setup_wizard, setup_is_complete

        if not setup_is_complete(_local_acl_dir()):
            if not run_setup_wizard(_local_acl_dir(), _version_text()):
                return 0
    return _launch_normal()


if __name__ == "__main__":
    raise SystemExit(main())
