"""Controlled person/server entry point for the two-real-Windows-user check.

The launcher, config identity, registry, mutex, setup gate and server.json are
real. Only the interactive wizard and HTTP application body are synthetic;
no browser, employee settings, ads, Google, Multilogin or telemetry are used.
"""
from __future__ import annotations

import csv
import contextlib
import ctypes
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import traceback


def write(path, value):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value), encoding="utf-8")
    os.replace(temp, path)


def wait_for(path, timeout=90):
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError(path.name)
        time.sleep(0.1)


def observe_launcher_gates(launcher, diagnostics):
    """Retain actual gate outcomes without changing calls, rights or results."""
    import config.paths

    lock = config.paths.machine_file_lock

    @contextlib.contextmanager
    def observed_lock(path, *args, **kwargs):
        if Path(path).name != "server-instance.lock":
            with lock(path, *args, **kwargs) as value:
                yield value
            return
        event = {"acquired": False}
        diagnostics["locks"].append(event)
        try:
            with lock(path, *args, **kwargs) as value:
                event["acquired"] = True
                yield value
        except BaseException as error:
            event.update(exception=type(error).__name__, reason=str(error),
                         errno=getattr(error, "errno", None),
                         winerror=getattr(error, "winerror", None))
            raise

    config.paths.machine_file_lock = observed_lock
    marker = launcher._installer_presence_mutex

    def observed_marker(kernel):
        event = {"called": False, "acquired": False}
        diagnostics["markers"].append(event)
        native = kernel.CreateMutexExW
        previous = native.errcheck

        def checked(result, function, arguments):
            # Capture the original result/error before LocalFree cleanup.
            event.update(called=True, acquired=bool(result), last_error=ctypes.get_last_error())
            return previous(result, function, arguments) if previous else result

        native.errcheck = checked
        try:
            result = marker(kernel)
            if not event["called"]:
                event.update(last_error=ctypes.get_last_error(), stage="security_descriptor")
            return result
        finally:
            if previous is None:
                del native.errcheck
            else:
                native.errcheck = previous

    launcher._installer_presence_mutex = observed_marker
    find_own = launcher.local_dashboard.find_own

    def observed_health(*args, **kwargs):
        result = find_own(*args, **kwargs)
        diagnostics["health"].append({"found": result is not None,
                                      "port": result[0] if result is not None else None})
        return result

    launcher.local_dashboard.find_own = observed_health


