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
