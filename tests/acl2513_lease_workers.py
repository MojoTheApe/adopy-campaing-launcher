"""ACL-2513: worker processes that drive the REAL callers under the REAL profile lease.

Not a test module. `tests/test_acl2513_lease_callers_process.py` spawns two or more of these
workers (same OS user, one shared lease folder) and asks them to run one caller each:

    read      account_spend_api.read_country_split_via_api (read lease, warm session, checkout)
    command   campaign_commands.open_session(commands=True)
    turn_off  account_spend_api.turn_off (a command that WRITES; transport can lose the answer)
    create    publish_jobs publish lease + leak watch + publish_api.ApiPublisher.open/finish_job
    checkout  account_spend_api._warm_session (the side path that takes its own lease)
    cleanup_warm      WarmRegistry.close_one
    cleanup_idle      publish_jobs._stop_idle_profile (idle kept window / Close profile)
    cleanup_deferred  account_spend._close_if_still_pending (deferred close after a read)

Everything outside the profile lease is synthetic: the Graph transport is `MetaGraph` over a
shared file (the "Meta" of the test), Multilogin start/stop is a fake that only logs, tabs are
fakes. No live profile, activation or budget edit. The conftest autouse fixtures do NOT apply
in a worker, so `warm._require_checkout_lease` is the real one here (`probe_checkout`).

Per-worker, not shared: the account-refresh machine folder (its one-read-per-machine lease is a
different mechanism from the profile lease and would serialize reads of DIFFERENT profiles), the
warm registry and the stop log. Shared: the profile lease folder and the fake Meta.
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]

CAMP = "1000001"
ACT = "act_123456789"


# ───────────────────────── signals between the test and a worker (plain files) ─────────────────────────

def _touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("1", encoding="utf-8")


def _wait_file(path: Path, seconds: float) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if path.exists():
            return True
        time.sleep(0.02)
    return path.exists()


def _log(sig: Path, job: str, ev: str, what: str = "") -> None:
    line = json.dumps({"job": job, "ev": ev, "what": what, "t": time.perf_counter_ns(), "pid": os.getpid()})
    fd = os.open(str(sig / "events.jsonl"), os.O_WRONLY | os.O_APPEND | os.O_CREAT)
    try:
        os.write(fd, (line + "\n").encode())
    finally:
        os.close(fd)


class Gate:
    """The point INSIDE a caller where the synthetic transport is reached - i.e. under the lease."""

    def __init__(self, job: dict):
        self.job = job
        self.sig = Path(job["sig"])
        self.id = job["job"]
        self.entered = False

    def __call__(self, what: str) -> None:
        if self.entered:
            return
        self.entered = True
        _log(self.sig, self.id, "enter", what)
        _touch(self.sig / f"entered-{self.id}")
        if self.job.get("hold"):
            _wait_file(self.sig / f"release-{self.id}", 60)
        action = self.job.get("then")
        if action == "crash":
            os._exit(9)                       # the process dies while it holds the lease
        if action == "cancel":
            from src.fb_ads_rpa import job_cancel
            raise job_cancel.JobCancelled("Stopped by the user")
        _log(self.sig, self.id, "exit", what)


# ───────────────────────── the synthetic Graph transport ─────────────────────────

class SharedMeta:
    """GET by id, POST status only; every POST is appended to one file shared by all workers."""

    def __init__(self, root: Path, gate: Gate, mode: str = ""):
        self.dir = root / "meta"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.posts_file = self.dir / "posts.jsonl"
        self.gate = gate
        self.mode = mode

    def posts(self) -> list[dict]:
        if not self.posts_file.exists():
            return []
        return [json.loads(x) for x in self.posts_file.read_text(encoding="utf-8").splitlines() if x]

    def _status(self, oid: str) -> str:
        return "PAUSED" if any(p["path"].strip("/") == oid for p in self.posts()) else "ACTIVE"

    def get(self, path: str):
        self.gate("graph read")
        oid = path.strip("/")
        if oid == CAMP:
            status = self._status(oid)
            if self.mode == "lost" and status == "PAUSED":
                status = "ACTIVE"             # the lost write did not apply (and the client cannot know)
            return {"status": 200, "body": {"id": CAMP, "name": "Camp One", "account_id": ACT[4:],
                                            "status": status, "effective_status": status,
                                            "objective": "OUTCOME_LEADS"}}
        return {"status": 200, "body": {"id": oid, "account_status": 1, "currency": "USD",
                                        "disable_reason": 0, "name": "Acc", "data": []}}

    def write(self, path: str, params: dict):
        from src.fb_api import write_policy as wp

        self.gate("graph write")
        wp.check_object_update("POST", path, params, None)
        fd = os.open(str(self.posts_file), os.O_WRONLY | os.O_APPEND | os.O_CREAT)
        try:
            os.write(fd, (json.dumps({"path": path, "params": params, "pid": os.getpid()}) + "\n").encode())
        finally:
            os.close(fd)
        if self.mode == "crash_after_post":
            os._exit(9)                        # the request left, the answer never came: outcome unknown
        if self.mode == "lost":
            return {"error": "call_unknown"}
        return {"status": 200, "body": {"success": True}}


def _meta_graph_class():
    from tests.test_acl1812_warm_sessions import FakeGraph

    class MetaGraph(FakeGraph):
        def __init__(self, tab, meta: SharedMeta):
            super().__init__(tab)
            self.meta = meta

        def state(self):
            return dict(self.fresh_state)

        def request(self, method, path, params=None):
            assert method == "GET"
            return self.meta.get(path)

        def write(self, path, params, files=None, filename=""):
            return self.meta.write(path, params)

    return MetaGraph


# ───────────────────────── per-worker setup ─────────────────────────

class World:
    """One job's fakes. Rebuilt for every job: nothing leaks from one scenario to the next."""

    def __init__(self, base: Path, shared: Path, job: dict, counter: int):
        from src.analysis_agent import account_spend as sp
        from src.analysis_agent import account_spend_api as api
        from src.fb_api import graph as g
        from src.fb_api import warm
        from tests.test_acl1812_warm_sessions import ACT as RIG_ACT
        from tests.test_acl1812_warm_sessions import Rig

        self.job = job
        self.pid = job["profile"]
        self.gate = Gate(job)
        self.meta = SharedMeta(shared, self.gate, job.get("meta", ""))
        self.rig = Rig(base / f"job-{counter}")
        self.stops: list[str] = []
        self.entry = SimpleNamespace(profile_id=job.get("map_profile") or self.pid, folder_id="F",
                                     multilogin_instance="A", ad_account_id=RIG_ACT)
        graph_cls = _meta_graph_class()
        meta = self.meta

        # the registry of this process: the profile is warm and was started by the dashboard
        warm._REGISTRY = self.rig.reg
        self.rig.warm_up(self.pid)
        self.rig.reg.register(self.pid, account="Acc", port=9333, dashboard_started=True)
        real_stop = self.rig.reg._close_window

        def stop(entry, label):                  # synthetic Multilogin stop; the gate marks "under the lease"
            self.gate("mlx stop")
            self.stops.append(label)
            real_stop(entry, label)

        self.rig.reg._close_window = stop

        sp._entry_for = lambda account: self.entry
        sp._set_wait_note = lambda *a: None
        sp._store_path = lambda: base / "spend.json"
        api._COOLDOWN.clear()
        api._make_graph = lambda tab, **kw: graph_cls(tab, meta)
        api.GraphClient = lambda tr, **kw: g.GraphClient(tr, sleep=lambda s: None, **{**kw, "min_interval_s": 0.0})
        api.TURN_OFF_RECONCILE_PAUSE_S = 0.0
        api._open_gated = lambda *a, **k: (_ for _ in ()).throw(AssertionError("a warm profile is not opened again"))


