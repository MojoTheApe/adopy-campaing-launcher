"""ACL-2513 (follow-up of ACL-2502): real worker PROCESSES go through the real callers under the real lease.

The earlier two-process tests (`test_acl1862_lease_windows.py`, `test_profile_process_lease.py`) hold
the lease with a lock helper or call one caller in the test process. Here BOTH sides are separate
spawned processes and every side runs a real caller (`tests/acl2513_lease_workers.py`):

    read      account_spend_api.read_country_split_via_api
    command   campaign_commands.open_session(commands=True) / account_spend_api.turn_off
    create    publish lease + leak watch + publish_api.ApiPublisher (open/finish_job)
    checkout  account_spend_api._warm_session -> WarmRegistry.checkout
    cleanup   WarmRegistry.close_one, publish_jobs._stop_idle_profile, account_spend._close_if_still_pending

The Graph transport, Multilogin stop and tabs are synthetic (a shared fake "Meta" file, fake MLX that
only logs). The conftest autouse fixture that disables the checkout lease does not exist in a worker,
so the real lease is on (`test_workers_run_with_the_real_checkout_lease`). No live profile, activation
or budget edit. Same OS user on one machine is the supported boundary.
"""
from __future__ import annotations

import json
import multiprocessing
import queue
import time
import uuid
from pathlib import Path

import pytest

from tests import acl2513_lease_workers as workers

pytestmark = pytest.mark.windows_only(reason="Real file lock shared by spawned processes")

CTX = multiprocessing.get_context("spawn")
UP = 120          # seconds to wait for a worker to answer (spawn + imports are slow on CI)


# ───────────────────────── harness ─────────────────────────

class Job:
    def __init__(self, cluster: "Cluster", worker: str, spec: dict):
        self.cluster, self.worker, self.spec = cluster, worker, spec
        self.id = spec["job"]
        self.sig = Path(spec["sig"])
        self._result: dict | None = None

    def entered(self) -> bool:
        return (self.sig / f"entered-{self.id}").exists()

    def wait_entered(self, seconds: float = UP) -> bool:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if self.entered():
                return True
            time.sleep(0.02)
        return self.entered()

    def release(self) -> None:
        (self.sig / f"release-{self.id}").write_text("1", encoding="utf-8")

    def result(self, seconds: float = UP) -> dict:
        if self._result is None:
            self._result = self.cluster.take(self.worker, self.id, seconds)
        return self._result

    def done(self) -> bool:
        if self._result is not None:
            return True
        try:
            self._result = self.cluster.take(self.worker, self.id, 0.01)
        except queue.Empty:
            return False
        return True


class Cluster:
    """Named worker processes sharing one lease folder and one fake Meta. One open job per worker."""

    def __init__(self, root: Path):
        self.root = root
        self.procs: dict[str, tuple] = {}
        self.jobs: list[Job] = []
        self._n = 0
        self._stash: dict[str, dict] = {}

    def start(self, name: str) -> None:
        commands, results = CTX.Queue(), CTX.Queue()
        proc = CTX.Process(target=workers.worker_main, args=(str(self.root), name, commands, results), daemon=True)
        proc.start()
        self.procs[name] = (proc, commands, results)

    def ready(self, name: str, sig: Path) -> None:
        assert self.run(name, "ping", str(uuid.uuid4()), sig=sig).result(UP)["result"] == "pong"

    def run(self, name: str, scenario: str, profile: str, *, sig: Path, **kw) -> Job:
        self._n += 1
        spec = {"job": f"{name}{self._n}", "scenario": scenario, "profile": profile, "sig": str(sig), **kw}
        job = Job(self, name, spec)
        self.jobs.append(job)
        self.procs[name][1].put(spec)
        return job

    def take(self, name: str, job_id: str, seconds: float) -> dict:
        results = self.procs[name][2]
        end = time.monotonic() + seconds
        while job_id not in self._stash:
            got = results.get(timeout=max(0.01, end - time.monotonic()))   # queue.Empty when it is late
            self._stash[got["job"]] = got
        return self._stash.pop(job_id)

    def alive(self, name: str) -> bool:
        return self.procs[name][0].is_alive()

    def kill(self, name: str) -> None:
        proc = self.procs[name][0]
        proc.terminate()
        proc.join(10)

    def release_all(self) -> None:
        for job in self.jobs:
            try:
                job.release()
            except OSError:
                pass

    def close(self) -> None:
        for proc, commands, results in self.procs.values():
            if proc.is_alive():
                commands.put(None)
        for proc, commands, results in self.procs.values():
            proc.join(10)
            if proc.is_alive():
                proc.terminate()
                proc.join(5)


@pytest.fixture(scope="module")
def cluster(tmp_path_factory):
    root = tmp_path_factory.mktemp("acl2513")
    c = Cluster(root)
    for name in ("A", "B"):
        c.start(name)
    sig = root / "ready"
    sig.mkdir()
    for name in ("A", "B"):
        c.ready(name, sig)
    yield c
    c.release_all()
    c.close()


