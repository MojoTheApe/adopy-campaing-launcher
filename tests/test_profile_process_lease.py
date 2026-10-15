"""Real independent processes, synthetic UUIDs, no browser/provider/network."""
from contextlib import contextmanager
import multiprocessing
import os
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import pytest

from src.fb_ads_rpa import job_cancel, profile_lease as pl
from src.fb_ads_rpa.operations import profile_lock, publish_lease, serialized_browser


pytestmark = pytest.mark.windows_only(reason="Exercise the shared Windows file lock across spawned processes and process termination")

PROFILE = "550e8400-e29b-41d4-a716-446655440000"
OTHER = "6ba7b810-9dad-11d1-80b4-00c04fd430c8"


def _child(root, profile, mode, entered, release, result):
    # All configuration is synthetic. The production root is not overridable
    # by instance config: tests inject the private path seam in each process.
    pl._lease_root = lambda: Path(root)
    os.environ["ACL_CONFIG_DIR"] = str(Path(root) / "other-config")
    os.environ["ACL_PORT"] = "9876"
    os.environ["MULTILOGIN_API_BASE_A"] = "http://127.0.0.1:1"
    lock = profile_lock(profile)
    if mode == "try":
        ok = lock.acquire(blocking=False)
        result.put(ok)
        if ok:
            lock.release()
    else:
        with lock:
            entered.set()
            release.wait(40)


@pytest.fixture
def process_factory(tmp_path, monkeypatch):
    root = tmp_path / "shared"
    monkeypatch.setattr(pl, "_lease_root", lambda: root)
    ctx = multiprocessing.get_context("spawn")
    processes = []

    def start(profile=PROFILE, mode="try"):
        entered, release, result = ctx.Event(), ctx.Event(), ctx.Queue()
        process = ctx.Process(target=_child, args=(str(root), profile, mode, entered, release, result))
        processes.append((process, release, result))
        process.start()
        return process, entered, release, result

    yield start
    for process, release, result in processes:
        if process.is_alive():
            release.set()
        process.join(3)
        if process.is_alive():
            process.terminate()
            process.join(3)
        result.close()


def test_processes_share_physical_uuid_across_instance_config(process_factory, monkeypatch, tmp_path):
    monkeypatch.setenv("ACL_CONFIG_DIR", str(tmp_path / "first-config"))
    monkeypatch.setenv("ACL_PORT", "1234")
    monkeypatch.setenv("MULTILOGIN_API_BASE_A", "http://127.0.0.1:2")
    with publish_lease(PROFILE):
        _, _, _, result = process_factory(PROFILE.upper().replace("-", ""))
        assert result.get(timeout=20) is False
        _, _, _, other = process_factory(OTHER)
        assert other.get(timeout=20) is True
    _, _, _, after = process_factory()
    assert after.get(timeout=20) is True


def test_reentrant_outer_lease_remains_exclusive(process_factory):
    lock = profile_lock(PROFILE)
    assert lock is publish_lease(" {" + PROFILE.upper() + "} ")
    with lock:
        with publish_lease(PROFILE):
            assert lock.held_by_current_thread()
        _, _, _, result = process_factory()
        assert result.get(timeout=20) is False
    assert not lock.held_by_current_thread()


def test_process_termination_releases_without_removing_lock_file(process_factory):
    process, entered, _, _ = process_factory(mode="hold")
    assert entered.wait(20)
    lock = profile_lock(PROFILE)
    assert not lock.acquire(blocking=False)
    path = pl._lock_path(PROFILE)
    assert path.exists()
    process.terminate()
    process.join(5)
    assert not process.is_alive()
    assert lock.acquire(timeout=1)
    lock.release()
    assert path.exists()


