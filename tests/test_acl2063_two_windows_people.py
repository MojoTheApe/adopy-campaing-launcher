"""Actual two-standard-OS-user acceptance of the new machine coordination boundary.

Only reviewed account/task/batch-logon test support is reused from ACL-1309.
No launcher, port registry or other unfinished product code is imported.
"""
from __future__ import annotations
import ctypes
from ctypes import wintypes
from contextlib import ExitStack
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import uuid
from types import SimpleNamespace
import pytest
pytestmark = pytest.mark.windows_only(reason="two real standard Windows users and production ProgramData coordination DACL/locks")

def scheduled_tasks(payload):
    """Use only the test's unique folder; credentials travel over stdin."""
    suffix = payload["folder"].removeprefix("acl2063-")
    assert payload["folder"].startswith("acl2063-")
    assert len(suffix) == 32 and all(c in "0123456789abcdef" for c in suffix)
    script = r"""
$ErrorActionPreference = 'Stop'
$p = [Console]::In.ReadToEnd() | ConvertFrom-Json
$service = New-Object -ComObject 'Schedule.Service'
$service.Connect()
$root = $service.GetFolder('\')
$output = @{completed=$true}
function RegisteredSnapshot($task) {
    $settings = $task.Definition.Settings
    return @{
        path=[string]$task.Path; enabled=[bool]$task.Enabled
        last_run=[string]$task.LastRunTime
        demand_start=[bool]$settings.AllowDemandStart
        run_only_if_idle=[bool]$settings.RunOnlyIfIdle
        run_only_if_network=[bool]$settings.RunOnlyIfNetworkAvailable
        disallow_battery=[bool]$settings.DisallowStartIfOnBatteries
        logon_type=[int]$task.Definition.Principal.LogonType
        run_level=[int]$task.Definition.Principal.RunLevel
    }
}
if ($p.operation -eq 'create') {
    $null = $root.CreateFolder($p.folder, $null)
} elseif ($p.operation -eq 'start') {
    $folder = $service.GetFolder('\' + $p.folder)
    $definition = $service.NewTask(0)
    $definition.Principal.UserId = $p.user
    $definition.Principal.LogonType = 1  # TASK_LOGON_PASSWORD
    $definition.Principal.RunLevel = 0  # TASK_RUNLEVEL_LUA, never Highest
    $definition.Settings.Enabled = $true
    $definition.Settings.AllowDemandStart = $true
    $definition.Settings.DisallowStartIfOnBatteries = $false
    $definition.Settings.StopIfGoingOnBatteries = $false
    $definition.Settings.ExecutionTimeLimit = 'PT6M'
    $definition.Settings.MultipleInstances = 2  # ignore a duplicate demand
    $action = $definition.Actions.Create(0)
    $action.Path = $p.executable
    $action.Arguments = $p.arguments
    $action.WorkingDirectory = $p.directory
    # TASK_CREATE only: never update a task that might belong to someone else.
    $task = $folder.RegisterTaskDefinition($p.name, $definition, 2, $p.user, $p.password, 1, $null)
    $output.registered = RegisteredSnapshot ($folder.GetTask($p.name))
    if (!$output.registered.enabled -or !$output.registered.demand_start -or
        $output.registered.logon_type -ne 1 -or $output.registered.run_level -ne 0) {
        throw ('Registered task differs from the controlled Password/Limited demand: ' +
               ($output.registered | ConvertTo-Json -Compress))
    }
    $instance = $task.Run($null)
    if (!$instance) { throw 'No actual task instance was started' }
    $output.instance = [string]$instance.InstanceGuid
    $output.instance_state = [int]$instance.State
} elseif ($p.operation -eq 'status') {
    $task = $service.GetFolder('\' + $p.folder).GetTask($p.name)
    $output.state = [int]$task.State
    $output.last_result = [int64]$task.LastTaskResult
    $output.registered = RegisteredSnapshot $task
    $output.instances = @($task.GetInstances(0) | ForEach-Object {
        @{guid=[string]$_.InstanceGuid; state=[int]$_.State;
          action=[string]$_.CurrentAction; engine_pid=[int]$_.EnginePID}
    })
    # Read only this test's exact task events. Never enable/change the channel.
    $channel = 'Microsoft-Windows-TaskScheduler/Operational'
    $xpath = "*[EventData[Data[@Name='TaskName']='" + $task.Path + "']]"
    try {
        $output.events = @(Get-WinEvent -LogName $channel -FilterXPath $xpath -MaxEvents 20 -ErrorAction Stop |
            ForEach-Object {
                [xml]$xml = $_.ToXml()
                @{id=[int]$_.Id; time=[string]$_.TimeCreated;
                  data=@($xml.Event.EventData.Data | ForEach-Object {
                      @{name=[string]$_.Name; value=[string]$_.'#text'}
                  })}
            })
    } catch { $output.events_unavailable = [string]$_.Exception.Message }
    # If task history is disabled, preserve any native batch-logon refusal
    # for this uniquely created test account, without reading other accounts.
    $xpath = "*[System[EventID=4625] and EventData[Data[@Name='TargetUserName']='" + $p.name + "']]"
    try {
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
    } catch { $output.logon_failures_unavailable = [string]$_.Exception.Message }
} elseif ($p.operation -eq 'cleanup') {
    $folder = $service.GetFolder('\' + $p.folder)
    foreach ($task in @($folder.GetTasks(1))) {
        if ($p.names -notcontains $task.Name) { throw 'Unexpected task in owned folder' }
        if ($task.State -eq 4 -or $task.State -eq 2) {
            try { $task.Stop(0) }
            catch { if ($_.Exception.HResult -ne -2147216629) { throw } }
        }
        $folder.DeleteTask($task.Name, 0)
    }
    $root.DeleteFolder($p.folder, 0)
} else { throw 'Unknown controlled task operation' }
$output | ConvertTo-Json -Depth 8 -Compress
"""
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                            input=json.dumps(payload), capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, f"controlled task operation failed: {result.stderr}"
    observed = json.loads(result.stdout)
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