@pytest.fixture
def sig(tmp_path, cluster, strict_checkout_lease):
    yield tmp_path
    cluster.release_all()                       # a failed test must not leave a worker parked on a hold


@pytest.fixture
def profile():
    return str(uuid.uuid4())


def events(sig: Path) -> list[dict]:
    path = sig / "events.jsonl"
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x] if path.exists() else []


def at(sig: Path, job: Job, ev: str) -> int:
    (row,) = [e for e in events(sig) if e["job"] == job.id and e["ev"] == ev]
    return row["t"]


def posts(cluster: Cluster) -> list[dict]:
    path = cluster.root / "meta" / "posts.jsonl"
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x] if path.exists() else []


#: What each contender does when the profile is taken: it waits (reads, commands, checkout, publish) and
#: is refused after the wait here shortened by `wait`, or it is refused at once (cleanup).
PAIRS = [
    ("read", "create"), ("create", "read"),                 # read + create
    ("read", "command"), ("command", "read"),               # read + command
    ("create", "cleanup_warm"), ("cleanup_warm", "create"),  # create + warm cleanup
    ("command", "checkout"), ("checkout", "command"),       # command + warm checkout
]


def refused_explicitly(res: dict) -> bool:
    """Not run, and said so: a lease error, a busy answer or a failed-with-text result - never silence."""
    if res["entered"]:
        return False
    return bool(res.get("error_type") == "ProfileLeaseError" or res.get("error")
                or res.get("result") not in (None, "", "ok", "closed"))


# ───────────────────────── the real lease is on ─────────────────────────

def test_workers_run_with_the_real_checkout_lease(cluster, sig, profile):
    got = cluster.run("A", "probe_checkout", profile, sig=sig).result()
    assert got["result"] == "profile_lease"     # a warm checkout without the lease is refused in a worker


# ───────────────────────── mixed-path pairs on ONE profile ─────────────────────────

@pytest.mark.parametrize("holder,contender", PAIRS)
def test_same_profile_pair_never_runs_concurrently(cluster, sig, profile, holder, contender):
    first = cluster.run("A", holder, profile, sig=sig, hold=True)
    assert first.wait_entered(), first.result(1)
    second = cluster.run("B", contender, profile, sig=sig)
    refused = second.result()
    assert refused_explicitly(refused), refused          # the other process did not run its caller
    first.release()
    held = first.result()
    assert held["entered"] and "error_type" not in held, held
    third = cluster.run("B", contender, profile, sig=sig).result()    # the lease is free again
    assert third["entered"], third
    assert at(sig, first, "exit") < at(sig, Job(cluster, "B", {"job": f"B{cluster._n}", "sig": str(sig)}), "enter")


@pytest.mark.parametrize("holder,contender", [("create", "read"), ("read", "command"), ("command", "checkout")])
def test_same_profile_waiting_contender_runs_only_after_the_holder(cluster, sig, profile, holder, contender):
    first = cluster.run("A", holder, profile, sig=sig, hold=True)
    assert first.wait_entered()
    second = cluster.run("B", contender, profile, sig=sig, wait=60)
    time.sleep(1.5)
    assert not second.entered() and not second.done()    # still waiting, never concurrent
    first.release()
    assert first.result()["entered"]
    got = second.result()
    assert got["entered"] and "error_type" not in got, got
    assert at(sig, first, "exit") < at(sig, second, "enter")


# ───────────────────────── the same pairs on TWO profiles ─────────────────────────

@pytest.mark.parametrize("holder,contender", PAIRS)
def test_different_profiles_pair_runs_independently(cluster, sig, holder, contender):
    one, other = str(uuid.uuid4()), str(uuid.uuid4())
    first = cluster.run("A", holder, one, sig=sig, hold=True)
    assert first.wait_entered()
    second = cluster.run("B", contender, other, sig=sig)
    got = second.result()                                # does not wait for the held profile
    assert got["entered"] and "error_type" not in got, got
    assert not (sig / f"release-{first.id}").exists()    # the holder is still inside
    first.release()
    assert first.result()["entered"]
    assert at(sig, second, "exit") < at(sig, first, "exit")


# ───────────────────────── aliases of one physical profile ─────────────────────────

def test_alias_spelling_converges_to_one_owner_across_processes(cluster, sig, profile):
    alias = "{" + profile.upper().replace("-", "") + "}"
    # braces and no dashes: still the same physical profile
    first = cluster.run("A", "cleanup_idle", profile, sig=sig, hold=True, map_profile=alias)
    assert first.wait_entered()
    got = cluster.run("B", "read", profile, sig=sig).result()
    assert not got["entered"]
    first.release()
    assert first.result()["entered"]
    cluster.run("B", "read", profile, sig=sig).result()  # free again, no error