def test_cancelled_waiter_never_enters_and_does_not_poison_lock(process_factory):
    _, entered, release, _ = process_factory(mode="hold")
    assert entered.wait(20)
    cancel, waiting, finished = threading.Event(), threading.Event(), threading.Event()
    outcome = []

    def wait():
        job_cancel.bind(cancel)
        waiting.set()
        try:
            with profile_lock(PROFILE):
                outcome.append("entered")
        except job_cancel.JobCancelled:
            outcome.append("cancelled")
        finally:
            job_cancel.unbind()
            finished.set()

    worker = threading.Thread(target=wait)
    worker.start()
    assert waiting.wait(2)
    cancel.set()
    assert finished.wait(.8)
    worker.join(1)
    assert outcome == ["cancelled"]
    release.set()
    lock = profile_lock(PROFILE)
    assert lock.acquire(timeout=3)
    lock.release()


def test_bounded_timeout_and_explicit_queue_cancel(process_factory, monkeypatch):
    _, entered, _, _ = process_factory(mode="hold")
    assert entered.wait(20)
    start = time.monotonic()
    assert not profile_lock(PROFILE).acquire(timeout=.1)
    assert time.monotonic() - start < 1
    with pytest.raises(job_cancel.JobCancelled):
        with publish_lease(PROFILE, cancelled=lambda: True):
            pytest.fail("cancelled queue entered")
    monkeypatch.setattr(pl, "WAIT_SECONDS", .1)
    with pytest.raises(pl.ProfileLeaseError, match="busy"):
        with profile_lock(PROFILE):
            pytest.fail("timed-out context entered")


def test_file_lock_failure_is_closed_and_thread_lock_recovers(monkeypatch):
    original = pl.machine_file_lock

    @contextmanager
    def denied(*args, **kwargs):
        raise PermissionError("synthetic path not included in user error")
        yield

    monkeypatch.setattr(pl, "machine_file_lock", denied)
    with pytest.raises(pl.ProfileLeaseError, match="Cannot acquire"):
        with profile_lock(PROFILE):
            pytest.fail("failed file lock entered")
    monkeypatch.setattr(pl, "machine_file_lock", original)
    with profile_lock(PROFILE):
        assert profile_lock(PROFILE).held_by_current_thread()


@pytest.mark.parametrize("value", ["", "label", "../profile", "1", None, "00000000-0000-0000-0000-000000000000"])
def test_invalid_identity_fails_before_operation(value):
    entered = []

    @serialized_browser(reading=True)
    def operation(profile_id):
        entered.append(profile_id)

    with pytest.raises(pl.ProfileLeaseError, match="UUID"):
        operation(value)
    assert not entered


def test_dolphin_identity_is_explicit_and_separate():
    assert pl.physical_profile_key("00123", provider="dolphin") == "dolphin:123"
    with profile_lock("dolphin:123"):
        with profile_lock(PROFILE):
            assert profile_lock("dolphin:123").held_by_current_thread()
    with pytest.raises(pl.ProfileLeaseError):
        pl.physical_profile_key("123")
    with pytest.raises(pl.ProfileLeaseError):
        pl.physical_profile_key(PROFILE, provider="dolphin")


@pytest.mark.parametrize("provider", ["multilogin", "multilogin_api", "multilogin_attach", "attach",
    "attach_only", "multilogin_direct", "direct", "direct_cdp", "playwright_profile", "pw_profile"])
def test_existing_provider_aliases_keep_one_physical_identity(provider):
    assert pl.physical_profile_key(PROFILE.upper(), provider=" " + provider.upper() + " ") == PROFILE


def test_padded_dolphin_provider_keeps_its_numeric_contract():
    assert pl.physical_profile_key("123", provider=" Dolphin ") == "dolphin:123"
    with pytest.raises(pl.ProfileLeaseError):
        pl.physical_profile_key("1" * 5000, provider="dolphin")


def test_nested_browser_call_cannot_switch_the_outer_profile():
    touched = []

    @serialized_browser(reading=True)
    def nested(profile):
        touched.append(profile)

    with profile_lock(PROFILE):
        nested(PROFILE)
        with pytest.raises(pl.ProfileLeaseError, match="changed"):
            nested(OTHER)
    assert touched == [PROFILE]


