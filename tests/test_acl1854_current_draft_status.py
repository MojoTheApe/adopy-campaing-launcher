"""Real job lifecycle and report persistence, with only the browser boundary faked."""
from __future__ import annotations

import contextlib
import importlib
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from config.paths import get_draft_status_path
from src.fb_ads_rpa.draft import RpaPublishResult
from src.web import publish_jobs as jobs
from tests.test_publish_safety_contract import _render_badge, _result

web = importlib.import_module("src.web.app")


def until(condition):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(.01)
    assert condition()


@pytest.fixture
def run(monkeypatch):
    made, gates = [], []
    monkeypatch.setattr(jobs, "_jobs", {})
    monkeypatch.setattr(jobs, "_active_publishers", {})
    monkeypatch.setattr(jobs, "_job_ended", {})
    monkeypatch.setattr(jobs, "use_rpa_publish", lambda: True)
    monkeypatch.setattr(jobs, "profile_key_for", lambda label: label)
    monkeypatch.setattr(jobs, "_publish_serial", lambda *a, **k: contextlib.nullcontext())
    monkeypatch.setattr(jobs, "keep_display_awake", lambda *a: contextlib.nullcontext())
    monkeypatch.setattr(jobs, "_watch_screen_lock", lambda *a: contextlib.nullcontext())
    monkeypatch.setattr(jobs, "_clear_inherited_asyncio_loop", lambda: None)
    monkeypatch.setattr(jobs, "_start_stuck_watch", lambda *a: None)
    monkeypatch.setattr(jobs, "_let_go_of_profile", lambda *a, **k: None)
    monkeypatch.setattr(jobs, "_slots", SimpleNamespace(
        acquire=lambda *a, **k: {"waited_s": 0, "decision": {}, "first_hold": None},
        release=lambda *a: None, keep=lambda *a: None, wake=lambda: None))
    monkeypatch.setenv("ACL_CANCEL_DEADLINE_S", ".1")
    drafts = lambda slots, **kw: [SimpleNamespace(name=s["name"], errors=[]) for s in slots]
    monkeypatch.setattr("src.fb_ads_rpa.build_draft.slots_to_campaign_drafts", drafts)

    class Publisher:
        def __init__(self, **kw):
            self.cancel_event = threading.Event()

        build_drafts = staticmethod(drafts)

        def publish_draft(self, draft):
            entered, release = controls[draft.name]
            entered.set()
            assert release.wait(8), "fixture publisher was not released"
            return RpaPublishResult(campaign_name=draft.name, success=True)

        def cancel(self):
            self.cancel_event.set()

        def close(self, **kw):
            pass

    controls = {}
    monkeypatch.setattr(jobs, "RpaPublisher", Publisher)

    def start(label="Account", names=("First", "Second")):
        for name in names:
            controls[name] = (threading.Event(), threading.Event())
            gates.append(controls[name][1])
        jid = jobs.start_job(label, "MX", [{"name": n} for n in names])
        made.append(jid)
        return jid

    yield SimpleNamespace(start=start, controls=controls)
    for gate in gates:
        gate.set()
    for jid in made:
        until(lambda: jid not in jobs._job_ended)


def saved(label="Account"):
    return web._load_draft_status().get(label, {})


def seed_ready(label="Account"):
    jobs._write_draft_status(label, [_result()], expected_total=1)


def test_accepted_run_invalidates_ready_and_duplicate_preserves_current(run):
    seed_ready()
    jid = run.start()
    assert run.controls["First"][0].wait(3)
    current = saved()
    assert current["state"] != "ready"
    assert current["job_id"] == jid
    assert current["plan_revision"]
    assert current["previous_ready"]["state"] == "ready"
    assert "✓ Draft ready" not in _render_badge(current)
    assert "Previous successful build" in _render_badge(current)
    with pytest.raises(jobs.DuplicateJobError):
        jobs.start_job("Account", "MX", [{"name": "Other"}])
    assert saved() == current


@pytest.mark.parametrize("abandon", [False, True])
def test_partial_cancel_reloads_current_stopped_result(run, abandon):
    seed_ready()
    jid = run.start()
    assert run.controls["First"][0].wait(3)
    run.controls["First"][1].set()
    assert run.controls["Second"][0].wait(3)
    assert jobs.get_job(jid)["done_count"] == 1
    assert jobs.cancel_job(jid)["ok"]
    if not abandon:
        run.controls["Second"][1].set()
    until(lambda: jobs.get_job(jid)["status"] == "cancelled")
    until(lambda: saved().get("state") == "stopped")
    current = saved()
    assert current["job_id"] == jid
    assert current["count"] >= 1
    assert "✓ Draft ready" not in _render_badge(current)
    assert "Draft stopped" in _render_badge(current)
    if abandon:
        assert jobs.get_job(jid)["abandoned"]
        run.controls["Second"][1].set()
        until(lambda: jid not in jobs._job_ended)
        assert saved() == current, "late abandoned completion cannot change the verdict"


