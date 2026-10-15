"""User-facing Windows launcher around the reviewed ACL runtime."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
import webbrowser
from pathlib import Path
from typing import Any


import local_dashboard

DEFAULT_PORT = local_dashboard.DEFAULT_PORT
HEALTH_TIMEOUT_SECONDS = 60
SERVER_MUTEX_NAME = r"Local\ACL-Campaign-Launcher-Server-v1"
_MUTEX_HANDLE: int | None = None


def _bundle_root() -> Path:
    """Return PyInstaller's data directory, or this file's directory in dev."""
    frozen_root = getattr(sys, "_MEIPASS", None)
    return Path(frozen_root) if frozen_root else Path(__file__).resolve().parent


def _local_acl_dir() -> Path:
    explicit = os.environ.get("ACL_CONFIG_DIR", "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
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
            f"Adopy Campaign Manager {payload.get('version', 'unknown')}",
            f"runtime source: {payload.get('core_baseline_sha', 'unknown')}",
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
            "Adopy Campaign Manager version error",
            f"Version metadata is unavailable.\n\n{type(exc).__name__}: {exc}",
            error=True,
        )
        return 1

    if sys.stdout is None:
        _show_message("Adopy Campaign Manager version", message)
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


def _acquire_server_mutex() -> bool:
    """Prevent two packaged ACL server processes from racing on Windows."""
    global _MUTEX_HANDLE
    if sys.platform != "win32":
        return True
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    handle = kernel.CreateMutexW(None, False, SERVER_MUTEX_NAME)
    if not handle:
        return False
    if ctypes.get_last_error() == 183:
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle(handle)
        return False
    _MUTEX_HANDLE = int(handle)
    return True


def _release_server_mutex() -> None:
    global _MUTEX_HANDLE
    if _MUTEX_HANDLE and sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle(_MUTEX_HANDLE)
    _MUTEX_HANDLE = None


def _run_setup() -> bool:
    # Запрещает менять настройку только МОЙ работающий дешборд. Раньше хватало
    # того, что 9090 занят кем угодно, — и второй человек на машине не мог
    # настроить свою копию вовсе: «закройте дешборд», которого у него нет
    # (ACL-1309).
    if local_dashboard.find_own(_local_acl_dir()) is not None or not _acquire_server_mutex():
        _show_message("Setup unavailable", "Close the running ACL dashboard process before changing its settings.", error=True)
        return False
    try:
        from setup_wizard import run_setup_wizard
        return run_setup_wizard(_local_acl_dir(), _version_text())
    finally:
        _release_server_mutex()


def _write_server_state(port: int) -> str:
    token = uuid.uuid4().hex
    try:
        version = str(_version_payload().get("version", "unknown"))
    except Exception:
        version = "unknown"
    import psutil
    state = {
        "schema": 1,
        "created_at": psutil.Process(os.getpid()).create_time(),
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
        return 0  # Another launcher may still be starting; the parent waits for health.

    _ensure_server_streams()
    server_args = [arg for arg in args if arg != "--server"]
    if "--no-browser" not in server_args:
        server_args.append("--no-browser")
    port = _configured_port(server_args)
    sys.argv = [sys.argv[0], *server_args]

    # Bind exactly the requested port; serve.py normally scans alternatives,
    # which would leave server.json and the parent polling the wrong address.
    import serve
    pick_port = serve._pick_port
    serve._pick_port = lambda host, preferred, **kw: pick_port(host, preferred, scan=1)
    serve_main = serve.main

    runtime_workdir = _runtime_workdir()
    os.chdir(runtime_workdir)
    os.environ.setdefault("ACL_RUNTIME_DIR", str(runtime_workdir))
    token = _write_server_state(port)
    try:
        serve_main()
    finally:
        _remove_server_state(token)
        _release_server_mutex()
    return 0


def _executable_command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, str(Path(__file__).resolve())]


def _launch_normal(*, open_browser: bool = True) -> int:
    config_dir = _local_acl_dir()
    # Открываем только свой дешборд. «На 9090 отвечает ACL» — это ещё не мой:
    # на машине с двумя людьми второй попадал в дешборд первого, с чужими
    # аккаунтами и без единого предупреждения (ACL-1309).
    own = local_dashboard.find_own(config_dir)
    if own is not None:
        if open_browser:
            webbrowser.open(f"http://127.0.0.1:{own[0]}/")
        return 0
    # Чужой ACL или посторонняя программа на порту — не наше дело и не повод
    # падать: встаём рядом. Чужое не останавливаем никогда.
    port = local_dashboard.free_port()
    if port is None:
        first, last = local_dashboard.PORTS[0], local_dashboard.PORTS[-1]
        _show_message(
            "Adopy Campaign Manager cannot start",
            f"Ports {first}-{last} are all used by other applications. The launcher did not stop any of them. "
            "Close one of them or contact support, then try again.",
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
        # Ждём СВОЙ дешборд, а не «какой-нибудь на этом порту». Второй запуск
        # того же человека проигрывает мьютекс и выходит, а открыть надо
        # победителя — он может оказаться на другом порту, если тот успел
        # освободиться или заняться между двумя запусками.
        own = local_dashboard.find_own(config_dir)
        if own is not None:
            if open_browser:
                webbrowser.open(f"http://127.0.0.1:{own[0]}/")
            return 0
        if process.poll() not in (None, 0):
            break
        time.sleep(0.5)

    _show_message(
        "Adopy Campaign Manager did not start",
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
        allowed = {os.path.normcase(str((Path(sys.executable).parent / name).resolve()))
                   for name in ("ACL.exe", "ACLConsole.exe")}
        command = process.cmdline()
        if (actual_exe != expected_exe or actual_exe not in allowed
                or abs(process.create_time() - float(state.get("created_at", -1))) > 0.01
                or not any(arg in command for arg in ("--server", "--no-browser"))):
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
        _show_message("Adopy Campaign Manager stop failed", f"Could not stop the launcher safely.\n\n{exc}", error=True)
        return 1


def _open_local_directory(name: str) -> int:
    path = _local_acl_dir() / name if name else _local_acl_dir()
    path.mkdir(parents=True, exist_ok=True)
    if hasattr(os, "startfile"):
        os.startfile(path)  # type: ignore[attr-defined]
        return 0
    _show_message("Adopy Campaign Manager folder", str(path))
    return 0


def main() -> int:
    os.environ["ACL_CONFIG_DIR"] = str(_local_acl_dir().resolve())
    args = sys.argv[1:]
    if "--acl-version" in args:
        return _print_version()
    if "--smoke-check" in args:
        from scripts.acl_smoke import main as smoke_main

        return smoke_main()
    if "--wizard-smoke-check" in args:
        import tkinter  # noqa: F401
        from machine_config import apply_machine_configuration  # noqa: F401
        from setup_wizard import setup_is_complete  # noqa: F401

        if sys.stdout is not None:
            print("Adopy Campaign Manager Setup import check OK.")
        return 0
    if "--export-diagnostics" in args or "--diagnostics-smoke-check" in args:
        from diagnostics import export_diagnostics

        output = export_diagnostics(_local_acl_dir(), _manifest_path())
        if "--diagnostics-smoke-check" in args and sys.stdout is not None:
            print(f"Adopy Campaign Manager diagnostic export OK: {output}")
        else:
            _show_message("Adopy Campaign Manager diagnostics", f"Sanitized diagnostic bundle created:\n\n{output}")
        return 0
    if "--apply-update" in args:
        from updater import main as update_main

        return update_main(args, _local_acl_dir())
    if "--stop-server" in args:
        return _stop_server()
    if "--settings" in args:
        return _open_local_directory("")
    if "--diagnostics" in args:
        return _open_local_directory("diagnostics")
    if "--setup" in args:
        return 0 if _run_setup() else 1
    if "--launcher-test" in args:
        return _launch_normal(open_browser=False)
    if "--server" in args or "--no-browser" in args:
        return _run_server(args)
    # Мастер пропускаем, только если работает МОЙ дешборд. Утром 24.09 на
    # 9090 работал чужой — и свежая установка молча открыла его вместо
    # мастера, со старой версией и чужими данными (ACL-1309).
    if local_dashboard.find_own(_local_acl_dir()) is None:
        from setup_wizard import setup_is_complete

        if not setup_is_complete(_local_acl_dir()):
            if not _run_setup():
                return 0
    return _launch_normal()


if __name__ == "__main__":
    raise SystemExit(main())
