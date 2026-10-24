"""User-facing Windows launcher around the reviewed ACL runtime."""
from __future__ import annotations

import errno
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
PORT_BUSY_EXIT_CODE = 73
SERVER_MUTEX_NAME = r"Local\ACL-Campaign-Launcher-Server-v1"
_MUTEX_HANDLE: int | None = None
_CONFIG_LOCK: Any = None


def _bundle_root() -> Path:
    """Return PyInstaller's data directory, or this file's directory in dev."""
    frozen_root = getattr(sys, "_MEIPASS", None)
    return Path(frozen_root) if frozen_root else Path(__file__).resolve().parent


def _local_acl_dir() -> Path:
    """The one settings folder (ACL-1680): the real LOCALAPPDATA/ACL folder, never
    the virtual copy an MSIX-packaged parent such as Claude Desktop hands out."""
    _ensure_repo_importable()
    from config.paths import get_local_config_dir

    folder = Path(get_local_config_dir()).expanduser()
    # An explicit ACL_CONFIG_DIR is canonicalised; the default is used as-is.
    return folder.resolve() if os.environ.get("ACL_CONFIG_DIR", "").strip() else folder


def _ensure_repo_importable() -> None:
    root = str(Path(__file__).resolve().parents[2])
    if root not in sys.path and not getattr(sys, "frozen", False):
        sys.path.insert(0, root)


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


def _installer_presence_mutex(kernel) -> int | None:
    """Keep the existing installer AppMutex alive, independently of ownership.

    Every dashboard in this Windows session holds the same presence marker.
    The shared authenticated-user grant is SYNCHRONIZE only; it grants no
    access to another person's configuration gate. The last dashboard closes it.
    """
    import ctypes
    from ctypes import wintypes

    class SecurityAttributes(ctypes.Structure):
        _fields_ = [("length", wintypes.DWORD), ("descriptor", ctypes.c_void_p),
                    ("inherit", wintypes.BOOL)]

    synchronize = 0x00100000
    kernel.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.OpenMutexW.restype = wintypes.HANDLE
    handle = kernel.OpenMutexW(synchronize, False, SERVER_MUTEX_NAME)
    if handle:
        return int(handle)
    if ctypes.get_last_error() != 2:  # ERROR_FILE_NOT_FOUND only
        return None  # Never recreate or change an inaccessible existing marker.

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    convert = advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.DWORD)]
    convert.restype = wintypes.BOOL
    descriptor = ctypes.c_void_p()
    # The new object's owner/SYSTEM/admin retain creation/control rights.
    # Other authenticated users get only shared presence access, as Inno's
    # OpenMutex(SYNCHRONIZE) requires. No filesystem ACL is changed.
    if not convert("D:(A;;GA;;;SY)(A;;GA;;;BA)(A;;GA;;;OW)(A;;0x00100000;;;AU)",
                   1, ctypes.byref(descriptor), None):
        return None
    try:
        attributes = SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor, False)
        kernel.CreateMutexExW.argtypes = [ctypes.POINTER(SecurityAttributes), wintypes.LPCWSTR,
                                         wintypes.DWORD, wintypes.DWORD]
        kernel.CreateMutexExW.restype = wintypes.HANDLE
        handle = kernel.CreateMutexExW(ctypes.byref(attributes), SERVER_MUTEX_NAME, 0, 0x001F0001)
        if not handle and ctypes.get_last_error() == 5:
            # Another creator may have won since the missing-object read.
            # Its AU grant deliberately cannot satisfy our creation access.
            handle = kernel.OpenMutexW(synchronize, False, SERVER_MUTEX_NAME)
        return int(handle) if handle else None
    finally:
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree.restype = ctypes.c_void_p
        kernel.LocalFree(descriptor)