def test_reload_after_lost_process_never_uses_older_ready(run, monkeypatch):
    seed_ready()
    jid = run.start()
    assert run.controls["First"][0].wait(3)
    with monkeypatch.context() as patch:
        patch.setattr(jobs, "_jobs", {})
        current = saved()
    assert current["state"] == "interrupted"
    assert current["job_id"] == jid
    assert "✓ Draft ready" not in _render_badge(current)


@pytest.mark.parametrize("contents", [None, "{bad json"])
def test_unavailable_storage_is_visible_and_unverified(run, contents):
    path = Path(get_draft_status_path())
    if contents is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")
    current = saved()
    assert current["state"] == "unverified"
    assert current["errors"]
    html = _render_badge(current)
    assert "Draft unverified" in html
    assert current["errors"][0] in html


@pytest.mark.parametrize("cancel_second", [False, True])
def test_two_public_jobs_finishing_together_keep_both_accounts(run, monkeypatch, cancel_second):
    seed_ready("Account")
    seed_ready("Other")
    one = run.start("Account", ("A",))
    two = run.start("Other", ("B",))
    assert run.controls["A"][0].wait(3) and run.controls["B"][0].wait(3)
    real_load = json.load
    first_read, second_read = threading.Event(), threading.Event()

    def interleave(f, *a, **kw):
        value = real_load(f, *a, **kw)
        if Path(f.name).name == "draft_status.json":
            if threading.current_thread().name == f"publish-{one[:8]}":
                first_read.set()
                # Old code lets B read and replace while A holds stale JSON.
                # A correct whole-transaction lock keeps B outside this read.
                second_read.wait(.25)
            elif threading.current_thread().name == f"publish-{two[:8]}":
                second_read.set()
        return value

    monkeypatch.setattr(json, "load", interleave)
    run.controls["A"][1].set()
    assert first_read.wait(3)
    if cancel_second:
        assert jobs.cancel_job(two)["ok"]
    run.controls["B"][1].set()
    until(lambda: one not in jobs._job_ended and two not in jobs._job_ended)
    current = web._load_draft_status()
    assert current["Account"]["campaign_names"] == ["A"]
    assert current["Other"]["campaign_names"] == ["B"]
    assert current["Account"].get("job_id") == one
    assert current["Other"].get("job_id") == two
    assert current["Account"]["state"] == "ready"
    assert current["Other"]["state"] == ("stopped" if cancel_second else "ready")