def _apply_waits(job: dict) -> None:
    from src.fb_ads_rpa import operations
    from src.fb_ads_rpa import profile_lease as pl

    wait = float(job.get("wait", 0.4))
    operations.READ_WAIT_SECONDS = wait
    pl.WAIT_SECONDS = wait
    pl.PUBLISH_WAIT_SECONDS = wait


def setup_process(root: Path, name: str) -> Path:
    """Private state per worker; the lease folder and the fake Meta are shared (`root`)."""
    base = root / name
    base.mkdir(parents=True, exist_ok=True)
    os.environ["ACL_CONFIG_DIR"] = str(base / "config")
    os.environ["FB_RPA_KEEP_PROFILES_OPEN"] = "1"
    os.environ["FB_RPA_REOPEN_HAND_WINDOW"] = "0"
    sys.path.insert(0, str(ROOT))
    from src.analysis_agent import account_refresh_state
    from src.fb_ads_rpa import profile_lease as pl
    from src.fb_ads_rpa import run_telemetry
    from src.fb_api import telemetry

    pl._lease_root = lambda: root / "leases"                       # the same folder for every worker
    account_refresh_state.machine_root = lambda: base / "coordination"
    run_telemetry.TELEMETRY_PATH = base / "rpa_telemetry.jsonl"
    telemetry.journal_path = lambda: base / "journal.jsonl"
    return base


# ───────────────────────── scenarios ─────────────────────────

def _run_read(w: World) -> dict:
    from src.analysis_agent import account_spend_api as api

    res = api.read_country_split_via_api("Acc", "2026-01-01")
    return {"result": str(getattr(res, "error", "") or getattr(res, "state", ""))[:200]}


def _run_command(w: World) -> dict:
    from src.web import campaign_commands as cc

    with cc.open_session("Acc", True) as sess:
        sess.client.get(f"/{CAMP}", {"fields": "name"})
    return {"result": "ok"}


def _run_turn_off(w: World) -> dict:
    from src.analysis_agent import account_spend_api as api

    out = api.turn_off("Acc", "campaign", CAMP, expected_name="Camp One", requester="Dana")
    return {"result": out.state, "error": out.error, "posts": len(w.meta.posts())}


def _run_checkout(w: World) -> dict:
    from src.analysis_agent import account_spend_api as api
    from src.fb_ads_rpa.profile_lease import physical_profile_key

    key = physical_profile_key(w.entry.profile_id)
    with api._warm_session("Acc", ACT, key, w.entry.profile_id, False) as sess:
        sess.client.get(f"/{ACT}")
    return {"result": "ok"}