@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), -2])
def test_invalid_timeout_cannot_create_an_unbounded_wait(timeout):
    with pytest.raises(ValueError):
        profile_lock(PROFILE).acquire(timeout=timeout)


def test_map_aliases_share_uuid_and_unknown_mapping_refuses(monkeypatch):
    from src.fb_ads_rpa import rpa_publisher as rp
    from src.analysis_agent import account_spend as spend

    entry = SimpleNamespace(profile_id=PROFILE.upper())
    monkeypatch.setattr(rp, "load_ad_account_map", lambda *_: {})
    monkeypatch.setattr(rp, "resolve_account_entry", lambda mapping, label: entry if label in {"a", "b"} else None)
    monkeypatch.setattr(spend, "_entry_for", lambda label: entry if label == "a" else None)
    assert rp.profile_key_for("a") == rp.profile_key_for("b") == spend._profile_key("a") == PROFILE
    with pytest.raises(pl.ProfileLeaseError):
        rp.profile_key_for("missing")
    with pytest.raises(pl.ProfileLeaseError):
        spend._profile_key("missing")


def test_changed_map_does_not_open_or_close_other_profile(monkeypatch):
    from src.fb_ads_rpa import rpa_publisher as rp, browser
    from src.analysis_agent import account_spend as spend

    entry = SimpleNamespace(profile_id=OTHER)
    publisher = object.__new__(rp.RpaPublisher)
    publisher._conn = None
    publisher.browser_provider_name = "multilogin_attach"
    publisher._resolve_entry = lambda _: entry
    touched = []
    monkeypatch.setattr(rp, "_resolve_account", lambda _: entry)
    monkeypatch.setattr(browser, "stop_profile_on_request", lambda *_: touched.append("closed"))
    monkeypatch.setattr(spend, "_mlx_profile_dir", lambda *_: touched.append("looked up browser"))
    with profile_lock(PROFILE):
        for action in (lambda: publisher._ensure_browser("a"), lambda: rp.close_account_profile("a"),
                       lambda: spend._attach_only(entry, "a")):
            with pytest.raises(pl.ProfileLeaseError, match="does not match"):
                action()
    assert not touched


@pytest.mark.parametrize("entry", [None, SimpleNamespace(profile_id=""), SimpleNamespace(profile_id="invalid")])
def test_public_read_failures_keep_their_result_shapes_without_touching_browser(monkeypatch, entry):
    from src.analysis_agent import account_spend as spend, country_split as country

    monkeypatch.setattr(spend, "_entry_for", lambda _: entry)
    monkeypatch.setattr(spend, "_attach_only", lambda *_: pytest.fail("invalid identity attached"))
    monkeypatch.setattr(spend, "_probe_entry", lambda *_: pytest.fail("invalid probe entered"))
    monkeypatch.setattr(country, "_attach_only", lambda *_: pytest.fail("invalid country profile attached"))
    assert spend.read_via_rpa("alias", "2026-01-01").state == "failed"
    assert country.read_country_split("alias", "2026-01-01").state == "failed"
    assert spend.open_for_automation("alias")[0] is False
    assert spend.open_profile_for_automation("invalid")[0] is False
    assert spend.read_ad_account("invalid")["ok"] is False


def test_public_read_reports_a_lease_io_failure(monkeypatch):
    from src.analysis_agent import account_spend as spend

    monkeypatch.setattr(spend, "_entry_for", lambda _: SimpleNamespace(profile_id=PROFILE))
    monkeypatch.setattr(spend, "_attach_only", lambda *_: pytest.fail("failed lease attached"))

    @contextmanager
    def denied(*args, **kwargs):
        raise PermissionError("synthetic")
        yield

    monkeypatch.setattr(pl, "machine_file_lock", denied)
    result = spend.read_via_rpa("alias", "2026-01-01")
    assert result.state == "failed" and "Cannot acquire" in result.error