def test_corrupt_storage_rejects_admission_without_overwriting_it(run):
    path = Path(get_draft_status_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{corrupt-but-preserved", encoding="utf-8")
    with pytest.raises(RuntimeError, match="could not be saved"):
        run.start()
    assert not jobs._jobs
    assert not run.controls["First"][0].is_set()
    assert path.read_text(encoding="utf-8") == "{corrupt-but-preserved"
    assert saved()["state"] == "unverified"


def test_storage_failure_at_finish_is_visible_and_never_publishable(run, monkeypatch):
    from src.web import draft_status

    jid = run.start(names=("First",))
    assert run.controls["First"][0].wait(3)

    def denied(*args):
        raise PermissionError("synthetic write denial")

    monkeypatch.setattr(draft_status, "_atomic_write", denied)
    run.controls["First"][1].set()
    until(lambda: jid not in jobs._job_ended)
    assert not jobs.get_job(jid)["publishable"]
    current = saved()
    assert current["state"] == "unverified"
    assert current["job_id"] == jid
    assert "could not be saved" in _render_badge(current)


def test_deleted_storage_during_a_run_cannot_claim_a_saved_success(run):
    jid = run.start(names=("First",))
    assert run.controls["First"][0].wait(3)
    Path(get_draft_status_path()).unlink()
    run.controls["First"][1].set()
    until(lambda: jid not in jobs._job_ended)
    assert not jobs.get_job(jid)["publishable"]
    assert saved()["state"] == "unverified"


def test_legacy_record_and_other_accounts_survive_new_run(run):
    seed_ready()
    path = Path(get_draft_status_path())
    data = json.loads(path.read_text(encoding="utf-8"))
    data["Untouched"] = {"state": "ready", "count": 7, "custom": "legacy"}
    path.write_text(json.dumps(data), encoding="utf-8")
    assert saved()["state"] == "ready"
    run.start()
    assert json.loads(path.read_text(encoding="utf-8"))["Untouched"] == data["Untouched"]
    assert saved()["previous_ready"]["campaign_names"] == ["Campaign A"]


def test_failed_build_is_the_current_outcome(run, monkeypatch):
    seed_ready()

    def broken(*a, **kw):
        raise ValueError("synthetic invalid draft")

    monkeypatch.setattr(jobs.RpaPublisher, "build_drafts", broken)
    jid = run.start()
    until(lambda: jid not in jobs._job_ended)
    current = saved()
    assert current["state"] == "blocked"
    assert current["job_id"] == jid
    assert any("synthetic invalid draft" in error for error in current["errors"])
    assert current["previous_ready"]["state"] == "ready"


def test_old_writer_cannot_replace_newer_job_identity(run):
    from src.web import draft_status

    jid = run.start()
    before = saved()
    assert draft_status.save(get_draft_status_path(), "Account", {
        "job_id": "obsolete-job", "state": "ready", "count": 99,
    }) is None
    assert saved() == before
    assert saved()["job_id"] == jid


def test_plan_revision_identifies_the_submitted_content(run):
    first = jobs.create_job("First", "MX", [{"name": "Campaign", "budget": 10}])
    same = jobs.create_job("Second", "MX", [{"budget": 10, "name": "Campaign"}])
    changed = jobs.create_job("Third", "MX", [{"name": "Campaign", "budget": 20}])
    assert jobs.get_job(first)["plan_revision"] == jobs.get_job(same)["plan_revision"]
    assert jobs.get_job(first)["plan_revision"] != jobs.get_job(changed)["plan_revision"]
    assert saved("Third")["job_id"] == changed


def test_worker_start_failure_persists_a_failed_current_run(run, monkeypatch):
    seed_ready()

    def cannot_start(self):
        raise RuntimeError("synthetic thread start failure")

    monkeypatch.setattr(threading.Thread, "start", cannot_start)
    with pytest.raises(RuntimeError, match="synthetic thread start"):
        run.start()
    current = saved()
    assert current["state"] == "blocked"
    assert "worker could not start" in current["errors"][0]
    assert current["previous_ready"]["state"] == "ready"
    assert not jobs._job_ended


@pytest.mark.parametrize("field,value", [
    ("state", []), ("state", {}), ("status_revision", "3"),
    ("status_revision", -1), ("job_id", {}),
    ("ts", 123), ("campaign_names", {}), ("previous_ready", {"ts": []}),
])
def test_invalid_record_fields_load_and_render_as_unverified(run, field, value):
    path = Path(get_draft_status_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"Account": {"state": "ready", field: value}}), encoding="utf-8")
    before = path.read_bytes()
    current = saved()
    assert current["state"] == "unverified"
    assert "Draft unverified" in _render_badge(current)
    assert path.read_bytes() == before


@pytest.mark.parametrize("same_job", [False, True])
def test_late_storage_failure_cannot_mask_a_newer_saved_verdict(run, monkeypatch, same_job):
    from src.web import draft_status

    path = get_draft_status_path()
    new = {"job_id": "current", "status_revision": 3, "state": "ready"}
    assert draft_status.save(path, "Account", new, begin=True) is None

    @contextlib.contextmanager
    def denied(*a, **kw):
        raise TimeoutError("synthetic busy lock")
        yield  # pragma: no cover

    monkeypatch.setattr(draft_status, "machine_file_lock", denied)
    old = {"job_id": "current" if same_job else "obsolete", "status_revision": 2, "state": "running"}
    assert draft_status.save(path, "Account", old)
    assert all(saved()[key] == value for key, value in new.items())


def test_late_progress_snapshot_cannot_overwrite_terminal_verdict(run):
    from src.web import draft_status

    path = get_draft_status_path()
    new = {"job_id": "current", "status_revision": 3, "state": "stopped"}
    assert draft_status.save(path, "Account", new, begin=True) is None
    assert draft_status.save(path, "Account", {
        "job_id": "current", "status_revision": 2, "state": "running",
    }) is None
    assert all(saved()[key] == value for key, value in new.items())