def main():
    settings_path, person, mode = sys.argv[1:4]
    settings = json.loads(Path(settings_path).read_text(encoding="utf-8"))
    ipc = Path(settings["ipc"])
    status = ipc / f"{person}.json"
    if mode == "person":
        write(status, {"phase": "boot"})
    # A new real profile supplies its own LOCALAPPDATA/USERPROFILE. Never
    # inherit a runner's ACL_CONFIG_DIR or load a production account's .env.
    os.environ["ACL_CONFIG_DIR"] = ""
    os.environ["ACL_PORT_REGISTRY_DIR"] = settings["registry"]
    os.environ["ACL_BUILD_MANIFEST"] = settings["manifest"]
    for key in ("ACL_SENTRY_DSN", "ACL_LIBRARY_REPO", "ACL_LIBRARY_TOKEN", "ACL_LIBRARY_BRANCH"):
        os.environ[key] = ""
    root = Path(__file__).resolve().parents[1]
    sys.path[:0] = [str(root / "packaging/windows"), str(root)]

    # Any accidental external operation is a failure, not a test integration.
    connect = socket.socket.connect

    def loopback_only(sock, address):
        if not isinstance(address, tuple) or address[0] not in ("127.0.0.1", "::1", "localhost"):
            raise AssertionError("two-user acceptance attempted an external connection")
        return connect(sock, address)

    socket.socket.connect = loopback_only
    import launcher
    import local_dashboard
    import setup_wizard
    import psutil
    import ctypes

    assert not ctypes.windll.shell32.IsUserAnAdmin(), "acceptance must use a standard local account"
    _native_diagnostics["mode"] = mode
    observe_launcher_gates(launcher, _native_diagnostics)

    local_dashboard.PORTS = settings["ports"]
    local = launcher._local_acl_dir()
    assert local == Path(os.environ["LOCALAPPDATA"]) / "ACL"
    assert Path(os.environ["USERPROFILE"]) in local.parents
    local.mkdir(parents=True, exist_ok=True)
    stop = ipc / f"{person}.server-stop"

    if mode == "server":
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from types import SimpleNamespace

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps({"app": "ACL", "ok": True,
                                   "config": local_dashboard.identity(local), "version": "test"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        class Dashboard(ThreadingHTTPServer):
            allow_reuse_address = False

            def server_bind(self):
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                super().server_bind()

        def serve():
            port = launcher._configured_port(sys.argv[1:])
            with Dashboard(("127.0.0.1", port), Handler) as server:
                server.timeout = 0.2
                while not stop.exists():
                    server.handle_request()

        sys.modules["serve"] = SimpleNamespace(main=serve, _pick_port=lambda *a, **kw: None)
        sys.argv = [sys.argv[0], *sys.argv[4:]]
        try:
            return launcher.main()
        finally:
            write(ipc / f"{person}.server-diagnostics.json", _native_diagnostics)

    children = []
    opened = []
    messages = _native_diagnostics["messages"]
    wizard_calls = []
    popen = launcher.subprocess.Popen

    def spawn(*args, **kwargs):
        child = popen(*args, **kwargs)
        if "--server" in args[0]:
            children.append(child)
        return child

    def wizard(directory, version):
        wizard_calls.append(str(directory))
        return True

    launcher.subprocess.Popen = spawn
    launcher.webbrowser.open = lambda url: opened.append(url)
    launcher._show_message = lambda title, message, **kw: messages.append(
        {"title": title, "body": message, "error": bool(kw.get("error"))})
    launcher._executable_command = lambda: [sys.executable, str(Path(__file__).resolve()),
                                           settings_path, person, "server"]
    setup_wizard.run_setup_wizard = wizard
    # The synthetic wizard deliberately leaves setup incomplete: only the
    # actual own-health gate may skip it on the second shortcut invocation.
    assert not setup_wizard.setup_is_complete(local)
    who = subprocess.check_output(["whoami.exe", "/user", "/fo", "csv", "/nh"], text=True).strip()
    sid = next(csv.reader([who]))[1]
    assert sid.startswith("S-1-5-21-")
    write(status, {"phase": "prepared", "username": psutil.Process().username(),
                   "sid": sid, "profile": os.environ["USERPROFILE"], "config": str(local)})
    try:
        wait_for(ipc / "start")
        sys.argv = ["ACL.exe"]
        result = launcher.main()
        own = local_dashboard.find_own(local)
        assert result == 0 and own is not None, (result, messages)
        first_port = own[0]
        first_state = json.loads((local / "server.json").read_text(encoding="utf-8"))
        assert first_state["port"] == first_port
        assert first_state["pid"] == children[0].pid
        assert wizard_calls == [str(local)]

        def snapshot(phase):
            current = local_dashboard.find_own(local)
            return {"phase": phase, "username": psutil.Process().username(), "sid": sid,
                    "profile": os.environ["USERPROFILE"], "config": str(local),
                    "hash": local_dashboard.identity(local), "port": current[0] if current else None,
                    "first_port": first_port, "children": [p.pid for p in children],
                    "wizard_calls": len(wizard_calls), "opened": opened, "messages": messages}

        write(status, snapshot("running"))
        sequence = 0
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            command_path = ipc / f"{person}.command.json"
            if not command_path.exists():
                time.sleep(0.1)
                continue
            command = json.loads(command_path.read_text(encoding="utf-8"))
            if command["sequence"] <= sequence:
                time.sleep(0.1)
                continue
            sequence = command["sequence"]
            action = command["action"]
            if action == "again":
                count = len(children)
                assert launcher.main() == 0
                assert len(children) == count
                assert local_dashboard.find_own(local)[0] == first_port
                assert opened[-1] == f"http://127.0.0.1:{first_port}/"
            elif action == "stop":
                stop.touch()
                children[-1].wait(timeout=20)
                assert children[-1].returncode == 0
                assert not (local / "server.json").exists()
            elif action == "setup":
                checks = {c.label: c for c in setup_wizard.system_readiness(local)}
                assert checks["Local dashboard port"].ok
                assert launcher._run_setup() is True
            elif action == "launch":
                stop.unlink(missing_ok=True)
                assert launcher.main() == 0
                assert local_dashboard.find_own(local)[0] == first_port
                state = json.loads((local / "server.json").read_text(encoding="utf-8"))
                assert state["port"] == first_port and state["pid"] == children[-1].pid
            elif action == "exit":
                break
            else:
                raise AssertionError(action)
            write(status, {**snapshot("running"), "sequence": sequence, "action": action})
        else:
            raise TimeoutError("controller did not finish")
    finally:
        stop.touch()
        for child in children:
            try:
                child.wait(timeout=20)
            except subprocess.TimeoutExpired:
                # Only a process handle this synthetic person's launcher owns.
                child.terminate()
                child.wait(timeout=10)
    return 0


if __name__ == "__main__":
    # launcher.main rewrites sys.argv; retain the producer's IPC context.
    _status_settings, _status_person = sys.argv[1:3]
    _native_diagnostics = {"messages": [], "locks": [], "markers": [], "health": []}
    try:
        raise SystemExit(main())
    except BaseException as error:
        if isinstance(error, SystemExit) and error.code in (None, 0):
            raise
        settings = json.loads(Path(_status_settings).read_text(encoding="utf-8"))
        server_diagnostics = Path(settings["ipc"]) / f"{_status_person}.server-diagnostics.json"
        write(Path(settings["ipc"]) / f"{_status_person}.json",
              {"phase": "error", "traceback": traceback.format_exc(),
               "diagnostics": _native_diagnostics,
               "server_diagnostics": json.loads(server_diagnostics.read_text(encoding="utf-8"))
               if server_diagnostics.exists() else None})
        raise