def _run_create(w: World) -> dict:
    from src.fb_ads_rpa import job_cancel
    from src.web import publish_api, publish_jobs

    gate = w.gate
    cancel_after = w.job.get("cancel_after")
    started = time.monotonic()

    def cancelled() -> bool:
        return bool(cancel_after) and time.monotonic() - started > float(cancel_after)

    publish_api._resolve_entry = lambda label: w.entry
    publish_api._open_profile = lambda *a: (_ for _ in ()).throw(AssertionError("a warm profile is not opened again"))
    publish_api._open_tab = lambda url: (_ for _ in ()).throw(AssertionError("no own tab for a warm profile"))
    graph_cls = _meta_graph_class()
    publish_api._make_graph = lambda tab, act, *, writes=True: graph_cls(tab, w.meta)

    def preflight(client, act):
        client.graph.request("GET", f"/{act}")   # the synthetic Graph answer; the gate sits in the transport
        return SimpleNamespace()

    publish_api.preflight_account = preflight
    publish_api.TargetingResolver = lambda client, **k: object()
    publish_api.GraphClient = lambda graph, **kw: SimpleNamespace(graph=graph, **kw)
    publish_api._cooldown = lambda label: (0.0, "")
    profile = w.entry.profile_id
    try:
        with publish_jobs._publish_serial(profile, cancelled=cancelled), publish_jobs._leak_watch(profile, "publish"):
            pub = publish_api.ApiPublisher(dry_run=True)
            pub.open("Acc")
            pub.finish_job("Acc", clean=True)
    except job_cancel.JobCancelled:
        return {"result": "cancelled"}
    return {"result": "ok"}


def _run_cleanup_warm(w: World) -> dict:
    res = w.rig.reg.close_one("Acc", w.entry)
    return {"result": res["code"]}


def _run_cleanup_idle(w: World) -> dict:
    from src.fb_ads_rpa import browser as rpa_browser
    from src.fb_ads_rpa import rpa_publisher
    from src.web import publish_jobs

    rpa_publisher._resolve_account = lambda label: w.entry

    def stop_profile(entry, label):
        w.gate("mlx stop")
        w.stops.append(label)
        return "stopped"

    rpa_browser.stop_profile_on_request = stop_profile
    res = publish_jobs._stop_idle_profile("Acc", w.entry.profile_id, busy_msg="busy", strict=True)
    return {"result": str(res.get("code") or "closed")}


def _run_cleanup_deferred(w: World) -> dict:
    from src.analysis_agent import account_spend as sp
    from src.fb_ads_rpa import browser as rpa_browser

    def stop_profile(entry, label):
        w.gate("mlx stop")
        w.stops.append(label)
        return "stopped"

    rpa_browser.stop_profile_on_request = stop_profile
    sp._publish_touched = lambda opened: False
    opened = sp._ReadOpen(account="Acc", started=True, profile_id=w.entry.profile_id, entry=w.entry,
                          started_mono=time.monotonic())
    sp._PENDING_CLOSE[w.entry.profile_id] = opened
    sp._close_if_still_pending(opened)
    pending = w.entry.profile_id in sp._PENDING_CLOSE
    sp._PENDING_CLOSE.pop(w.entry.profile_id, None)
    return {"result": "pending" if pending else "closed", "pending": pending}


def _run_probe_checkout(w: World) -> dict:
    """The REAL lease check of `WarmRegistry.checkout` is on in a worker (no conftest fixture)."""
    from src.fb_api import warm

    try:
        w.rig.checkout(w.pid)
    except warm.WarmError as exc:
        return {"result": exc.code}
    return {"result": "checked out without a lease"}


def _run_ping(w: World) -> dict:
    return {"result": "pong"}


SCENARIOS = {
    "read": _run_read, "command": _run_command, "turn_off": _run_turn_off, "checkout": _run_checkout,
    "create": _run_create, "cleanup_warm": _run_cleanup_warm, "cleanup_idle": _run_cleanup_idle,
    "cleanup_deferred": _run_cleanup_deferred, "probe_checkout": _run_probe_checkout, "ping": _run_ping,
}


def run_job(base: Path, shared: Path, job: dict, counter: int) -> dict:
    _apply_waits(job)
    w = World(base, shared, job, counter)
    try:
        out = SCENARIOS[job["scenario"]](w)
    except BaseException as exc:  # noqa: BLE001 - report, never die silently
        out = {"error_type": type(exc).__name__, "error": str(exc)[:300],
               "trace": traceback.format_exc()[-600:]}
    return {"entered": w.gate.entered, "stops": list(w.stops), **out}


def worker_main(root: str, name: str, commands, results) -> None:
    shared = Path(root)
    base = setup_process(shared, name)
    counter = 0
    while True:
        job = commands.get()
        if job is None:
            return
        counter += 1
        out = run_job(base, shared, job, counter)
        _log(Path(job["sig"]), job["job"], "done", job["scenario"])
        results.put({"job": job["job"], **out})