@pytest.mark.parametrize("advance_predecessor", [False, True])
def test_failed_admission_overlay_respects_predecessor_revision(monkeypatch, tmp_path, advance_predecessor):
    from src.web import draft_status

    path = tmp_path / "draft_status.json"
    monkeypatch.setenv("ACL_CONFIG_DIR", str(tmp_path))
    old = {"job_id": "accepted-job", "status_revision": 2, "state": "running", "ts": "2026-10-13T00:00:00"}
    assert draft_status.save(str(path), "Account", old, begin=True) is None
    entered, resume = threading.Event(), threading.Event()
    mutex = threading.Lock()
    real_write = draft_status._atomic_write

    def fail_new_admission(*args):
        if threading.current_thread().name == "rejected-admission":
            raise PermissionError("synthetic admission write failure")
        real_write(*args)

    class FailureLock:
        def __enter__(self):
            if threading.current_thread().name == "rejected-admission":
                entered.set()
                assert resume.wait(5)
            mutex.acquire()

        def __exit__(self, *args):
            mutex.release()

    monkeypatch.setattr(draft_status, "_failure_lock", FailureLock())
    monkeypatch.setattr(draft_status, "_atomic_write", fail_new_admission)
    errors = []
    worker = threading.Thread(name="rejected-admission", target=lambda: errors.append(
        draft_status.save(str(path), "Account", {
            "job_id": "not-accepted", "status_revision": 1, "state": "running",
        }, begin=True)))
    worker.start()
    try:
        assert entered.wait(5)
        if advance_predecessor:
            # The earlier job finished before admission, but its final snapshot
            # can commit after admission fails: disk I/O is outside _jobs_lock.
            assert draft_status.save(str(path), "Account", {
                **old, "status_revision": 3, "state": "ready", "count": 1,
                "campaign_names": ["Synthetic campaign"],
            }) is None
    finally:
        resume.set()
        worker.join(5)
    assert not worker.is_alive()
    assert errors and errors[0]
    current = jobs.load_draft_status()["Account"]
    if advance_predecessor:
        assert json.loads(path.read_text(encoding="utf-8"))["Account"]["state"] == "ready"
        assert current["state"] == "ready"
        assert current["job_id"] == "accepted-job"
        assert current["status_revision"] == 3
        assert "✓ Draft ready" in _render_badge(current)
    else:
        assert current["state"] == "unverified"
        assert "could not be saved" in _render_badge(current)


def test_slow_storage_does_not_hold_the_cancellation_lock(run, monkeypatch):
    from src.web import draft_status

    jid = run.start(names=("First",))
    assert run.controls["First"][0].wait(3)
    entered, release = threading.Event(), threading.Event()
    real_write = draft_status._atomic_write

    def delayed(*args):
        entered.set()
        assert release.wait(5)
        return real_write(*args)

    monkeypatch.setattr(draft_status, "_atomic_write", delayed)
    run.controls["First"][1].set()
    try:
        assert entered.wait(3)
        assert jobs.cancel_job(jid)["ok"]
        until(lambda: jobs.get_job(jid)["status"] == "cancelled")
        assert jobs.get_job(jid)["abandoned"]
    finally:
        release.set()
    until(lambda: jid not in jobs._job_ended)
    until(lambda: saved()["state"] == "stopped")


def _process_writer(path, label, read_entered, release, attempted, output):
    """Spawn-safe child; a real OS lock guards an intentionally held read."""
    from src.web import draft_status

    original = draft_status._read

    def held_read(target):
        data = original(target)
        read_entered.set()
        if release is not None:
            assert release.wait(10)
        return data

    draft_status._read = held_read
    attempted.set()
    output.put(draft_status.save(path, label, {"state": "blocked", "count": 1}))


def test_separate_processes_lock_the_entire_read_modify_write(run):
    import multiprocessing

    seed_ready("Untouched")
    ctx = multiprocessing.get_context("spawn")
    first_read, second_read, release = ctx.Event(), ctx.Event(), ctx.Event()
    first_attempt, second_attempt, output = ctx.Event(), ctx.Event(), ctx.Queue()
    path = get_draft_status_path()
    one = ctx.Process(target=_process_writer, args=(path, "First", first_read, release, first_attempt, output))
    two = ctx.Process(target=_process_writer, args=(path, "Second", second_read, None, second_attempt, output))
    one.start()
    try:
        assert first_read.wait(10)
        two.start()
        assert second_attempt.wait(10)
        assert not second_read.wait(.15), "second process read outside the transaction lock"
    finally:
        release.set()
        one.join(10)
        if two.pid is not None:
            two.join(10)
    assert one.exitcode == two.exitcode == 0
    assert output.get(timeout=2) is None and output.get(timeout=2) is None
    current = web._load_draft_status()
    assert set(current) == {"Untouched", "First", "Second"}
