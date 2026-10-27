"""Portable observability contracts; these do not reproduce a native COM hang."""
import ast
import copy
import inspect
import json
import re
import subprocess
from types import SimpleNamespace

import pytest
from tests import test_acl1309_two_windows_people as native
from tests.acl2063_task_control import TaskControlError


def payload(operation="start"):
    return {"operation": operation, "folder": "acl1309-" + "0" * 32,
            "password": "synthetic-private-password"}


def script_from_callsite(monkeypatch):
    scripts = []
    def complete(command, **kwargs):
        scripts.append(command[-1])
        return SimpleNamespace(returncode=0, stderr="", stdout=json.dumps(
            {"completed": True, "instance": "550e8400-e29b-41d4-a716-446655440000"}))
    monkeypatch.setattr(subprocess, "run", complete)
    native.scheduled_tasks(payload())
    return scripts[0]


def test_exact_native_callsite_flushes_fixed_begin_end_phases(monkeypatch):
    script = script_from_callsite(monkeypatch)
    assert "[Console]::Error.Flush()" in script
    # Critical independent boundaries must not collapse into a start/receipt label.
    for stage, statement in {
        "service.create": "$service = New-Object -ComObject 'Schedule.Service'",
        "service.connect": "$service.Connect()",
        "root.open": "$root = $service.GetFolder('\\')",
        "register": "$task = $folder.RegisterTaskDefinition($p.name, $definition, 2, $p.user, $p.password, 1, $null)",
        "run": "$instance = $task.Run($null)",
        "instance.guid": "$output.instance = [string]$instance.InstanceGuid",
        "cleanup.delete": "$folder.DeleteTask($task.Name, 0)",
        "cleanup.folder_delete": "$root.DeleteFolder($p.folder, 0)",
    }.items():
        assert re.search(re.escape("Phase '" + stage + ".begin'") + r"\s*" +
                         re.escape(statement) + r"\s*" +
                         re.escape("Phase '" + stage + ".end'"), script), stage
    for line in script.splitlines():
        if line.strip().startswith("Phase "):
            assert re.fullmatch(r"\s*Phase '[a-z_.]+'", line)


def test_every_actual_flushed_phase_survives_timeout_redacted(monkeypatch):
    script = script_from_callsite(monkeypatch)
    phases = re.findall(r"^\s*Phase '([a-z_.]+)'", script, re.M)
    assert len(phases) > 30
    for phase in phases:
        def timeout(command, **kwargs):
            assert kwargs["timeout"] == 30
            assert "synthetic-private-password" not in str(command)
            assert json.loads(kwargs["input"])["password"] == "synthetic-private-password"
            raise subprocess.TimeoutExpired(command, 30, stderr=(
                "ACL2063_TASK_PHASE:powershell.begin\nACL2063_TASK_PHASE:" + phase +
                "\nuntrusted synthetic-private-password\nACL2063_TASK_PHASE:synthetic-private-password\n").encode())
        monkeypatch.setattr(subprocess, "run", timeout)
        with pytest.raises(TaskControlError, match=re.escape("phase=" + phase)) as caught:
            native.scheduled_tasks(payload())
        assert "synthetic-private-password" not in str(caught.value)
        frame = caught.value.__traceback__
        while frame:
            if "payload" in frame.tb_frame.f_locals:
                assert "synthetic-private-password" not in repr(frame.tb_frame.f_locals["payload"])
            frame = frame.tb_next


def actual_cleanup(primary, monkeypatch):
    function = ast.parse(inspect.getsource(
        native.test_two_real_people_keep_their_dashboard_setup_state_and_stable_ports)).body[0]
    boundary = [node for node in function.body if isinstance(node, ast.Try)][-1]
    program = ast.fix_missing_locations(ast.Module(body=[ast.Try(
        body=ast.parse("raise primary" if primary else "pass").body,
        handlers=[], orelse=[], finalbody=copy.deepcopy(boundary.finalbody))], type_ignores=[]))
    events = []
    def timeout(command, **kwargs):
        assert json.loads(kwargs["input"])["operation"] == "cleanup"
        raise subprocess.TimeoutExpired(command, 30, stderr=b"ACL2063_TASK_PHASE:cleanup.list.begin\n")
    monkeypatch.setattr(subprocess, "run", timeout)
    context = dict(vars(native), primary=primary, processes=[], tasks_created=True,
        users=["owned"], created=["owned"], task_folder="acl1309-" + "0" * 32,
        admission_cleanup=SimpleNamespace(close=lambda: events.append("rights")),
        foreign=SimpleNamespace(shutdown=lambda: events.append("shutdown"),
                                server_close=lambda: events.append("server")),
        net=SimpleNamespace(NetUserDel=lambda *args: events.append("user") or 0),
        shutil=SimpleNamespace(rmtree=lambda path: events.append("ipc")),
        registry=SimpleNamespace(parent=SimpleNamespace(exists=lambda: False)))
    class IPC:
        def __truediv__(self, key):
            return SimpleNamespace(touch=lambda: events.append("stop"))
    context["ipc"] = IPC()
    try:
        exec(compile(program, "<actual1309 cleanup>", "exec"), context)
    finally:
        assert events == ["stop", "rights", "shutdown", "server", "user", "ipc"]


def test_actual_cleanup_preserves_primary_and_its_independent_phase(monkeypatch):
    primary = TaskControlError("Controlled task start timed out; phase=instance.guid.begin")
    with pytest.raises(TaskControlError, match="instance.guid.begin") as caught:
        actual_cleanup(primary, monkeypatch)
    assert caught.value is primary
    assert any("cleanup.list.begin" in note for note in primary.__notes__)


def test_cleanup_alone_still_fails(monkeypatch):
    with pytest.raises(TaskControlError, match="cleanup.list.begin"):
        actual_cleanup(None, monkeypatch)


@pytest.mark.parametrize("result, exception", [
    ({"completed": False}, AssertionError),
    ({"completed": True, "instance": "00000000-0000-0000-0000-000000000000"}, AssertionError),
    ({"completed": True, "instance": "not-a-uuid"}, ValueError),
])
def test_observability_keeps_completion_and_instance_assertions(monkeypatch, result, exception):
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs:
                        SimpleNamespace(returncode=0, stderr="", stdout=json.dumps(result)))
    with pytest.raises(exception):
        native.scheduled_tasks(payload())