def _acquire_server_mutex() -> bool:
    """One server/setup per configuration; preserve the fixed installer marker."""
    global _MUTEX_HANDLE, _CONFIG_LOCK
    from config.paths import machine_file_lock

    # Reuse the existing native process lock in the canonical own folder. It
    # covers different Windows sessions without requiring a Global namespace
    # unavailable to Store-context launches. Never wait behind a running server.
    lock = machine_file_lock(_local_acl_dir() / "server-instance.lock", timeout=0)
    try:
        lock.__enter__()
    except (OSError, TimeoutError):
        return False
    try:
        marker = None
        if sys.platform == "win32":
            import ctypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            marker = _installer_presence_mutex(kernel)
            if marker is None:
                # Do not bypass an inaccessible older presence object or
                # start a process invisible to the existing installer guard.
                return False
        _MUTEX_HANDLE = marker
        _CONFIG_LOCK = lock
        return True
    finally:
        if _CONFIG_LOCK is not lock:
            lock.__exit__(None, None, None)


def _release_server_mutex() -> None:
    global _MUTEX_HANDLE, _CONFIG_LOCK
    marker, lock = _MUTEX_HANDLE, _CONFIG_LOCK
    _MUTEX_HANDLE = None
    _CONFIG_LOCK = None
    try:
        if marker and sys.platform == "win32":
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel.CloseHandle(marker)
    finally:
        if lock is not None:
            lock.__exit__(None, None, None)


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

    token = None
    serve = None
    pick_port = None
    try:
        _ensure_server_streams()
        server_args = [arg for arg in args if arg != "--server"]
        if "--no-browser" not in server_args:
            server_args.append("--no-browser")
        port = _configured_port(server_args)
        sys.argv = [sys.argv[0], *server_args]

        # Keep a single actual port per child/state record. If another process
        # binds after selection, the parent retries a different bounded candidate.
        import serve
        pick_port = serve._pick_port
        serve._pick_port = lambda host, preferred, **kw: pick_port(host, preferred, scan=1)
        runtime_workdir = _runtime_workdir()
        os.chdir(runtime_workdir)
        os.environ.setdefault("ACL_RUNTIME_DIR", str(runtime_workdir))
        token = _write_server_state(port)
        try:
            serve.main()
        except SystemExit as exc:
            if exc.code not in (None, 0) and local_dashboard.port_in_use(port):
                return PORT_BUSY_EXIT_CODE
            raise
        except OSError as exc:
            if exc.errno == errno.EADDRINUSE or getattr(exc, "winerror", None) == 10048:
                return PORT_BUSY_EXIT_CODE
            raise
    finally:
        if token is not None:
            _remove_server_state(token)
        if serve is not None and pick_port is not None:
            serve._pick_port = pick_port
        _release_server_mutex()
    return 0


def _executable_command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, str(Path(__file__).resolve())]


def _no_available_port() -> int:
    first, last = local_dashboard.PORTS[0], local_dashboard.PORTS[-1]
    _show_message(
        "Adopy Campaign Manager cannot start",
        f"Ports {first}-{last} are all used by other applications. The launcher did not stop any of them. "
        "Close one of them or contact support, then try again.",
        error=True,
    )
    return 2


def _start_server(port: int) -> subprocess.Popen:
    command = [*_executable_command(), "--server", "--no-browser", "--port", str(port)]
    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    return subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=creationflags,
    )


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
    # падать: встаём рядом. Чужое не останавливаем никогда. Порт — свой,
    # закреплённый за человеком, а не «первый свободный»: иначе кто раньше
    # включил компьютер, тот и на 9090, и адрес меняется день ото дня.
    port = local_dashboard.choose_port(config_dir)
    if port is None:
        return _no_available_port()
    attempted = {port}
    process = _start_server(port)

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
        result = process.poll()
        if result == PORT_BUSY_EXIT_CODE:
            # Never retry the same just-lost candidate, even if its new owner
            # stops before this observation. Keep the original health deadline.
            port = local_dashboard.choose_port(
                config_dir, in_use=lambda candidate: candidate in attempted
                or local_dashboard.port_in_use(candidate),
            )
            if port is None:
                return _no_available_port()
            attempted.add(port)
            process = _start_server(port)
            continue
        if result not in (None, 0):
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


def _prepare_config_dir() -> None:
    """Merge a package copy of the settings into the real folder; never blocks launch."""
    try:
        from config.config_unify import prepare_config_dir

        prepare_config_dir()
    except Exception:
        pass


def main() -> int:
    os.environ["ACL_CONFIG_DIR"] = str(_local_acl_dir())
    _prepare_config_dir()
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
