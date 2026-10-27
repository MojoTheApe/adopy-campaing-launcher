"""The literal two-real-Windows-account acceptance for dashboard ownership.

Creates only two ephemeral standard local users, with real batch logons and
loaded profiles. Filesystem grants affect only synthetic IPC; scheduled tasks
are test-owned, and the ProgramData reservation tree is unchanged.
Missing runner privileges fail this
required acceptance; there is no environment-dependent success/skip fallback.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from contextlib import ExitStack
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
from types import SimpleNamespace

import pytest

from tests.acl1309_windows_person import write
from tests.synthetic_json_ipc import read_json
from tests.acl2063_task_control import run_task_control, preserve_primary_cleanup, private_payload

pytestmark = pytest.mark.windows_only(
    reason="two real Windows local accounts, native logon/profile/mutex and ProgramData file permissions")


def scheduled_tasks(payload):
    """Use the owned folder and shared redacted marker protocol; secrets use stdin."""
    payload = private_payload(payload)
    suffix = payload["folder"].removeprefix("acl1309-")
    assert payload["folder"].startswith("acl1309-")
    assert len(suffix) == 32 and all(c in "0123456789abcdef" for c in suffix)
    script = r"""
$ErrorActionPreference = 'Stop'
$p = [Console]::In.ReadToEnd() | ConvertFrom-Json
function Phase($name) {
    [Console]::Error.WriteLine('ACL2063_TASK_PHASE:' + $name)
    [Console]::Error.Flush()
}
trap {
    [Console]::Error.WriteLine('ACL2063_TASK_HRESULT:' + [string][int]$_.Exception.HResult)
    [Console]::Error.Flush()
    exit 1
}
Phase 'powershell.begin'
Phase 'service.create.begin'
$service = New-Object -ComObject 'Schedule.Service'
Phase 'service.create.end'
Phase 'service.connect.begin'
$service.Connect()
Phase 'service.connect.end'
Phase 'root.open.begin'
$root = $service.GetFolder('\')
Phase 'root.open.end'
$output = @{completed=$true}
function RegisteredSnapshot($task) {
    Phase 'snapshot.settings.begin'
    $settings = $task.Definition.Settings
    Phase 'snapshot.settings.end'
    $snapshot = @{}
    Phase 'snapshot.path.begin'
    $snapshot.path = [string]$task.Path
    Phase 'snapshot.path.end'
    Phase 'snapshot.enabled.begin'
    $snapshot.enabled = [bool]$task.Enabled
    Phase 'snapshot.enabled.end'
    Phase 'snapshot.last_run.begin'
    $snapshot.last_run = [string]$task.LastRunTime
    Phase 'snapshot.last_run.end'
    Phase 'snapshot.demand_start.begin'
    $snapshot.demand_start = [bool]$settings.AllowDemandStart
    Phase 'snapshot.demand_start.end'
    Phase 'snapshot.run_only_if_idle.begin'
    $snapshot.run_only_if_idle = [bool]$settings.RunOnlyIfIdle
    Phase 'snapshot.run_only_if_idle.end'
    Phase 'snapshot.run_only_if_network.begin'
    $snapshot.run_only_if_network = [bool]$settings.RunOnlyIfNetworkAvailable
    Phase 'snapshot.run_only_if_network.end'
    Phase 'snapshot.disallow_battery.begin'
    $snapshot.disallow_battery = [bool]$settings.DisallowStartIfOnBatteries
    Phase 'snapshot.disallow_battery.end'
    Phase 'snapshot.logon_type.begin'
    $snapshot.logon_type = [int]$task.Definition.Principal.LogonType
    Phase 'snapshot.logon_type.end'
    Phase 'snapshot.run_level.begin'
    $snapshot.run_level = [int]$task.Definition.Principal.RunLevel
    Phase 'snapshot.run_level.end'
    return $snapshot
}
if ($p.operation -eq 'create') {
    Phase 'folder.create.begin'
    $null = $root.CreateFolder($p.folder, $null)
    Phase 'folder.create.end'
} elseif ($p.operation -eq 'start') {
    Phase 'task.folder.begin'
    $folder = $service.GetFolder('\' + $p.folder)
    Phase 'task.folder.end'
    Phase 'task.definition.begin'
    $definition = $service.NewTask(0)
    Phase 'task.definition.end'
    Phase 'definition.principal_userid.begin'
    $definition.Principal.UserId = $p.user
    Phase 'definition.principal_userid.end'
    Phase 'definition.principal_logontype.begin'
    $definition.Principal.LogonType = 1  # TASK_LOGON_PASSWORD
    Phase 'definition.principal_logontype.end'
    Phase 'definition.principal_runlevel.begin'
    $definition.Principal.RunLevel = 0  # TASK_RUNLEVEL_LUA, never Highest
    Phase 'definition.principal_runlevel.end'
    Phase 'definition.settings_enabled.begin'
    $definition.Settings.Enabled = $true
    Phase 'definition.settings_enabled.end'
    Phase 'definition.settings_allowdemandstart.begin'
    $definition.Settings.AllowDemandStart = $true
    Phase 'definition.settings_allowdemandstart.end'
    Phase 'definition.settings_disallowstartifonbatteries.begin'
    $definition.Settings.DisallowStartIfOnBatteries = $false
    Phase 'definition.settings_disallowstartifonbatteries.end'
    Phase 'definition.settings_stopifgoingonbatteries.begin'
    $definition.Settings.StopIfGoingOnBatteries = $false
    Phase 'definition.settings_stopifgoingonbatteries.end'
    Phase 'definition.settings_executiontimelimit.begin'
    $definition.Settings.ExecutionTimeLimit = 'PT6M'
    Phase 'definition.settings_executiontimelimit.end'
    Phase 'definition.settings_multipleinstances.begin'
    $definition.Settings.MultipleInstances = 2  # ignore a duplicate demand
    Phase 'definition.settings_multipleinstances.end'
    Phase 'action.create.begin'
    $action = $definition.Actions.Create(0)
    Phase 'action.create.end'
    Phase 'action.path.begin'
    $action.Path = $p.executable
    Phase 'action.path.end'
    Phase 'action.arguments.begin'
    $action.Arguments = $p.arguments
    Phase 'action.arguments.end'
    Phase 'action.workingdirectory.begin'
    $action.WorkingDirectory = $p.directory
    Phase 'action.workingdirectory.end'
    # TASK_CREATE only: never update a task that might belong to someone else.
    Phase 'register.begin'
    $task = $folder.RegisterTaskDefinition($p.name, $definition, 2, $p.user, $p.password, 1, $null)
    Phase 'register.end'
    Phase 'snapshot.begin'
    $output.registered = RegisteredSnapshot ($folder.GetTask($p.name))
    Phase 'snapshot.end'
    if (!$output.registered.enabled -or !$output.registered.demand_start -or
        $output.registered.logon_type -ne 1 -or $output.registered.run_level -ne 0) {
        throw ('Registered task differs from the controlled Password/Limited demand: ' +
               ($output.registered | ConvertTo-Json -Compress))
    }
    Phase 'run.begin'
    $instance = $task.Run($null)
    Phase 'run.end'
    if (!$instance) { throw 'No actual task instance was started' }
    Phase 'instance.guid.begin'
    $output.instance = [string]$instance.InstanceGuid
    Phase 'instance.guid.end'
    Phase 'instance.state.begin'
    $output.instance_state = [int]$instance.State
    Phase 'instance.state.end'
} elseif ($p.operation -eq 'status') {
    Phase 'status.task.begin'
    $task = $service.GetFolder('\' + $p.folder).GetTask($p.name)
    Phase 'status.task.end'
    Phase 'status.state.begin'
    $output.state = [int]$task.State
    Phase 'status.state.end'
    Phase 'status.result.begin'
    $output.last_result = [int64]$task.LastTaskResult
    Phase 'status.result.end'
    Phase 'status.snapshot.begin'
    $output.registered = RegisteredSnapshot $task
    Phase 'status.snapshot.end'
    Phase 'status.instances.begin'
    $output.instances = @($task.GetInstances(0) | ForEach-Object {
        @{guid=[string]$_.InstanceGuid; state=[int]$_.State;
          action=[string]$_.CurrentAction; engine_pid=[int]$_.EnginePID}
    })
    Phase 'status.instances.end'
    # Read only this test's exact task events. Never enable/change the channel.
    $channel = 'Microsoft-Windows-TaskScheduler/Operational'
    $xpath = "*[EventData[Data[@Name='TaskName']='" + $task.Path + "']]"
    try {
        Phase 'status.events.begin'
        $output.events = @(Get-WinEvent -LogName $channel -FilterXPath $xpath -MaxEvents 20 -ErrorAction Stop |
            ForEach-Object {
                [xml]$xml = $_.ToXml()
                @{id=[int]$_.Id; time=[string]$_.TimeCreated;
                  data=@($xml.Event.EventData.Data | ForEach-Object {
                      @{name=[string]$_.Name; value=[string]$_.'#text'}
                  })}
            })
        Phase 'status.events.end'
    } catch { $output.events_unavailable = [string]$_.Exception.Message }
    # If task history is disabled, preserve any native batch-logon refusal
    # for this uniquely created test account, without reading other accounts.
    $xpath = "*[System[EventID=4625] and EventData[Data[@Name='TargetUserName']='" + $p.name + "']]"
    try {
        Phase 'status.security.begin'
        $output.logon_failures = @(Get-WinEvent -LogName Security -FilterXPath $xpath -MaxEvents 10 -ErrorAction Stop |
            ForEach-Object {
                [xml]$xml = $_.ToXml()
                @{time=[string]$_.TimeCreated;
                  data=@($xml.Event.EventData.Data | Where-Object {
                      $_.Name -in @('Status', 'SubStatus', 'FailureReason', 'LogonType')
                  } | ForEach-Object {
                      @{name=[string]$_.Name; value=[string]$_.'#text'}
                  })}
            })
        Phase 'status.security.end'
    } catch { $output.logon_failures_unavailable = [string]$_.Exception.Message }
} elseif ($p.operation -eq 'cleanup') {
    Phase 'cleanup.folder.begin'
    $folder = $service.GetFolder('\' + $p.folder)
    Phase 'cleanup.folder.end'
    Phase 'cleanup.list.begin'
    $tasks = @($folder.GetTasks(1))
    Phase 'cleanup.list.end'
    foreach ($task in $tasks) {
        if ($p.names -notcontains $task.Name) { throw 'Unexpected task in owned folder' }
        Phase 'cleanup.state.begin'
        $needsStop = ($task.State -eq 4 -or $task.State -eq 2)
        Phase 'cleanup.state.end'
        if ($needsStop) {
            try {
                Phase 'cleanup.stop.begin'
                $task.Stop(0)
                Phase 'cleanup.stop.end'
            }
            catch { if ($_.Exception.HResult -ne -2147216629) { throw } }
        }
        Phase 'cleanup.delete.begin'
        $folder.DeleteTask($task.Name, 0)
        Phase 'cleanup.delete.end'
    }
    Phase 'cleanup.folder_delete.begin'
    $root.DeleteFolder($p.folder, 0)
    Phase 'cleanup.folder_delete.end'
} else { throw 'Unknown controlled task operation' }
Phase 'complete'
$output | ConvertTo-Json -Depth 8 -Compress
"""
    observed = run_task_control(script, payload)
    assert observed["completed"] is True
    if payload["operation"] == "start":
        assert uuid.UUID(observed["instance"]).int != 0, observed
    return observed


def retain_person_process(kernel, identity, user, helper, settings):
    """Fence the handle to the actual created user's exact helper instance."""
    import psutil

    child = psutil.Process(identity["pid"])
    assert child.username().casefold() == identity["username"].casefold()
    assert child.username().casefold().endswith("\\" + user.casefold())
    assert child.create_time() == identity["created_at"]
    assert Path(child.exe()).resolve() == Path(sys.executable).resolve()
    command = child.cmdline()
    assert command[-1] == "person" and str(helper) in command and str(settings) in command
    handle = kernel.OpenProcess(0x00101001, False, identity["pid"])
    assert handle, ("cannot retain owned helper process", ctypes.get_last_error())
    try:
        assert psutil.Process(identity["pid"]).create_time() == identity["created_at"]
    except BaseException:
        kernel.CloseHandle(handle)
        raise
    return SimpleNamespace(process=handle, pid=identity["pid"])


def test_two_real_people_keep_their_dashboard_setup_state_and_stable_ports():
    from tests.acl1309_windows_batch_logon import WindowsBatchPolicy, admit_owned_batch

    assert sys.platform == "win32", "the platform selector must run this acceptance on Windows"
    assert ctypes.windll.shell32.IsUserAnAdmin(), "controlled account creation requires an elevated test runner"
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    net = ctypes.WinDLL("netapi32", use_last_error=True)
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.OpenMutexW.restype = wintypes.HANDLE
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p

    class User(ctypes.Structure):
        _fields_ = [("name", wintypes.LPWSTR), ("password", wintypes.LPWSTR),
                    ("age", wintypes.DWORD), ("privilege", wintypes.DWORD),
                    ("home", wintypes.LPWSTR), ("comment", wintypes.LPWSTR),
                    ("flags", wintypes.DWORD), ("script", wintypes.LPWSTR)]

    class Member(ctypes.Structure):
        _fields_ = [("name", wintypes.LPWSTR)]

    net.NetUserAdd.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p,
                              ctypes.POINTER(wintypes.DWORD)]
    net.NetUserDel.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    net.NetLocalGroupAddMembers.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR,
                                           wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
    advapi.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
    advapi.ConvertStringSidToSidW.restype = wintypes.BOOL
    advapi.LookupAccountSidW.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p, wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD)]
    advapi.LookupAccountSidW.restype = wintypes.BOOL
    # Resolve the standard Users group by its well-known SID, independently
    # of the runner's display language. Never add these accounts to admins.
    sid = ctypes.c_void_p()
    assert advapi.ConvertStringSidToSidW("S-1-5-32-545", ctypes.byref(sid))
    try:
        name_chars, domain_chars, use = wintypes.DWORD(), wintypes.DWORD(), wintypes.DWORD()
        advapi.LookupAccountSidW(None, sid, None, ctypes.byref(name_chars),
                                 None, ctypes.byref(domain_chars), ctypes.byref(use))
        assert name_chars.value > 0
        group, domain = ctypes.create_unicode_buffer(name_chars.value), ctypes.create_unicode_buffer(domain_chars.value)
        assert advapi.LookupAccountSidW(None, sid, group, ctypes.byref(name_chars),
                                        domain, ctypes.byref(domain_chars), ctypes.byref(use))
        users_group = group.value
    finally:
        kernel.LocalFree(sid)
    run = uuid.uuid4().hex[:12]
    users = [f"acl1309{run}{n}" for n in (1, 2)]
    ipc = Path(os.environ["PROGRAMDATA"]) / f"acl1309-ipc-{run}"
    registry = Path(os.environ["PROGRAMDATA"]) / f"acl1309-registry-{run}" / "ports"
    ipc.mkdir()
    created, processes = [], []
    person_processes = {}
    start_receipts = {}
    batch_admissions = {}
    admission_cleanup = ExitStack()
    task_folder = "acl1309-" + uuid.uuid4().hex
    tasks_created = False

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
    manifest = ipc / "acl-build-manifest.json"
    manifest.write_text(json.dumps({"version": "test"}), encoding="utf-8")
    settings = ipc / "settings.json"
    settings.write_text(json.dumps({"ipc": str(ipc), "registry": str(registry),
                                   "ports": ports, "manifest": str(manifest)}), encoding="utf-8")

    def read(person, predicate, timeout=100):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            path = ipc / f"{person}.json"
            if path.exists():
                value = read_json(path)
                assert value.get("phase") != "error", json.dumps(value, indent=2)
                if predicate(value):
                    return value
            process = person_processes.get(person)
            if process and kernel.WaitForSingleObject(process.process, 0) == 0:
                # Exit is observed after the earlier status read: collect the
                # producer's final atomic error, not a stale prepared snapshot.
                if path.exists():
                    final = read_json(path)
                    assert final.get("phase") != "error", json.dumps(final, indent=2)
                code = wintypes.DWORD()
                assert kernel.GetExitCodeProcess(process.process, ctypes.byref(code))
                pytest.fail(f"synthetic Windows process exited before required state: 0x{code.value:08X}")
            time.sleep(0.1)
        task = scheduled_tasks({"operation": "status", "folder": task_folder, "name": person})
        pytest.fail(f"real account did not reach required state: {person}; "
                    f"start={start_receipts.get(person)}; task={task}")

    sequence = 0

    def command(person, action):
        nonlocal sequence
        sequence += 1
        target = ipc / f"{person}.command.json"
        write(target, {"sequence": sequence, "action": action})
        return read(person, lambda v: v.get("sequence") == sequence)

    def installer_still_sees_a_running_dashboard():
        # Check the unchanged session-scoped Inno API from both actual users,
        # including the user whose server was stopped beside a live neighbor.
        for person in users:
            assert command(person, "installer")["installer_checks"] >= 1

    try:
        scheduled_tasks({"operation": "create", "folder": task_folder})
        tasks_created = True
        for user in users:
            password = "Aa!" + secrets.token_hex(20)
            info = User(user, password, 0, 1, None, "Ephemeral ACL dashboard acceptance", 0x10201, None)
            parameter = wintypes.DWORD()
            result = net.NetUserAdd(None, 1, ctypes.byref(info), ctypes.byref(parameter))
            assert result == 0, ("cannot create controlled local account", result, parameter.value)
            created.append(user)
            member = Member(os.environ["COMPUTERNAME"] + "\\" + user)
            result = net.NetLocalGroupAddMembers(None, users_group, 3, ctypes.byref(member), 1)
            assert result in (0, 1378), ("cannot assign standard Users membership", result)
            policy = admission_cleanup.enter_context(WindowsBatchPolicy(user, password, created))
            batch_admissions[user] = admission_cleanup.enter_context(admit_owned_batch(policy))
            # IPC only; the actual shared registry retains ProgramData inheritance.
            subprocess.run(["icacls.exe", str(ipc), "/grant", f"{user}:(OI)(CI)M"],
                           check=True, capture_output=True, timeout=20)
            helper = Path(__file__).with_name("acl1309_windows_person.py")
            arguments = subprocess.list2cmdline([str(helper), str(settings), user, "person"])
            start_receipts[user] = scheduled_tasks({"operation": "start", "folder": task_folder, "name": user,
                             "user": os.environ["COMPUTERNAME"] + "\\" + user,
                             "password": password, "executable": sys.executable,
                             "arguments": arguments, "directory": str(helper.parent)})
            start_receipts[user]["batch_admission"] = batch_admissions[user]
        prepared = [read(user, lambda v: v["phase"] == "prepared") for user in users]
        assert prepared[0]["sid"] != prepared[1]["sid"]
        assert prepared[0]["profile"] != prepared[1]["profile"]
        assert prepared[0]["config"] != prepared[1]["config"]
        assert prepared[0]["session"] == prepared[1]["session"]
        for user, result in zip(users, prepared):
            assert result["sid"] == batch_admissions[user]["sid"]
            assert result["username"].casefold().endswith("\\" + user.casefold())
            process = retain_person_process(kernel, result, user, helper, settings)
            processes.append(process)
            person_processes[user] = process
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
            write(target, {"sequence": sequence + 1, "action": "exit"})
            assert kernel.WaitForSingleObject(process.process, 30000) == 0
            code = wintypes.DWORD()
            assert kernel.GetExitCodeProcess(process.process, ctypes.byref(code))
            assert code.value == 0
    finally:
        with preserve_primary_cleanup(sys.exception()):
            for user in users:
                (ipc / f"{user}.server-stop").touch()
            for process in processes:
                if kernel.WaitForSingleObject(process.process, 25000) != 0:
                    kernel.TerminateProcess(process.process, 1)
                    kernel.WaitForSingleObject(process.process, 10000)
                kernel.CloseHandle(process.process)
            try:
                if tasks_created:
                    scheduled_tasks({"operation": "cleanup", "folder": task_folder, "names": users})
            finally:
                try:
                    admission_cleanup.close()
                finally:
                    foreign.shutdown()
                    foreign.server_close()
                    failures = [(user, net.NetUserDel(None, user)) for user in created]
                    shutil.rmtree(ipc)
                    if registry.parent.exists():
                        shutil.rmtree(registry.parent)
                    assert all(code == 0 for _, code in failures), failures

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        # Publish only anonymous synthetic acceptance facts, after every
        # assertion and owned cleanup succeeds; never profiles, SIDs or users.
        with open(summary, "a", encoding="utf-8") as report:
            report.write(
                "\n### ACL-1309: two real standard Windows accounts â€” passed\n\n"
                "Distinct native users/profiles and own configuration identities; "
                "side-by-side dashboards; repeated shortcut reattach; setup and "
                "stable-port restart beside a live neighbor; actual child PID/port "
                "state cleanup; unchanged installer presence guard; unrelated "
                "listener preserved. Shared ProgramData registry used inherited "
                "permissions. Filesystem grants were confined to test IPC; missing "
                "batch-logon admission was scoped to the two created accounts and "
                "restored without removing any deny. Own scheduled "
                "tasks used real Password logons at Limited run level. "
                "Wizard and HTTP application bodies were synthetic; no live services.\n"
            )
