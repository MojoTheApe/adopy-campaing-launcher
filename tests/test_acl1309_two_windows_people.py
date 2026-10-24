"""The literal two-real-Windows-account acceptance for dashboard ownership.

Creates only two ephemeral standard local users, with real primary tokens and
loaded profiles. Permissions are widened only on synthetic IPC, never on the
separate ProgramData reservation tree. Missing runner privileges fail this
required acceptance; there is no environment-dependent success/skip fallback.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import uuid

import pytest

pytestmark = pytest.mark.windows_only(
    reason="two real Windows local accounts, native logon/profile/mutex and ProgramData file permissions")


def test_two_real_people_keep_their_dashboard_setup_state_and_stable_ports():
    assert sys.platform == "win32", "the platform selector must run this acceptance on Windows"
    assert ctypes.windll.shell32.IsUserAnAdmin(), "controlled account creation requires an elevated test runner"
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    net = ctypes.WinDLL("netapi32", use_last_error=True)
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.OpenMutexW.restype = wintypes.HANDLE

    class User(ctypes.Structure):
        _fields_ = [("name", wintypes.LPWSTR), ("password", wintypes.LPWSTR),
                    ("age", wintypes.DWORD), ("privilege", wintypes.DWORD),
                    ("home", wintypes.LPWSTR), ("comment", wintypes.LPWSTR),
                    ("flags", wintypes.DWORD), ("script", wintypes.LPWSTR)]

    class Startup(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("reserved", wintypes.LPWSTR),
                    ("desktop", wintypes.LPWSTR), ("title", wintypes.LPWSTR),
                    *[(n, wintypes.DWORD) for n in ("x", "y", "width", "height", "chars_x", "chars_y", "fill", "flags")],
                    ("show", wintypes.WORD), ("reserved_bytes", wintypes.WORD),
                    ("reserved_ptr", ctypes.POINTER(ctypes.c_byte)),
                    ("stdin", wintypes.HANDLE), ("stdout", wintypes.HANDLE), ("stderr", wintypes.HANDLE)]

    class Process(ctypes.Structure):
        _fields_ = [("process", wintypes.HANDLE), ("thread", wintypes.HANDLE),
                    ("pid", wintypes.DWORD), ("tid", wintypes.DWORD)]

    net.NetUserAdd.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p,
                              ctypes.POINTER(wintypes.DWORD)]
    net.NetUserDel.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    advapi.CreateProcessWithLogonW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR,
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD, ctypes.c_void_p,
        wintypes.LPCWSTR, ctypes.POINTER(Startup), ctypes.POINTER(Process)]
    advapi.CreateProcessWithLogonW.restype = wintypes.BOOL
    run = uuid.uuid4().hex[:12]
    users = [f"acl1309{run}{n}" for n in (1, 2)]
    ipc = Path(os.environ["PROGRAMDATA"]) / f"acl1309-ipc-{run}"
    registry = Path(os.environ["PROGRAMDATA"]) / f"acl1309-registry-{run}" / "ports"
    ipc.mkdir()
    created, processes = [], []
    listeners = []
    ports = []
    # Reserve together so the OS cannot hand the same ephemeral port back.
    for _ in range(5):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        listeners.append(sock)
        ports.append(sock.getsockname()[1])
    for sock in listeners:
        sock.close()

    class Foreign(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"unrelated-test-listener")

        def log_message(self, *args):
            pass

    class ForeignServer(ThreadingHTTPServer):
        allow_reuse_address = False

        def server_bind(self):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            super().server_bind()

    foreign = ForeignServer(("127.0.0.1", ports[0]), Foreign)
    thread = threading.Thread(target=foreign.serve_forever, daemon=True)
    thread.start()
    settings = ipc / "settings.json"
    settings.write_text(json.dumps({"ipc": str(ipc), "registry": str(registry), "ports": ports}), encoding="utf-8")

    def read(person, predicate, timeout=100):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            path = ipc / f"{person}.json"
            if path.exists():
                value = json.loads(path.read_text(encoding="utf-8"))
                assert value.get("phase") != "error", value
                if predicate(value):
                    return value
            time.sleep(0.1)
        pytest.fail(f"real account did not reach required state: {person}")

    sequence = 0

    def command(person, action):
        nonlocal sequence
        sequence += 1
        target = ipc / f"{person}.command.json"
        temp = target.with_suffix(".tmp")
        temp.write_text(json.dumps({"sequence": sequence, "action": action}), encoding="utf-8")
        os.replace(temp, target)
        return read(person, lambda v: v.get("sequence") == sequence)

    def installer_still_sees_a_running_dashboard():
        # The existing Inno Setup AppMutex check uses precisely SYNCHRONIZE.
        marker = kernel.OpenMutexW(0x00100000, False, r"Local\ACL-Campaign-Launcher-Server-v1")
        assert marker, "running dashboard became invisible to the existing installer"
        kernel.CloseHandle(marker)

    try:
        for user in users:
            password = "Aa!" + secrets.token_hex(20)
            info = User(user, password, 0, 1, None, "Ephemeral ACL dashboard acceptance", 0x10001, None)
            parameter = wintypes.DWORD()
            result = net.NetUserAdd(None, 1, ctypes.byref(info), ctypes.byref(parameter))
            assert result == 0, ("cannot create controlled local account", result, parameter.value)
            created.append(user)
            # IPC only; the actual shared registry retains ProgramData inheritance.
            subprocess.run(["icacls.exe", str(ipc), "/grant", f"{user}:(OI)(CI)M"],
                           check=True, capture_output=True, timeout=20)
            startup = Startup()
            startup.cb = ctypes.sizeof(startup)
            process = Process()
            helper = Path(__file__).with_name("acl1309_windows_person.py")
            arguments = subprocess.list2cmdline([sys.executable, str(helper), str(settings), user, "person"])
            ok = advapi.CreateProcessWithLogonW(user, ".", password, 1, sys.executable,
                ctypes.create_unicode_buffer(arguments), 0x08000000, None, str(helper.parent),
                ctypes.byref(startup), ctypes.byref(process))
            assert ok, ("real profile logon failed", ctypes.get_last_error())
            processes.append(process)
            kernel.CloseHandle(process.thread)
        prepared = [read(user, lambda v: v["phase"] == "prepared") for user in users]
        assert prepared[0]["sid"] != prepared[1]["sid"]
        assert prepared[0]["profile"] != prepared[1]["profile"]
        assert prepared[0]["config"] != prepared[1]["config"]
        for user, result in zip(users, prepared):
            assert result["username"].casefold().endswith("\\" + user.casefold())
        (ipc / "start").touch()
        running = [read(user, lambda v: v["phase"] == "running") for user in users]
        assert running[0]["hash"] != running[1]["hash"]
        assert running[0]["port"] != running[1]["port"]
        assert ports[0] not in {r["port"] for r in running}
        marks = [json.loads((registry / f"{r['port']}.json").read_text(encoding="utf-8")) for r in running]
        assert [mark["config"] for mark in marks] == [r["hash"] for r in running]
        installer_still_sees_a_running_dashboard()
        for user in users:
            repeated = command(user, "again")
            assert len(repeated["children"]) == 1 and repeated["wizard_calls"] == 1
        command(users[1], "stop")
        installer_still_sees_a_running_dashboard()
        command(users[1], "setup")  # first person's real server/mutex remains live
        restarted = command(users[1], "launch")
        assert restarted["port"] == running[1]["port"]
        assert command(users[0], "again")["port"] == running[0]["port"]
        with urllib.request.urlopen(f"http://127.0.0.1:{ports[0]}/health", timeout=5) as answer:
            assert answer.read() == b"unrelated-test-listener"
        for user, process in zip(users, processes):
            target = ipc / f"{user}.command.json"
            temp = target.with_suffix(".tmp")
            temp.write_text(json.dumps({"sequence": sequence + 1, "action": "exit"}), encoding="utf-8")
            os.replace(temp, target)
            assert kernel.WaitForSingleObject(process.process, 30000) == 0
            code = wintypes.DWORD()
            assert kernel.GetExitCodeProcess(process.process, ctypes.byref(code))
            assert code.value == 0
    finally:
        for user in users:
            (ipc / f"{user}.server-stop").touch()
        for process in processes:
            if kernel.WaitForSingleObject(process.process, 25000) != 0:
                kernel.TerminateProcess(process.process, 1)
                kernel.WaitForSingleObject(process.process, 10000)
            kernel.CloseHandle(process.process)
        foreign.shutdown()
        foreign.server_close()
        for user in created:
            assert net.NetUserDel(None, user) == 0
        shutil.rmtree(ipc)
        if registry.parent.exists():
            shutil.rmtree(registry.parent)
