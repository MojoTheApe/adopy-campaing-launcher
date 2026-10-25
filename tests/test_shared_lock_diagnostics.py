"""Real spawned children prove bounded Windows stress failure diagnostics."""
from __future__ import annotations

import json
import multiprocessing
import re
import time

import pytest

from tests import test_shared_file_locks as harness

pytestmark = pytest.mark.windows_only(reason="real spawned Windows stress-writer evidence and cleanup")


def _fails_writer(start, index):
    # The old harness can run this same negative and expose its missing evidence.
    progress = getattr(harness, "_writer_progress", None)
    if progress is not None:
        progress("target-entry", -1)
    assert start.wait(60)
    if progress is not None:
        progress("before-write", index)
    raise RuntimeError("controlled real child failure")


def _blocked_writer(start, index):
    harness._writer_progress("target-entry", -1)
    assert start.wait(60)
    harness._writer_progress("before-write", index)
    multiprocessing.get_context("spawn").Event().wait(60)


def _completed_writer(start, index, output):
    harness._writer_progress("target-entry", -1)
    from pathlib import Path
    assert start.wait(60)
    harness._writer_progress("before-write", index)
    Path(output, str(index)).write_text(str(index), encoding="utf-8")
    harness._writer_progress("after-write", index)


def _unobserved_child(ready, release):
    ready.set()
    release.wait(30)


def _without_entry_marker(start, index, ready, release):
    assert start.wait(60)
    ready.set()
    release.wait(30)


def _notes(error):
    notes = [n for n in getattr(error, "__notes__", ()) if n.startswith("Windows writer diagnostics: ")]
    assert len(notes) == 1, "the original failure must retain one bounded real-writer evidence note"
    return json.loads(notes[0].split(": ", 1)[1])


def _assert_reaped(evidence):
    live = {p.pid for p in multiprocessing.active_children()}
    assert len(evidence["writers"]) == 4
    assert len({p["pid"] for p in evidence["writers"]}) == 4
    assert all(p["pid"] not in live for p in evidence["writers"])


def test_real_failed_children_retain_failure_identity_and_progress():
    with pytest.raises(AssertionError, match="writer .* exit code") as caught:
        harness._run_processes(_fails_writer)
    evidence = _notes(caught.value)
    _assert_reaped(evidence)
    assert evidence["target"] == "_fails_writer"
    observed = [p for p in evidence["writers"] if p["target_entry_observed"]]
    assert observed
    assert any(p["phase"] == "before-write" for p in observed)
    assert any(p["exitcode_at_failure"] not in (None, 0) for p in observed)


def test_real_blocked_children_keep_the_timeout_failure_and_actual_trace():
    before = time.monotonic()
    with pytest.raises(AssertionError, match="exit code None") as caught:
        harness._run_processes(_blocked_writer, join_timeout=10, diagnostic_after=0.5)
    assert time.monotonic() - before < 30
    evidence = _notes(caught.value)
    _assert_reaped(evidence)
    waiting = [p for p in evidence["writers"] if p["alive_at_failure"] and p["target_entry_observed"]]
    assert waiting, "at least one real child must actually enter and block"
    stacks = [p["trace"].partition("\n")[2] for p in waiting]  # Exclude the descriptive header.
    assert any(re.search(r'File "[^\n"]*test_shared_lock_diagnostics\.py", line \d+,? in _blocked_writer\b', stack)
               and re.search(r"line \d+,? in wait\b", stack) for stack in stacks)
    assert any(p["phase"] == "before-write" for p in waiting)
    assert all(len(p["trace"]) <= 4_200 for p in evidence["writers"])


def test_completed_real_writers_keep_all_four_outputs(tmp_path):
    harness._run_processes(_completed_writer, str(tmp_path), join_timeout=10, diagnostic_after=1)
    assert {p.name: p.read_text() for p in tmp_path.iterdir()} == {str(i): str(i) for i in range(4)}


def test_invalid_diagnostic_timing_cannot_disable_the_capture():
    for join, capture in ((0, 1), (1, 0), (1, 1), (1, 2), (float("inf"), 1), (2, float("nan"))):
        with pytest.raises(ValueError, match="diagnostic"):
            harness._run_processes(_fails_writer, join_timeout=join, diagnostic_after=capture)


def test_partial_start_failure_keeps_original_error_and_reaps_actual_children(monkeypatch):
    ctx = multiprocessing.get_context("spawn")
    real_start = ctx.Process.start
    started = []

    def start_or_fail_third(proc):
        if len(started) == 2:
            raise OSError("controlled third process startup failure")
        real_start(proc)
        started.append(proc)

    monkeypatch.setattr(ctx.Process, "start", start_or_fail_third)
    with pytest.raises(OSError, match="controlled third process startup failure") as caught:
        harness._run_processes(_blocked_writer)
    evidence = _notes(caught.value)
    assert evidence["planned_writers"] == 4
    assert {p["pid"] for p in evidence["writers"]} == {p.pid for p in started}
    assert len(started) == 2
    assert all(not p.is_alive() and p.exitcode is not None for p in started)


