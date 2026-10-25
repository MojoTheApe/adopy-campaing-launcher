"""Portable checks of the actual native controller's failure boundaries.

No Windows service, account, task, process or product namespace is touched.
"""
import ast
import copy
import inspect
import json
import subprocess
from types import SimpleNamespace

import pytest

from tests import test_acl2063_two_windows_people as native


def _payload():
    return {"operation": "start", "folder": "acl2063-" + "0" * 32,
            "password": "not-a-real-credential"}


def test_timeout_reports_the_last_safe_com_phase_without_payload(monkeypatch):
    def timeout(command, **kwargs):
        assert kwargs["timeout"] == 30
        assert json.loads(kwargs["input"])["password"] == "not-a-real-credential"
        assert "not-a-real-credential" not in str(command)
        raise subprocess.TimeoutExpired(command, 30, stderr=(
            b"ACL2063_TASK_PHASE:register.begin\n"
            b"untrusted diagnostic: not-a-real-credential\n"))

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(RuntimeError, match=r"start.*30.*register.begin") as caught:
        native.scheduled_tasks(_payload())
    assert "not-a-real-credential" not in str(caught.value)
    frame = caught.value.__traceback__
    while frame:
        if "payload" in frame.tb_frame.f_locals:
            assert "not-a-real-credential" not in repr(frame.tb_frame.f_locals["payload"])
        frame = frame.tb_next


def _controller_cleanup(primary):
    # Execute the real controller's finally block, with only the OS boundary
    # replaced. This catches cleanup masking the original start exception.
    function = ast.parse(inspect.getsource(
        native.test_two_standard_windows_users_share_machine_union_results_stop_and_recovery)).body[0]
    boundary = [node for node in function.body if isinstance(node, ast.Try)][-1]
    body = ast.parse("raise primary" if primary is not None else "pass").body
    program = ast.fix_missing_locations(ast.Module(body=[ast.Try(
        body=body, handlers=[], orelse=[], finalbody=copy.deepcopy(boundary.finalbody))],
        type_ignores=[]))
    events = []

    def cleanup(payload):
        assert payload["operation"] == "cleanup"
        raise RuntimeError("synthetic cleanup detail must not replace the primary")

    context = dict(vars(native), processes=[], tasks_created=True, users=["owned"],
                   created=["owned"], task_folder="acl2063-" + "0" * 32,
                   scheduled_tasks=cleanup, primary=primary,
                   admission_cleanup=SimpleNamespace(close=lambda: events.append("rights restored")),
                   net=SimpleNamespace(NetUserDel=lambda *args: events.append("user removed") or 0),
                   shutil=SimpleNamespace(rmtree=lambda path: events.append("private IPC removed")),
                   ipc="synthetic IPC", machine=SimpleNamespace(exists=lambda: False))
    try:
        exec(compile(program, "<actual native controller cleanup>", "exec"), context)
    finally:
        assert events == ["rights restored", "user removed", "private IPC removed"]


def test_controller_preserves_primary_and_still_attempts_all_owned_cleanup():
    primary = ValueError("original start failure")
    with pytest.raises(ValueError, match="original start failure") as caught:
        _controller_cleanup(primary)
    assert caught.value is primary
    notes = getattr(primary, "__notes__", [])
    assert any("RuntimeError" in note for note in notes)
    assert all("synthetic cleanup detail" not in note for note in notes)


def test_cleanup_failure_without_a_primary_still_fails():
    with pytest.raises(RuntimeError, match="synthetic cleanup detail"):
        _controller_cleanup(None)


def test_start_success_still_requires_real_registered_instance(monkeypatch):
    def complete(command, **kwargs):
        assert kwargs["timeout"] == 30
        assert json.loads(kwargs["input"])["operation"] == "start"
        return SimpleNamespace(returncode=0, stderr="", stdout=json.dumps(
            {"completed": True, "instance": "550e8400-e29b-41d4-a716-446655440000"}))

    monkeypatch.setattr(subprocess, "run", complete)
    assert native.scheduled_tasks(_payload())["completed"] is True


def test_unknown_phase_and_raw_diagnostics_are_not_echoed(monkeypatch):
    def timeout(command, **kwargs):
        raise subprocess.TimeoutExpired(command, 30, stderr=(
            b"ACL2063_TASK_PHASE:not-a-real-credential\n"
            b"ACL2063_TASK_HRESULT:999999999999999999999999\n"))

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(RuntimeError, match="phase=unknown") as caught:
        native.scheduled_tasks(_payload())
    assert "not-a-real-credential" not in str(caught.value)
    assert "999999999999999999999999" not in str(caught.value)


def test_nonzero_exit_retains_only_phase_and_numeric_hresult(monkeypatch):
    def refused(command, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr=(
            "ACL2063_TASK_PHASE:register.begin\n"
            "ACL2063_TASK_HRESULT:-2147024891\n"
            "untrusted diagnostic: not-a-real-credential\n"))

    monkeypatch.setattr(subprocess, "run", refused)
    with pytest.raises(RuntimeError, match=r"register.begin.*HRESULT=-2147024891") as caught:
        native.scheduled_tasks(_payload())
    assert "not-a-real-credential" not in str(caught.value)