def test_two_standard_windows_users_share_machine_union_results_stop_and_recovery():
    from tests.acl2063_windows_batch_logon import WindowsBatchPolicy, admit_owned_batch
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
    users = [f"acl2063{run}{n}" for n in (1, 2)]
    ipc = Path(os.environ["PROGRAMDATA"]) / f"acl2063-ipc-{run}"
    machine = Path(os.environ["PROGRAMDATA"]) / f"acl2063-machine-{run}"
    ipc.mkdir()
    created, processes = [], []
    person_processes = {}
    start_receipts = {}
    batch_admissions = {}
    admission_cleanup = ExitStack()
    task_folder = "acl2063-" + uuid.uuid4().hex
    tasks_created = False

    from tests.acl2063_machine_scenario import MachinePeople
    settings = ipc / "settings.json"
    settings.write_text(json.dumps({"ipc": str(ipc), "machine": str(machine), "run": run, "require_standard": True,
                                   "maps": {users[0]: ["A", "B"], users[1]: ["B", "C"]}}))
    (ipc / "unsupported-permissions").mkdir()
    scenario = MachinePeople(ipc, users, diagnostic=lambda person: scheduled_tasks(
        {"operation": "status", "folder": task_folder, "name": person}))
    try:
        scheduled_tasks({"operation": "create", "folder": task_folder})
        tasks_created = True
        for user in users:
            password = "Aa!" + secrets.token_hex(20)
            info = User(user, password, 0, 1, None, "Ephemeral ACL machine refresh acceptance", 0x10201, None)
            parameter = wintypes.DWORD()
            result = net.NetUserAdd(None, 1, ctypes.byref(info), ctypes.byref(parameter))
            assert result == 0, ("cannot create controlled standard user", result, parameter.value)
            created.append(user)
            member = Member(os.environ["COMPUTERNAME"] + "\\" + user)
            result = net.NetLocalGroupAddMembers(None, users_group, 3, ctypes.byref(member), 1)
            assert result in (0, 1378), ("cannot assign standard Users membership", result)
            policy = admission_cleanup.enter_context(WindowsBatchPolicy(user, password, created))
            batch_admissions[user] = admission_cleanup.enter_context(admit_owned_batch(policy))
            # Grant only owned synthetic IPC, never a product/employee directory.
            subprocess.run(["icacls.exe", str(ipc), "/grant", f"{user}:(OI)(CI)M"],
                           check=True, capture_output=True, timeout=20)
            helper = Path(__file__).with_name("acl2063_machine_person.py")
            arguments = subprocess.list2cmdline([str(helper), str(settings), user, "person"])
            start_receipts[user] = scheduled_tasks({"operation": "start", "folder": task_folder, "name": user,
                             "user": os.environ["COMPUTERNAME"] + "\\" + user,
                             "password": password, "executable": sys.executable,
                             "arguments": arguments, "directory": str(helper.parent)})
        prepared = [scenario.read(user, lambda v: v["phase"] == "prepared") for user in users]
        assert prepared[0]["sid"] != prepared[1]["sid"]
        assert prepared[0]["profile"] != prepared[1]["profile"]
        for user, result in zip(users, prepared):
            assert result["sid"] == batch_admissions[user]["sid"]
            assert result["admin"] is False
            process = retain_person_process(kernel, result, user, helper, settings)
            processes.append(process); person_processes[user] = process
        scenario.exercise()
        for user in users:
            assert scenario.command(user, "deny")["unsupported_permissions_refused"]
        for user, process in zip(users, processes):
            scenario.command(user, "exit")
            assert kernel.WaitForSingleObject(process.process, 30000) == 0
            code = wintypes.DWORD()
            assert kernel.GetExitCodeProcess(process.process, ctypes.byref(code)) and code.value == 0
    finally:
        for process in processes:
            if kernel.WaitForSingleObject(process.process, 15000) != 0:
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
                failures = [(user, net.NetUserDel(None, user)) for user in created]
                shutil.rmtree(ipc)
                if machine.exists():
                    shutil.rmtree(machine)
                assert all(code == 0 for _, code in failures), failures
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as report:
            report.write("\n### ACL-2063: two real standard Windows users — passed\n\n"
                         "Distinct native SIDs, profiles and private configs joined one logical window. "
                         "Union A/B + B/C read three targets once, maximum one actual reader; both local caches "
                         "received their complete lists. Shared checkpoint survived restart and cleared only "
                         "after explicit successful recovery. Private sentinel bytes stayed unchanged. "
                         "Actual new ProgramData coordination permissions supported cross-user atomic replacement; "
                         "unsupported existing permissions refused. Synthetic transport only: no Meta/MLX calls. "
                         "Password/Limited owned tasks and scoped missing batch admission cleaned up.\n")
