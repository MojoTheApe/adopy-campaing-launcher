"""Synthetic status/command transport tolerates a briefly open reader handle.

The real ordinary-user acceptance remains in the two Windows-only suites.
These deterministic cases exercise their actual writers without racing timers.
"""
from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from config import paths
from tests.acl2063_machine_scenario import MachinePeople
from tests import synthetic_json_ipc as ipc


@pytest.fixture(params=["acl1309_windows_person", "acl2063_machine_person", "machine_command"])
def writer(request, tmp_path):
    target = tmp_path / "person.command.json"
    if request.param == "machine_command":
        scenario = MachinePeople(tmp_path, ["person"])
        return target, lambda: scenario.send("person", "state"), {"sequence": 1, "action": "state"}
    write = importlib.import_module("tests." + request.param).write
    value = {"phase": "boot", "sequence": 1, "detail": "complete JSON"}
    return target, lambda: write(target, value), value


def test_reader_releases_the_file_and_complete_json_is_replaced(monkeypatch, writer):
    target, publish, value = writer
    previous = b'{"sequence": 0}'
    target.write_bytes(previous)
    replace = os.replace
    attempts, pauses = [], []

    def busy_then_free(temp, destination):
        attempts.append((temp, destination))
        assert destination.read_bytes() == previous
        assert json.loads(temp.read_text(encoding="utf-8")) == value
        if len(attempts) <= 3:
            raise PermissionError(5, "synthetic reader still holds the destination")
        replace(temp, destination)

    monkeypatch.setattr(paths.os, "replace", busy_then_free)
    monkeypatch.setattr(paths.time, "sleep", pauses.append)
    publish()

    assert json.loads(target.read_text(encoding="utf-8")) == value
    assert len(attempts) == 4
    assert pauses == [paths._REPLACE_PAUSE] * 3
    assert not target.with_suffix(".tmp").exists()


def test_permanent_denial_is_bounded_and_previous_json_survives(monkeypatch, writer):
    target, publish, value = writer
    previous = b'{"sequence": 0}'
    target.write_bytes(previous)
    attempts, pauses = [], []
    denied = PermissionError(5, "synthetic permanent denial")

    def held(temp, destination):
        attempts.append((temp, destination))
        assert destination.read_bytes() == previous
        assert json.loads(temp.read_text(encoding="utf-8")) == value
        raise denied

    monkeypatch.setattr(paths.os, "replace", held)
    monkeypatch.setattr(paths.time, "sleep", pauses.append)
    with pytest.raises(PermissionError) as error:
        publish()

    assert error.value is denied
    assert len(attempts) == paths._REPLACE_ATTEMPTS == 10
    assert pauses == [paths._REPLACE_PAUSE] * 9
    assert sum(pauses) < 0.5
    assert target.read_bytes() == previous


def test_unrelated_error_is_immediate(monkeypatch, writer):
    target, publish, _value = writer
    previous = b'{"sequence": 0}'
    target.write_bytes(previous)
    attempts, pauses = [], []
    full = OSError(28, "synthetic full disk")

    def broken(temp, destination):
        attempts.append((temp, destination))
        raise full

    monkeypatch.setattr(paths.os, "replace", broken)
    monkeypatch.setattr(paths.time, "sleep", pauses.append)
    with pytest.raises(OSError) as error:
        publish()

    assert error.value is full
    assert len(attempts) == 1 and pauses == []
    assert target.read_bytes() == previous