# ───────────────────────── idle/deferred cleanup never closes a held profile ─────────────────────────

@pytest.mark.parametrize("holder", ["read", "create"])
@pytest.mark.parametrize("cleanup", ["cleanup_warm", "cleanup_idle", "cleanup_deferred"])
def test_cleanup_does_not_stop_a_profile_held_by_another_process(cluster, sig, profile, holder, cleanup):
    first = cluster.run("A", holder, profile, sig=sig, hold=True)
    assert first.wait_entered()
    blocked = cluster.run("B", cleanup, profile, sig=sig).result()
    assert not blocked["entered"] and blocked["stops"] == [], blocked      # no stop was sent
    if cleanup == "cleanup_deferred":
        assert blocked["pending"] is True                                 # the close stays pending, not lost
    else:
        assert blocked["result"] == "busy", blocked                          # explicit, safe refusal
    first.release()
    assert first.result()["entered"]
    ok = cluster.run("B", cleanup, profile, sig=sig).result()             # after the holder: it does close
    assert ok["entered"] and len(ok["stops"]) == 1, ok


# ───────────────────────── cancel, crash, exit ─────────────────────────

def test_cancel_in_the_holder_releases_the_lease_and_the_waiter_runs(cluster, sig, profile):
    first = cluster.run("A", "create", profile, sig=sig, hold=True, then="cancel")
    assert first.wait_entered()
    waiter = cluster.run("B", "command", profile, sig=sig, wait=60)
    time.sleep(1.0)
    assert not waiter.entered()
    first.release()
    got = first.result()
    assert got["result"] == "cancelled" and got["entered"]      # Stop reached the operation under the lease
    assert not [e for e in events(sig) if e["job"] == first.id and e["ev"] == "exit"]
    assert waiter.result()["entered"]                           # ... and the other process got the profile
    assert cluster.alive("A")                                   # released by unwinding, not by process death
    assert cluster.run("A", "ping", str(uuid.uuid4()), sig=sig).result()["result"] == "pong"
    journal = cluster.root / "A" / "journal.jsonl"
    assert (not journal.exists()) or "profile_lease_leaked" not in journal.read_text(encoding="utf-8")


def test_cancel_of_a_waiting_contender_runs_nothing_and_leaves_no_trace(cluster, sig, profile):
    first = cluster.run("A", "read", profile, sig=sig, hold=True)
    assert first.wait_entered()
    waiter = cluster.run("B", "create", profile, sig=sig, wait=60, cancel_after=0.5)
    got = waiter.result()
    assert got["result"] == "cancelled" and not got["entered"]
    first.release()
    assert first.result()["entered"]
    assert cluster.run("B", "create", profile, sig=sig).result()["entered"]   # no stale claim left behind


def test_holder_killed_mid_operation_releases_the_lease(cluster, sig, profile):
    cluster.start("K1")
    cluster.ready("K1", sig)
    doomed = cluster.run("K1", "command", profile, sig=sig, hold=True)
    assert doomed.wait_entered()
    blocked = cluster.run("B", "command", profile, sig=sig).result()
    assert not blocked["entered"]
    cluster.kill("K1")
    got = cluster.run("B", "command", profile, sig=sig, wait=20).result()
    assert got["entered"] and "error_type" not in got, got


def test_crash_after_an_external_write_releases_and_the_write_is_never_replayed(cluster, sig, profile):
    before = len(posts(cluster))
    cluster.start("K2")
    cluster.ready("K2", sig)
    crashing = cluster.run("K2", "turn_off", profile, sig=sig, meta="crash_after_post")
    crashing.wait_entered()
    deadline = time.monotonic() + 30
    while cluster.alive("K2") and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not cluster.alive("K2")                                      # the process died inside the write
    assert len(posts(cluster)) == before + 1                            # the request left exactly once
    for scenario in ("command", "read", "checkout"):                    # every caller can enter at once
        got = cluster.run("B", scenario, profile, sig=sig, wait=20).result()
        assert got["entered"] and "error_type" not in got, (scenario, got)
    cleanup = cluster.run("B", "cleanup_warm", profile, sig=sig).result()
    assert cleanup["entered"]
    assert len(posts(cluster)) == before + 1                            # nothing replayed it, unknown stays unknown


def test_unknown_write_stays_unconfirmed_is_not_retried_and_releases_on_exit(cluster, sig, profile):
    before = len(posts(cluster))
    got = cluster.run("A", "turn_off", profile, sig=sig, meta="lost").result()
    assert got["result"] == "unconfirmed", got                          # the answer was lost: reported unknown
    assert len(posts(cluster)) == before + 1                            # one POST, no automatic retry
    after = cluster.run("B", "command", profile, sig=sig, wait=20).result()   # normal exit freed the lease
    assert after["entered"]
    assert len(posts(cluster)) == before + 1