def test_unobserved_live_entry_and_missing_trace_are_explicit(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    ready, release = ctx.Event(), ctx.Event()
    proc = ctx.Process(target=_unobserved_child, args=(ready, release))
    proc.start()
    try:
        assert ready.wait(10), "the constructed pre-instrumentation child must really be alive"
        state = ctx.RawArray("q", 4 * harness._WRITER_FIELDS)
        observed = harness._writer_observations([proc], state)
        evidence = json.loads(harness._writer_evidence_note(_unobserved_child, observed, tmp_path, []).split(": ", 1)[1])
        item = evidence["writers"][0]
        assert item["pid"] == proc.pid and item["alive_at_failure"]
        assert not item["wrapper_entered"] and not item["target_entry_observed"]
        assert item["last_wrapper_event"] == "not-observed"
        assert item["capture"] == "not-armed"
        assert item["trace"] == "" and item["trace_read_error"] == "FileNotFoundError"
    finally:
        release.set()
        proc.join(10)
        if proc.is_alive():
            proc.terminate()
            proc.join(10)
        assert not proc.is_alive()


def test_unavailable_capture_does_not_skip_the_real_child_work(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    start = ctx.Event()
    start.set()
    output = tmp_path / "output"
    output.mkdir()
    state = ctx.RawArray("q", 4 * harness._WRITER_FIELDS)
    proc = ctx.Process(target=harness._diagnosed_writer,
                       args=(_completed_writer, start, 0, (str(output),), state,
                             str(tmp_path / "missing-trace-directory"), 1))
    proc.start()
    try:
        proc.join(10)
        assert proc.exitcode == 0
        assert (output / "0").read_text() == "0"
        observed = harness._writer_observations([proc], state)[0]
        assert observed["target_entry_observed"] and observed["last_wrapper_event"] == "call-returned"
        assert observed["capture"] == "unavailable" and observed["phase"] == "after-write"
    finally:
        if proc.is_alive():
            proc.terminate()
            proc.join(10)
        assert not proc.is_alive()


def test_partial_trace_is_bounded_and_cannot_break_the_failure_note(tmp_path):
    (tmp_path / "writer-0.trace").write_bytes(b"actual trace prefix\xff\n" + b"x" * 20_000 + b"not-in-bounded-output")
    note = harness._writer_evidence_note(_fails_writer, [{"index": 0}], tmp_path, [])
    evidence = json.loads(note.split(": ", 1)[1])
    trace = evidence["writers"][0]["trace"]
    assert trace.startswith("actual trace prefix") and trace.endswith("[truncated]")
    assert len(trace) <= 4_200 and "not-in-bounded-output" not in note


def test_wrapper_preparation_is_not_reported_as_observed_target_entry(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    start, ready, release = ctx.Event(), ctx.Event(), ctx.Event()
    start.set()
    state = ctx.RawArray("q", 4 * harness._WRITER_FIELDS)
    proc = ctx.Process(target=harness._diagnosed_writer,
                       args=(_without_entry_marker, start, 0, (ready, release), state, str(tmp_path), 1))
    proc.start()
    try:
        assert ready.wait(10), "the real child must reach the deliberately uninstrumented target"
        observed = harness._writer_observations([proc], state)[0]
        assert observed["wrapper_entered"] and observed["alive_at_failure"]
        assert observed["last_wrapper_event"] == "call-prepared"
        assert observed["phase"] == "call-prepared"
        assert not observed["target_entry_observed"], "a wrapper event cannot prove a function-body event"
    finally:
        release.set()
        proc.join(10)
        if proc.is_alive():
            proc.terminate()
            proc.join(10)
        assert not proc.is_alive()


def test_parent_diagnostic_directory_failure_keeps_the_four_real_writers(monkeypatch, tmp_path):
    def unavailable(*args, **kwargs):
        raise PermissionError("controlled parent diagnostic-directory failure")

    monkeypatch.setattr(harness.tempfile, "TemporaryDirectory", unavailable)
    harness._run_processes(_completed_writer, str(tmp_path), join_timeout=10, diagnostic_after=1)
    assert {p.name: p.read_text() for p in tmp_path.iterdir()} == {str(i): str(i) for i in range(4)}
    with pytest.raises(AssertionError, match="writer .* exit code") as caught:
        harness._run_processes(_fails_writer)
    evidence = _notes(caught.value)
    _assert_reaped(evidence)
    assert all(p["trace_read_error"] == "DirectoryUnavailable" for p in evidence["writers"])
