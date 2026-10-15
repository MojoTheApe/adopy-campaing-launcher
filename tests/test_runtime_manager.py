"""Versioned runtimes keep a failed update from damaging the working venv."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from scripts import acl_runtime_manager as runtime

# ACL-1582: файл идёт только в Windows-задание CI; причина — в маркере.
pytestmark = pytest.mark.windows_only(
    reason="per-commit launcher runtime in the Windows venv layout (Scripts/python.exe)")

OLD = "a" * 40
NEW = "b" * 40


@pytest.fixture
def local_config(monkeypatch, tmp_path: Path) -> Path:
    target = tmp_path / "machine-config"
    monkeypatch.setenv("ACL_CONFIG_DIR", str(target))
    return target


def _completed(returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, "", "")


def test_runtime_path_is_machine_local_and_sha_scoped(local_config: Path) -> None:
    assert runtime.runtime_dir(NEW) == local_config / "runtimes" / NEW
    assert runtime.runtime_python(NEW) == local_config / "runtimes" / NEW / "Scripts" / "python.exe"
    with pytest.raises(ValueError):
        runtime.runtime_dir("origin/master")


def test_prepare_builds_staging_runtime_without_touching_bootstrap(
    monkeypatch, local_config: Path, tmp_path: Path
) -> None:
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("example-package==1.2.3\n", encoding="utf-8")
    calls: list[list[str]] = []

    def fake_run(args, **_kwargs):
        args = [str(value) for value in args]
        calls.append(args)
        if args[1:3] == ["-m", "venv"]:
            python = Path(args[3]) / "Scripts" / "python.exe"
            python.parent.mkdir(parents=True)
            python.write_bytes(b"fake-python")
        return _completed()

    monkeypatch.setattr(runtime.subprocess, "run", fake_run)

    python = runtime.prepare_runtime(NEW, requirements)

    assert python == runtime.runtime_python(NEW)
    assert python.is_file()
    assert runtime.runtime_dir(NEW) != runtime._ROOT / ".venv"
    metadata = json.loads((runtime.runtime_dir(NEW) / "runtime.json").read_text())
    assert metadata["state"] == "prepared"
    assert metadata["sha"] == NEW
    assert any(call[1:3] == ["-m", "venv"] for call in calls)
    assert any("pip" in call and "install" in call for call in calls)

    runtime.activate_runtime(NEW)
    assert runtime.is_active(NEW) is True


def test_prepare_never_reuses_mismatched_runtime(local_config: Path, tmp_path: Path) -> None:
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("one==1\n", encoding="utf-8")
    runtime.runtime_dir(NEW).mkdir(parents=True)

    with pytest.raises(RuntimeError, match="incomplete or mismatched"):
        runtime.prepare_runtime(NEW, requirements)


def test_pending_candidate_rolls_back_and_is_quarantined(
    monkeypatch, local_config: Path
) -> None:
    state = {"head": NEW}
    runtime._atomic_json(
        runtime.pending_path(),
        {"schema": 1, "previous": OLD, "candidate": NEW},
    )
    monkeypatch.setattr(runtime, "_head", lambda: state["head"])
    monkeypatch.setattr(runtime, "_tracked_clean", lambda: True)
    monkeypatch.setattr(runtime, "_is_ancestor", lambda _old, _new: True)

    def fake_git(*args: str):
        if args[:2] == ("reset", "--keep"):
            assert args[2] == OLD
            state["head"] = OLD
        return _completed()

    monkeypatch.setattr(runtime, "_git", fake_git)

    changed, detail = runtime.recover_pending()

    assert changed is True
    assert state["head"] == OLD
    assert "quarantined" in detail
    assert runtime.is_quarantined(NEW) is True
    assert not runtime.pending_path().exists()


def test_pending_marker_before_merge_is_cleared_without_quarantine(
    monkeypatch, local_config: Path
) -> None:
    runtime._atomic_json(
        runtime.pending_path(),
        {"schema": 1, "previous": OLD, "candidate": NEW},
    )
    monkeypatch.setattr(runtime, "_head", lambda: OLD)

    changed, detail = runtime.recover_pending()

    assert changed is False
    assert "before code activation" in detail
    assert runtime.is_quarantined(NEW) is False


def test_mark_pending_refuses_dirty_or_non_fast_forward_checkout(
    monkeypatch, local_config: Path
) -> None:
    monkeypatch.setattr(runtime, "_head", lambda: OLD)
    monkeypatch.setattr(runtime, "_tracked_clean", lambda: False)

    with pytest.raises(RuntimeError, match="local changes"):
        runtime.mark_pending(OLD, NEW)
    assert not runtime.pending_path().exists()

    monkeypatch.setattr(runtime, "_tracked_clean", lambda: True)
    monkeypatch.setattr(runtime, "_is_ancestor", lambda _old, _new: False)
    with pytest.raises(RuntimeError, match="not a fast-forward"):
        runtime.mark_pending(OLD, NEW)
    assert not runtime.pending_path().exists()


def test_confirmation_requires_candidate_head_and_active_runtime(
    monkeypatch, local_config: Path
) -> None:
    target = runtime.runtime_dir(NEW)
    python = runtime.runtime_python(NEW)
    python.parent.mkdir(parents=True)
    python.write_bytes(b"fake-python")
    runtime._atomic_json(
        target / "runtime.json",
        {"schema": 1, "sha": NEW, "state": "active", "requirements_sha256": "x"},
    )
    runtime._atomic_json(
        runtime.pending_path(),
        {"schema": 1, "previous": OLD, "candidate": NEW},
    )
    monkeypatch.setattr(runtime, "_head", lambda: NEW)

    assert "confirmed healthy release" in runtime.confirm_running()
    assert not runtime.pending_path().exists()


def test_quarantined_release_cannot_be_prepared_again(
    local_config: Path, tmp_path: Path
) -> None:
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("one==1\n", encoding="utf-8")
    runtime._quarantine(NEW, "failed health check")

    with pytest.raises(RuntimeError, match="quarantined"):
        runtime.prepare_runtime(NEW, requirements)


def test_unknown_head_never_triggers_a_reset(monkeypatch, local_config: Path) -> None:
    runtime._atomic_json(
        runtime.pending_path(),
        {"schema": 1, "previous": OLD, "candidate": NEW},
    )
    monkeypatch.setattr(runtime, "_head", lambda: "c" * 40)
    monkeypatch.setattr(
        runtime,
        "_git",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not reset")),
    )

    with pytest.raises(RuntimeError, match="neither side"):
        runtime.recover_pending()