def test_country_ownership_refusal_replaces_cached_success(monkeypatch, tmp_path):
    import json
    from src.analysis_agent import account_spend as spend, country_split as country

    path = tmp_path / "country.json"
    monkeypatch.setattr(country, "_store_path", lambda: path)
    monkeypatch.setattr(spend, "_entry_for", lambda _: None)
    country.save_split(country.CountrySplit("alias", "2026-01-01", "2026-01-01",
                                           state="ok", rows=[{"country": "Test", "spend": 1}]))
    refused = country.read_country_split("alias", "2026-01-01")
    cached = next(iter(json.loads(path.read_text(encoding="utf-8")).values()))
    assert refused.state == cached["state"] == "failed"
    assert cached["rows"] == [] and "account map" in cached["error"]


def test_rejected_turn_off_keeps_a_failed_audit_record_without_browser_action(monkeypatch):
    from src.analysis_agent import account_spend as spend

    recorded = []
    monkeypatch.setattr(spend, "_entry_for", lambda _: None)
    monkeypatch.setattr(spend, "_record_turn_off", recorded.append)
    monkeypatch.setattr(spend, "_turn_off", lambda *a, **kw: pytest.fail("unowned turn-off started"))
    result = spend.turn_off("missing", "campaign", "123", expected_name="Synthetic campaign", requester=" tester ")
    assert result.state == "failed" and result.requester == "tester"
    assert result.route == "rpa" and "account map" in result.error
    assert recorded == [result]


@pytest.fixture
def queued_job(monkeypatch):
    from src.web import publish_jobs as pj, profile_slots as ps

    monkeypatch.setattr(pj, "_jobs", {})
    monkeypatch.setattr(pj, "_write_draft_status", lambda *a, **kw: None)
    monkeypatch.setattr(pj, "profile_key_for", lambda _: PROFILE)
    monkeypatch.setattr(pj, "_run_rpa", lambda *a, **kw: pytest.fail("refused job entered browser"))
    slots = ps.ProfileSlots(memory=lambda: None, settings=lambda: ps.SlotSettings(override=2), tick_s=.02)
    monkeypatch.setattr(pj, "_slots", slots)
    job = pj.create_job("alias", "Test", [{}])

    def run():
        pj._run_job(job, [{}], account_label="alias", country="Test", via_multilogin=True, activate=False)

    return pj, slots, job, run


def test_publish_lease_failure_finishes_job_and_releases_slot(queued_job, monkeypatch):
    pj, slots, job, run = queued_job

    @contextmanager
    def denied(*args, **kwargs):
        raise PermissionError("synthetic")
        yield

    monkeypatch.setattr(pl, "machine_file_lock", denied)
    run()
    result = pj.get_job(job)
    assert result["status"] == "done" and result["finished_at"]
    assert "Cannot acquire" in result["errors"][-1]
    assert not result.get("started_at")
    assert slots.snapshot()["running"] == []


def test_publish_cancel_waiting_for_other_process_never_opens_browser(queued_job, process_factory):
    pj, slots, job, run = queued_job
    _, entered, _, _ = process_factory(mode="hold")
    assert entered.wait(20)
    worker = threading.Thread(target=run)
    worker.start()
    end = time.monotonic() + 3
    while not slots.snapshot()["running"] and time.monotonic() < end:
        time.sleep(.02)
    assert slots.snapshot()["running"]
    with pj._jobs_lock:
        pj._jobs[job]["cancel_requested"] = True
    worker.join(1)
    assert not worker.is_alive()
    assert pj.get_job(job)["status"] == "cancelled"
    assert not pj.get_job(job).get("started_at")
    assert slots.snapshot()["running"] == []


# ── ACL-1853 review: publish waiters, timer, clean refusals ─────────────────