@pytest.mark.parametrize("helper", ["acl1309_windows_person.py", "acl2063_machine_person.py"])
def test_standalone_helper_can_publish_early_boot_outside_repo(tmp_path, helper):
    script = Path(__file__).with_name(helper)
    target = tmp_path / "boot.json"
    # An isolated interpreter has neither the repo nor tests/ on sys.path.
    # Execute definitions only; do not launch real users or the application.
    code = """
import runpy, sys
from pathlib import Path
namespace = runpy.run_path(sys.argv[1], run_name='isolated_fixture')
namespace['write'](Path(sys.argv[2]), {'phase': 'boot'})
"""
    result = subprocess.run([sys.executable, "-I", "-c", code, str(script), str(target)],
                            cwd=tmp_path, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert json.loads(target.read_text(encoding="utf-8")) == {"phase": "boot"}


@pytest.fixture(params=["direct", "machine_status"])
def reader(request, tmp_path):
    target = tmp_path / "person.json"
    value = {"phase": "ready", "sequence": 7, "action": "state"}
    target.write_text(json.dumps(value), encoding="utf-8")
    if request.param == "machine_status":
        scenario = MachinePeople(tmp_path, ["person"])
        scenario.sequence = 7
        return target, lambda: scenario.read("person", lambda v: v["phase"] == "ready"), value
    return target, lambda: ipc.read_json(target), value


def test_reader_recovers_and_returns_actual_json(monkeypatch, reader):
    target, consume, value = reader
    read_text, attempts, pauses = Path.read_text, [], []

    def briefly_denied(path, *args, **kwargs):
        if path == target:
            attempts.append(path)
            if len(attempts) <= 3:
                raise PermissionError(13, "synthetic replacement still holds the name")
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", briefly_denied)
    monkeypatch.setattr(ipc.time, "sleep", pauses.append)
    assert consume() == value
    assert len(attempts) == 4 and pauses == [ipc._READ_PAUSE] * 3


def test_reader_permanent_denial_never_returns_an_earlier_good_status(monkeypatch, reader):
    target, consume, value = reader
    assert consume() == value  # a prior valid snapshot must never become fallback
    attempts, pauses = [], []
    denied = [PermissionError(13, f"synthetic denial {n}") for n in range(10)]

    def held(path, *args, **kwargs):
        assert path == target
        attempts.append(path)
        raise denied[len(attempts) - 1]

    monkeypatch.setattr(Path, "read_text", held)
    monkeypatch.setattr(ipc.time, "sleep", pauses.append)
    with pytest.raises(PermissionError) as error:
        consume()
    assert error.value is denied[-1]
    assert len(attempts) == ipc._READ_ATTEMPTS == 10
    assert pauses == [ipc._READ_PAUSE] * 9 and sum(pauses) < 0.5
    assert json.loads(target.read_bytes()) == value


@pytest.mark.parametrize("failure", [FileNotFoundError(2, "synthetic missing file"),
                                    OSError(28, "synthetic full disk")])
def test_reader_other_errors_are_immediate(monkeypatch, reader, failure):
    target, consume, _value = reader
    attempts, pauses = [], []

    def broken(path, *args, **kwargs):
        assert path == target
        attempts.append(path)
        raise failure

    monkeypatch.setattr(Path, "read_text", broken)
    monkeypatch.setattr(ipc.time, "sleep", pauses.append)
    with pytest.raises(type(failure)) as error:
        consume()
    assert error.value is failure
    assert len(attempts) == 1 and pauses == []


def test_reader_malformed_json_fails_immediately(monkeypatch, reader):
    target, consume, _value = reader
    target.write_bytes(b'{"phase":')
    read_text, attempts, pauses = Path.read_text, [], []

    def counted(path, *args, **kwargs):
        assert path == target
        attempts.append(path)
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", counted)
    monkeypatch.setattr(ipc.time, "sleep", pauses.append)
    with pytest.raises(json.JSONDecodeError):
        consume()
    assert len(attempts) == 1 and pauses == []


def test_busy_status_read_does_not_resend_or_resequence_command(monkeypatch, tmp_path):
    scenario = MachinePeople(tmp_path, ["person"])
    status = tmp_path / "person.json"
    value = {"phase": "ready", "sequence": 1}
    status.write_text(json.dumps(value), encoding="utf-8")
    read_text, send, reads, sends = Path.read_text, scenario.send, [], []

    def briefly_denied(path, *args, **kwargs):
        if path == status:
            reads.append(path)
            if len(reads) <= 3:
                raise PermissionError(13, "synthetic replacement still holds the name")
        return read_text(path, *args, **kwargs)

    def counted_send(person, action):
        sends.append((person, action))
        return send(person, action)

    monkeypatch.setattr(Path, "read_text", briefly_denied)
    monkeypatch.setattr(scenario, "send", counted_send)
    monkeypatch.setattr(ipc.time, "sleep", lambda _seconds: None)
    assert scenario.command("person", "state") == value
    assert sends == [("person", "state")] and scenario.sequence == 1
    assert json.loads((tmp_path / "person.command.json").read_text(encoding="utf-8")) == {
        "sequence": 1, "action": "state"}
    assert len(reads) == 4