def _hold_in_thread(seconds):
    held, done = threading.Event(), threading.Event()

    def run():
        with publish_lease(PROFILE):
            held.set()
            done.wait(seconds)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert held.wait(5)
    return thread, done


def test_a_waiting_publish_outlasts_the_short_read_cap_but_a_read_does_not(monkeypatch):
    monkeypatch.setattr(pl, "WAIT_SECONDS", .1)
    monkeypatch.setattr(pl, "PUBLISH_WAIT_SECONDS", 5.0)
    thread, done = _hold_in_thread(.6)
    reader = threading.Thread(target=lambda: results.append(profile_lock(PROFILE).acquire()))
    results = []
    reader.start()
    reader.join(2)
    assert results == [False], "a read still gives up at the short cap"
    started = time.monotonic()
    with publish_lease(PROFILE, cancelled=lambda: False):
        waited = time.monotonic() - started
    assert waited > .3, "the publish waited past the short cap"
    thread.join(2)


def test_a_waiting_publish_is_cancelled_promptly(monkeypatch):
    monkeypatch.setattr(pl, "WAIT_SECONDS", .1)
    monkeypatch.setattr(pl, "PUBLISH_WAIT_SECONDS", 30.0)
    thread, done = _hold_in_thread(10)
    flag = threading.Event()
    threading.Timer(.3, flag.set).start()
    started = time.monotonic()
    with pytest.raises(job_cancel.JobCancelled):
        with publish_lease(PROFILE, cancelled=flag.is_set):
            pytest.fail("entered while another holder had the lease")
    assert time.monotonic() - started < 3
    done.set()
    thread.join(2)


def test_the_deferred_close_timer_survives_a_lease_refusal(monkeypatch):
    from src.analysis_agent import account_spend as spend

    opened = SimpleNamespace(profile_id="not-a-uuid", account="Acc")
    monkeypatch.setattr(spend, "_PENDING_CLOSE", {"not-a-uuid": opened})
    monkeypatch.setattr(spend, "_close_started", lambda o: pytest.fail("closed without owning the profile"))
    spend._close_if_still_pending(opened)
    assert spend._PENDING_CLOSE == {"not-a-uuid": opened}, "left for the next operation"


def test_the_api_country_split_refuses_with_a_clean_message(monkeypatch, tmp_path):
    from src.analysis_agent import account_spend as spend, account_spend_api as api, country_split as country

    monkeypatch.setattr(spend, "_entry_for", lambda label: None)
    monkeypatch.setattr(country, "_store_path", lambda: tmp_path / "country_splits.json")
    out = api.read_country_split_via_api("missing", "2026-01-01")
    assert out.state == "failed" and "account map" in out.error
    assert "ProfileLeaseError" not in out.error
    assert spend._failure_text(pl.ProfileLeaseError("plain")) == "plain"
    assert spend._failure_text(RuntimeError("x")) == "RuntimeError: x"


def test_close_profile_skips_an_unrelated_unresolvable_job(monkeypatch, tmp_path):
    from src.fb_ads_rpa import rpa_publisher as rp
    from src.web import publish_jobs as pj

    monkeypatch.setenv("ACL_CONFIG_DIR", str(tmp_path))

    def key(label):
        if label == "broken":
            raise pl.ProfileLeaseError("Cannot resolve a physical profile from the account map.")
        return PROFILE

    closed = []
    monkeypatch.setattr(pj, "profile_key_for", key)
    monkeypatch.setattr(pj, "_jobs", {"mine": {"account_label": "Acc", "status": "done"},
                                      "other": {"account_label": "broken", "status": "running"}})
    monkeypatch.setattr(pj._slots, "release", lambda job_id: None)
    monkeypatch.setattr(pj._slots, "wake", lambda: None)
    monkeypatch.setattr(rp, "close_account_profile", closed.append)
    assert pj.close_profile("mine")["ok"] is True and closed == ["Acc"]
